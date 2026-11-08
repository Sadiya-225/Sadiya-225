"""Mirror every repository owned by SOURCE_USER into DEST_USER.

Guarantees
- Complete: walks every page of /user/repos (owner affiliation) and fails loudly
  if any page cannot be fetched, instead of silently mirroring a partial list.
- No duplicates: repos are de-duplicated by id and by case-insensitive name, and
  each destination repo is created at most once.
- Up to date: branches and tags are force-synced and pruned, so new pushes,
  force-pushes, deleted branches and deleted tags in the source are reflected.
  Repos whose refs already match are skipped (cheap hourly runs).
- Faithful metadata: description, homepage, topics, default branch and archived
  state are copied over to the destination.

Environment
  GITHUB_TOKEN   token for SOURCE_USER (read access to all repos, incl. private)
  DEST_TOKEN     token for DEST_USER (repo scope; delete_repo if PRUNE_DELETED=true)
  PRUNE_DELETED  delete destination repos that no longer exist in the source
                 (default: true; set "false" to only report them)
  STATE_FILE     JSON file remembering how each copy was published (default: mirror-state.json)
  MIRROR_VISIBILITY  "public" (default, legacy behaviour: every copy is public) or
                 "match" (copies keep the source's public/private visibility)
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import requests

SOURCE_USER = "Sadiya-125"
DEST_USER = "Sadiya-225"
# The profile repo of the destination account; mirroring over it would overwrite
# this very repository (workflow + README), so it is never touched.
PROTECTED_DEST_REPOS = {DEST_USER.lower()}

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
DEST_TOKEN = os.environ.get("DEST_TOKEN", "").strip()
PRUNE_DELETED = os.environ.get("PRUNE_DELETED", "true").strip().lower() != "false"
MIRROR_VISIBILITY = os.environ.get("MIRROR_VISIBILITY", "public").strip().lower()

API = "https://api.github.com"

# Remembers, per repo, how the copy is published so hourly runs stay cheap and a
# private fallback is never flipped public again:
#   {repo_lowercase: {"mode": "public" | "sanitized" | "private", "src": <hash of source refs>}}
STATE_FILE = os.environ.get("STATE_FILE", "mirror-state.json")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def refs_hash(refs):
    return hashlib.sha1(json.dumps(sorted(refs.items())).encode()).hexdigest()


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


source_headers = headers(TOKEN)
dest_headers = headers(DEST_TOKEN)


def redact(text):
    for secret in (TOKEN, DEST_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def request(method, url, retries=6, **kwargs):
    """requests wrapper that retries on rate limits and transient 5xx errors."""
    for attempt in range(retries):
        r = requests.request(method, url, timeout=60, **kwargs)
        if r.status_code in (403, 429) and "secondary rate limit" in r.text.lower():
            wait = int(r.headers.get("Retry-After", 0)) or 60 * (attempt + 1)
            print(f"  Secondary rate limit hit, sleeping {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = min(max(reset - time.time(), 1), 300)
            print(f"  Rate limited, sleeping {wait:.0f}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r
    return r


def remove_readonly(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def safe_rmtree(path):
    if os.path.exists(path):
        shutil.rmtree(path, onerror=remove_readonly)


def paginate(url, hdrs):
    """Return every item across all pages; raise if a page fails."""
    items, page = [], 1
    sep = "&" if "?" in url else "?"
    while True:
        r = request("GET", f"{url}{sep}per_page=100&page={page}", headers=hdrs)
        if r.status_code != 200:
            raise RuntimeError(f"GET {url} page {page} failed: {r.status_code} {r.text}")
        data = r.json()
        if not data:
            return items
        items.extend(data)
        if len(data) < 100:
            return items
        page += 1


def dedupe(repos):
    """Drop duplicates by id and by case-insensitive name (GitHub names are case-insensitive)."""
    seen_ids, seen_names, unique = set(), set(), []
    for repo in repos:
        name = repo["name"].lower()
        if repo["id"] in seen_ids or name in seen_names:
            continue
        seen_ids.add(repo["id"])
        seen_names.add(name)
        unique.append(repo)
    return unique


def get_source_repos():
    repos = paginate(f"{API}/user/repos?affiliation=owner&sort=created&direction=asc", source_headers)
    repos = [r for r in repos if r["owner"]["login"].lower() == SOURCE_USER.lower()]
    return dedupe(repos)


def get_dest_repos():
    repos = paginate(f"{API}/user/repos?affiliation=owner", dest_headers)
    return {r["name"].lower(): r for r in repos if r["owner"]["login"].lower() == DEST_USER.lower()}


def ensure_dest_repo(repo, dest_repos):
    """Create the destination repo if missing. Returns True when it exists afterwards."""
    key = repo["name"].lower()
    if key in dest_repos:
        return True
    r = request(
        "POST",
        f"{API}/user/repos",
        headers=dest_headers,
        json={
            "name": repo["name"],
            "private": repo["private"] if MIRROR_VISIBILITY == "match" else False,
            "description": repo.get("description") or "",
            "homepage": repo.get("homepage") or "",
        },
    )
    if r.status_code == 201:
        dest_repos[key] = r.json()
        print(f"  Created repo: {repo['name']}")
        time.sleep(5)  # stay under GitHub's secondary rate limit for content creation
        return True
    print(f"  Failed to create {repo['name']}: {r.status_code} {r.json().get('message', r.text)}")
    return False


def sync_metadata(repo, dest_repo):
    """Copy description, homepage, topics and archived state. Default branch is handled after push."""
    wanted = {
        "description": repo.get("description") or "",
        "homepage": repo.get("homepage") or "",
    }
    current = {k: dest_repo.get(k) or "" for k in wanted}
    if wanted != current:
        r = request("PATCH", f"{API}/repos/{DEST_USER}/{dest_repo['name']}", headers=dest_headers, json=wanted)
        if r.status_code != 200:
            print(f"  Warning: metadata update failed: {r.status_code} {r.text}")

    topics = sorted(repo.get("topics") or [])
    r = request("GET", f"{API}/repos/{DEST_USER}/{dest_repo['name']}/topics", headers=dest_headers)
    if r.status_code == 200 and sorted(r.json().get("names", [])) != topics:
        r = request(
            "PUT",
            f"{API}/repos/{DEST_USER}/{dest_repo['name']}/topics",
            headers=dest_headers,
            json={"names": topics},
        )
        if r.status_code != 200:
            print(f"  Warning: topics update failed: {r.status_code} {r.text}")


def finalize_repo_state(repo, dest_repo_name, mode="public"):
    """After refs are pushed: align default branch, then archived state (archived repos are read-only)."""
    patch = {}
    default_branch = repo.get("default_branch")
    r = request("GET", f"{API}/repos/{DEST_USER}/{dest_repo_name}", headers=dest_headers)
    if r.status_code != 200:
        return
    current = r.json()
    if default_branch and current.get("default_branch") != default_branch:
        patch["default_branch"] = default_branch
    want_private = True if mode == "private" else (repo["private"] if MIRROR_VISIBILITY == "match" else False)
    if bool(current.get("private")) != bool(want_private):
        patch["private"] = bool(want_private)
    if bool(current.get("archived")) != bool(repo.get("archived")):
        patch["archived"] = bool(repo.get("archived"))
    if patch:
        r = request("PATCH", f"{API}/repos/{DEST_USER}/{dest_repo_name}", headers=dest_headers, json=patch)
        if r.status_code != 200:
            print(f"  Warning: state update failed: {r.status_code} {r.text}")


def git(args, cwd=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def ls_refs(url, quiet=False):
    """Return {ref: sha} for branches and tags, or None if the remote can't be read."""
    result = git(["ls-remote", "--heads", "--tags", url])
    if result.returncode != 0:
        if not quiet:
            print(f"  ls-remote failed: {redact(result.stderr).strip()}")
        return None
    refs = {}
    for line in result.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):  # peeled tag, the tag object itself is already listed
            continue
        refs[ref] = sha
    return refs


