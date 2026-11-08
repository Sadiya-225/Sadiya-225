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
  MIRROR_VISIBILITY  "public" (default, legacy behaviour: every copy is public) or
                 "match" (copies keep the source's public/private visibility)
"""
import os
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


def request(method, url, retries=3, **kwargs):
    """requests wrapper that retries on rate limits and transient 5xx errors."""
    for attempt in range(retries):
        r = requests.request(method, url, timeout=60, **kwargs)
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


def finalize_repo_state(repo, dest_repo_name):
    """After refs are pushed: align default branch, then archived state (archived repos are read-only)."""
    patch = {}
    default_branch = repo.get("default_branch")
    r = request("GET", f"{API}/repos/{DEST_USER}/{dest_repo_name}", headers=dest_headers)
    if r.status_code != 200:
        return
    current = r.json()
    if default_branch and current.get("default_branch") != default_branch:
        patch["default_branch"] = default_branch
    want_private = repo["private"] if MIRROR_VISIBILITY == "match" else False
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
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


def ls_refs(url):
    """Return {ref: sha} for branches and tags, or None if the remote can't be read."""
    result = git(["ls-remote", "--heads", "--tags", url])
    if result.returncode != 0:
        print(f"  ls-remote failed: {redact(result.stderr).strip()}")
        return None
    refs = {}
    for line in result.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):  # peeled tag, the tag object itself is already listed
            continue
        refs[ref] = sha
    return refs


def mirror_repo(repo, dest_name):
    src_url = f"https://x-access-token:{TOKEN}@github.com/{SOURCE_USER}/{repo['name']}.git"
    dest_url = f"https://x-access-token:{DEST_TOKEN}@github.com/{DEST_USER}/{dest_name}.git"

    src_refs = ls_refs(src_url)
    if src_refs is None:
        print("  Failed to read source refs")
        return False
    if ls_refs(dest_url) == src_refs:
        print("  Already up to date")
        return True

    work = tempfile.mkdtemp(prefix="mirror-")
    try:
        mirror_dir = os.path.join(work, "repo.git")
        print(f"  Cloning {repo['name']}...")
        result = git(["clone", "--bare", src_url, mirror_dir])
        if result.returncode != 0:
            print(f"  Failed to clone: {redact(result.stderr)}")
            return False

        # Force-push only branches and tags (GitHub rejects its hidden refs/pull/*,
        # which is what makes `git push --mirror` fail). --prune removes refs that
        # were deleted in the source.
        print(f"  Pushing to {DEST_USER}/{dest_name}...")
        result = git(
            ["push", "--prune", "--force", dest_url,
             "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
            cwd=mirror_dir,
        )
        if result.returncode != 0:
            # An empty source repo has nothing to push; that is not an error.
            if not src_refs:
                return True
            print(f"  Failed to push: {redact(result.stderr)}")
            return False
        return True
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
    print(f"Found {len(source_repos)} source repositories ({len(dest_repos)} already on {DEST_USER})\n")

    success, failed = 0, []

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
            if mirror_repo(repo, dest_repo["name"]):
                sync_metadata(repo, dest_repo)
                finalize_repo_state(repo, dest_repo["name"])
                success += 1
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

    print(f"\nComplete! Success: {success}, Failed: {len(failed)}")
    if failed:
        print("Failed repos: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
