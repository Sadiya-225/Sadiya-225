import requests
import os
import subprocess
import shutil
import stat

def remove_readonly(func, path, _):
    """Clear the readonly bit and retry removal"""
    os.chmod(path, stat.S_IWRITE)
    func(path)

def safe_rmtree(path):
    """Remove directory tree, handling Windows read-only files"""
    if os.path.exists(path):
        shutil.rmtree(path, onexc=remove_readonly)

SOURCE_USER = "Sadiya-125"
DEST_USER = "Sadiya-225"

# Tokens from environment variables (for GitHub Actions) or fallback to hardcoded (for local testing)
# Strip whitespace/newlines that may be accidentally included in secrets
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
DEST_TOKEN = os.environ.get("DEST_TOKEN", "").strip()

# Headers for source account (reading repos)
source_headers = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github+json"
}

# Headers for destination account (creating repos)
dest_headers = {
    "Authorization": f"token {DEST_TOKEN}",
    "Accept": "application/vnd.github+json"
}

def get_repos():
    """Get all repositories (including private) for the authenticated user"""
    repos = []
    page = 1
    while True:
        # Use /user/repos endpoint to get private repos (requires authentication)
        url = f"https://api.github.com/user/repos?per_page=100&page={page}&affiliation=owner"
        r = requests.get(url, headers=source_headers)
        if r.status_code != 200:
            print(f"Error fetching repos: {r.json()}")
            break
        data = r.json()
        if not data:
            break
        # Filter to only include repos owned by SOURCE_USER
        repos.extend([repo for repo in data if repo['owner']['login'] == SOURCE_USER])
        page += 1
    return repos

def repo_exists(repo_name):
    url = f"https://api.github.com/repos/{DEST_USER}/{repo_name}"
    r = requests.get(url, headers=dest_headers)
    return r.status_code == 200

def create_repo(repo_name, private=True, description=""):
    url = "https://api.github.com/user/repos"
    data = {
        "name": repo_name,
        "private": private,
        "description": description or ""
    }
    r = requests.post(url, json=data, headers=dest_headers)
    if r.status_code == 201:
        print(f"  Created repo: {repo_name}")
        return True
    else:
        print(f"  Failed to create {repo_name}: {r.json().get('message', 'Unknown error')}")
        return False

def mirror_repo(repo):
    repo_name = repo["name"]
    mirror_dir = f"{repo_name}.git"

    # Clean up if directory exists from previous run
    safe_rmtree(mirror_dir)

    # Clone URL with authentication
    clone_url = repo["clone_url"].replace(
        "https://", f"https://{TOKEN}@"
    )

    # Clone as bare mirror
    print(f"  Cloning {repo_name}...")
    result = subprocess.run(
        ["git", "clone", "--mirror", clone_url],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"  Failed to clone {repo_name}: {result.stderr}")
        return False

    # Push to destination (using DEST_TOKEN for auth)
    dest_url = f"https://{DEST_TOKEN}@github.com/{DEST_USER}/{repo_name}.git"
    print(f"  Pushing to {DEST_USER}/{repo_name}...")
    result = subprocess.run(
        ["git", "push", "--mirror", dest_url],
        cwd=mirror_dir,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"  Failed to push {repo_name}: {result.stderr}")
        # Cleanup on failure
        safe_rmtree(mirror_dir)
        return False

    # Cleanup after successful push
    safe_rmtree(mirror_dir)
    print(f"  Successfully mirrored {repo_name}")
    return True

def main():
    print(f"Fetching repositories from {SOURCE_USER}...")
    source_repos = get_repos()
    print(f"Found {len(source_repos)} repositories to mirror\n")

    success_count = 0
    fail_count = 0

    for repo in source_repos:
        visibility = "PRIVATE" if repo['private'] else "PUBLIC"
        print(f"[{visibility}] {repo['name']}")

        # Create repo if it doesn't exist on destination (always public)
        if not repo_exists(repo["name"]):
            if not create_repo(repo["name"], private=False, description=repo.get("description")):
                fail_count += 1
                continue
        else:
            print(f"  Repo already exists on {DEST_USER}")

        # Mirror the repository
        if mirror_repo(repo):
            success_count += 1
        else:
            fail_count += 1

        print()

    print(f"\nComplete! Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    main()