SECRET_BLOCK = "GH013"
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/.]{20,}={0,2}")
MAX_CLEAN_ROUNDS = 10


def blocked_secret_tokens(stderr, mirror_dir):
    """Extract the secret strings GitHub push protection blocked.

    GitHub reports only commit + path:line, not the value, so read that line and
    take its long, key-looking tokens (mixed letters and digits, or 32+ chars).
    """
    text = re.sub(r"^remote:\s?", "", stderr, flags=re.M)
    tokens = set()
    for commit, path, line in re.findall(r"commit:\s*([0-9a-f]{40})\s*\n\s*path:\s*(.+?):(\d+)\s*\n", text):
        blob = git(["show", f"{commit}:{path}"], cwd=mirror_dir)
        lines = blob.stdout.splitlines()
        if blob.returncode != 0 or int(line) > len(lines):
            continue
        for tok in TOKEN_RE.findall(lines[int(line) - 1]):
            if len(tok) >= 32 or (re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok)):
                tokens.add(tok)
    return tokens


def rewrite_history(mirror_dir, work, replacements=(), strip_big=False):
    """git filter-repo: redact literal strings and/or drop blobs GitHub would reject."""
    cmd = ["filter-repo", "--force"]
    if replacements:
        rules = os.path.join(work, "replacements.txt")
        with open(rules, "w", encoding="utf-8") as fh:
            for tok in sorted(replacements):
                fh.write(f"literal:{tok}==>***REMOVED***\n")
        cmd += ["--replace-text", rules]
    if strip_big:
        cmd += ["--strip-blobs-bigger-than", "95M"]
    result = git(cmd, cwd=mirror_dir)
    if result.returncode != 0:
        print(f"  git filter-repo failed: {redact(result.stderr).strip()[:500]}")
    return result.returncode == 0


