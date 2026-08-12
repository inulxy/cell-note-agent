#!/usr/bin/env python3
"""Setup git repo and push cell-note-agent to GitHub.

Usage:
    python push_to_github.py --token YOUR_GITHUB_TOKEN
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"[run] {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[stderr] {result.stderr.strip()}")
    if check:
        result.check_returncode()
    return result


def create_github_repo(token: str, repo_name: str) -> None:
    url = "https://api.github.com/user/repos"
    data = json.dumps({"name": repo_name, "private": False, "description": "Stage-aware discovery agent for public scATAC-seq and paired RNA-ATAC multiome datasets"}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[github] repo created: {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code == 422:
            print(f"[github] repo already exists: {e.code}")
        else:
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="GitHub personal access token")
    parser.add_argument("--repo", default="cell-note-agent")
    parser.add_argument("--user", default="inulxy")
    args = parser.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # Init repo if needed
    if not os.path.exists(".git"):
        run(["git", "init"])
        run(["git", "checkout", "-b", "main"])

    run(["git", "add", "."])
    run(["git", "commit", "-m", "chore: rename to cell-note-agent"])

    remote_url = f"https://{args.user}:{args.token}@github.com/{args.user}/{args.repo}.git"
    if "origin" not in subprocess.run(["git", "remote"], capture_output=True, text=True).stdout:
        run(["git", "remote", "add", "origin", remote_url])
    else:
        run(["git", "remote", "set-url", "origin", remote_url])

    create_github_repo(args.token, args.repo)
    run(["git", "push", "-u", "origin", "main"])
    print(f"[done] pushed to https://github.com/{args.user}/{args.repo}")


if __name__ == "__main__":
    main()