def push_refs(mirror_dir, dest_url):
    return git(
        ["push", "--prune", "--force", dest_url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
        cwd=mirror_dir,
    )


def clone_source(src_url, work, name):
    mirror_dir = os.path.join(work, f"{name}.git")
    safe_rmtree(mirror_dir)
    result = git(["clone", "--bare", src_url, mirror_dir])
    if result.returncode != 0:
        print(f"  Failed to clone: {redact(result.stderr)}")
        return None
    return mirror_dir


def set_dest_private(dest_name):
    r = request("PATCH", f"{API}/repos/{DEST_USER}/{dest_name}", headers=dest_headers, json={"private": True})
    return r.status_code == 200


def mirror_repo(repo, dest_name, entry):
    """Mirror one repo. Returns (ok, entry) where entry is the new state for this repo."""
    src_url = f"https://x-access-token:{TOKEN}@github.com/{SOURCE_USER}/{repo['name']}.git"
    dest_url = f"https://x-access-token:{DEST_TOKEN}@github.com/{DEST_USER}/{dest_name}.git"
    mode = entry.get("mode", "public")

    src_refs = ls_refs(src_url)
    if src_refs is None:
        print("  Failed to read source refs")
        return False, entry
    src_hash = refs_hash(src_refs)

    if mode == "public":
        up_to_date = ls_refs(dest_url, quiet=True) == src_refs
    else:  # rewritten or private: dest refs differ from source, so compare against what we last saw
        up_to_date = entry.get("src") == src_hash
    if up_to_date:
        print(f"  Already up to date ({mode})")
        return True, entry

    work = tempfile.mkdtemp(prefix="mirror-")
    try:
        print(f"  Cloning {repo['name']}...")
        mirror_dir = clone_source(src_url, work, "repo")
        if mirror_dir is None:
            return False, entry

        def done(new_mode):
            return True, {"mode": new_mode, "src": src_hash}

        # Private fallback copies are private on purpose: push everything unmodified.
        if mode == "private":
            if not set_dest_private(dest_name):
                print("  Failed to make the copy private; refusing to push")
                return False, entry
            result = push_refs(mirror_dir, dest_url)
            if result.returncode == 0 or not src_refs:
                return done("private")
            print(f"  Failed to push: {redact(result.stderr)[:800]}")
            return False, entry

        print(f"  Pushing to {DEST_USER}/{dest_name}...")
        cleaned = mode == "sanitized"
        for round_no in range(MAX_CLEAN_ROUNDS + 1):
            result = push_refs(mirror_dir, dest_url)
            if result.returncode == 0 or not src_refs:
                return done("sanitized" if cleaned else "public")

            err = result.stderr
            secrets_blocked = SECRET_BLOCK in err and "secret" in err.lower()
            too_big = "GH001" in err and "exceeds" in err.lower()
            if not (secrets_blocked or too_big) or round_no == MAX_CLEAN_ROUNDS:
                print(f"  Failed to push: {redact(err)[:800]}")
                return False, entry

            if secrets_blocked:
                tokens = blocked_secret_tokens(err, mirror_dir)
                if not tokens:
                    print("  Push blocked by secret scanning but no secret could be located; falling back to private")
                    break
                print(f"  Secret scanning blocked the push; redacting {len(tokens)} secret(s) from history (round {round_no + 1})")
                ok = rewrite_history(mirror_dir, work, replacements=tokens)
            else:
                print("  Files over GitHub's size limit found; stripping blobs larger than 95 MB from history")
                ok = rewrite_history(mirror_dir, work, strip_big=True)
            if not ok:
                break
            cleaned = True
        else:
            return False, entry

        # Could not be cleaned automatically: publish an unmodified copy privately instead.
        print("  Falling back to a PRIVATE copy")
        if not set_dest_private(dest_name):
            print("  Failed to make the copy private; refusing to push")
            return False, entry
        mirror_dir = clone_source(src_url, work, "repo-private")
        if mirror_dir is None:
            return False, entry
        result = push_refs(mirror_dir, dest_url)
        if result.returncode != 0:
            print(f"  Failed to push private copy: {redact(result.stderr)[:800]}")
            return False, entry
        return done("private")
    finally:
        safe_rmtree(work)


def preflight():
    """Verify both tokens can do what the run needs. Exits with a clear message if not."""
    problems = []
    for label, user, hdrs, needs in (
        ("GITHUB_TOKEN (source)", SOURCE_USER, source_headers, ["repo"]),
        ("DEST_TOKEN (destination)", DEST_USER, dest_headers, ["repo"] + (["delete_repo"] if PRUNE_DELETED else [])),
    ):
        r = request("GET", f"{API}/user", headers=hdrs)
        if r.status_code != 200:
            problems.append(f"{label}: rejected by GitHub ({r.status_code} {r.json().get('message', '')})")
            continue
        login = r.json()["login"]
        if login.lower() != user.lower():
            problems.append(f"{label}: belongs to '{login}', expected '{user}'")
        scopes_header = r.headers.get("X-OAuth-Scopes")
        if scopes_header is not None:  # classic token / OAuth: scopes are visible
            scopes = {x.strip() for x in scopes_header.split(",") if x.strip()}
            for need in needs:
                if need not in scopes:
                    problems.append(f"{label}: missing '{need}' scope (has: {', '.join(sorted(scopes)) or 'none'})")
        else:
            print(f"{label}: fine-grained token, scopes not introspectable (checked by use)")
        print(f"{label}: authenticated as {login}")
    if problems:
        print("\nToken problems:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)


def main():
    if not TOKEN or not DEST_TOKEN:
        sys.exit("GITHUB_TOKEN and DEST_TOKEN must both be set")
    preflight()

    print(f"Fetching repositories from {SOURCE_USER}...")
    source_repos = get_source_repos()
    dest_repos = get_dest_repos()
    # The API listing only needs metadata access; cloning needs contents access.
    # Fail fast with a clear message instead of failing once per repo.
    probe = next((r for r in source_repos if r["private"]), None)
    if probe and ls_refs(f"https://x-access-token:{TOKEN}@github.com/{SOURCE_USER}/{probe['name']}.git") is None:
        kind = "fine-grained" if TOKEN.startswith("github_pat_") else "classic" if TOKEN.startswith("ghp_") else "other (" + TOKEN[:4] + "...)"
        d = request("GET", f"{API}/repos/{SOURCE_USER}/{probe['name']}/contents/", headers=source_headers)
        print(f"Diagnostics: token type={kind}; contents API -> {d.status_code} {d.json().get('message', '') if d.headers.get('content-type', '').startswith('application/json') else ''}")
        print(f"Diagnostics: X-OAuth-Scopes={d.headers.get('X-OAuth-Scopes')!r}; accepted permissions={d.headers.get('X-Accepted-GitHub-Permissions')!r}")
        sys.exit(
            "GITHUB_TOKEN can list your repos but cannot read the contents of private ones "
            f"(tried {probe['name']}). Use a classic token with the 'repo' scope, or a fine-grained "
            "token with access to all repositories and 'Contents: Read-only' permission."
        )
    print(f"Found {len(source_repos)} source repositories ({len(dest_repos)} already on {DEST_USER})\n")

    state = load_state()
    success, failed, sanitized, private_copies = 0, [], [], []

    for repo in source_repos:
        name = repo["name"]
        visibility = "PRIVATE" if repo["private"] else "PUBLIC"
        print(f"[{visibility}] {name}")

        if name.lower() in PROTECTED_DEST_REPOS:
            print(f"  Skipped: would overwrite {DEST_USER}'s profile repository\n")
            continue

        try:
            if not ensure_dest_repo(repo, dest_repos):
                failed.append(name)
                continue
            dest_repo = dest_repos[name.lower()]
            ok, entry = mirror_repo(repo, dest_repo["name"], state.get(name.lower(), {}))
            if ok:
                state[name.lower()] = entry
                save_state(state)
                sync_metadata(repo, dest_repo)
                finalize_repo_state(repo, dest_repo["name"], entry.get("mode", "public"))
                success += 1
                if entry.get("mode") == "sanitized":
                    sanitized.append(name)
                elif entry.get("mode") == "private":
                    private_copies.append(name)
            else:
                failed.append(name)
        except Exception as exc:  # keep going so one bad repo can't block the rest
            print(f"  Error: {redact(str(exc))}")
            failed.append(name)
        print()

    # Destination repos with no counterpart in the source.
    source_names = {r["name"].lower() for r in source_repos}
    orphans = [r for k, r in dest_repos.items() if k not in source_names and k not in PROTECTED_DEST_REPOS]
    for orphan in orphans:
        if PRUNE_DELETED:
            r = request("DELETE", f"{API}/repos/{DEST_USER}/{orphan['name']}", headers=dest_headers)
            print(f"Deleted orphan {orphan['name']}: {r.status_code}")
        else:
            print(f"Orphan on {DEST_USER} (not in source, left untouched): {orphan['name']}")

    # Forget repos deleted at the source so a re-created repo of the same name starts clean.
    for key in list(state):
        if key not in source_names:
            del state[key]
    save_state(state)

    if sanitized:
        print("\nPublished with secrets redacted from history: " + ", ".join(sanitized))
    if private_copies:
        print("Kept PRIVATE on " + DEST_USER + " (secrets could not be cleaned automatically): " + ", ".join(private_copies))
    print(f"\nComplete! Success: {success}, Failed: {len(failed)}")
    if failed:
        print("Failed repos: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
