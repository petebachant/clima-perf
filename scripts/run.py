"""Run a CliMA performance benchmark for a given date."""

import argparse
import os
from datetime import datetime, timedelta

import git


def get_last_commit_at_date(repo_path: str, date: str) -> str | None:
    """Return the commit hash of the last commit made to a repository at a
    specific date.

    If no commits were made, return None.
    """
    repo = git.Repo(repo_path)
    commits = repo.iter_commits(
        since=datetime.fromisoformat(date) - timedelta(days=1),
        until=datetime.fromisoformat(date),
    )
    for commit in commits:
        return commit.hexsha


def get_latest_commit_at_date(repo_path: str, date: str) -> str | None:
    """Return the latest commit hash of the repository at a specific date.

    If no commits were made, return None.
    """
    repo = git.Repo(repo_path)
    commits = repo.iter_commits(
        until=datetime.fromisoformat(date),
    )
    for commit in commits:
        return commit.hexsha


def get_repo_revs_at_date(date: str) -> dict:
    """Return a dictionary mapping repository names to their respective commit
    hashes at a specific date.
    """
    repos = os.listdir("./repos")
    commits = {}
    latest_main = {}
    for repo in repos:
        repo_path = os.path.join("./repos", repo)
        rev = get_last_commit_at_date(repo_path, date)
        if rev is not None:
            commits[repo] = rev
        else:
            latest_rev = get_latest_commit_at_date(repo_path, date)
            if latest_rev is not None:
                latest_main[repo] = latest_rev
    return {"updated": commits, "static": latest_main}


def main():
    parser = argparse.ArgumentParser(
        description="Run a CliMA performance benchmark for a given date."
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="The date (YYYY-MM-DD) for which to run the benchmark.",
    )
    args = parser.parse_args()
    date = args.date
    repo_revs = get_repo_revs_at_date(date)
    print(f"Running benchmark for date: {date}")
    print("Repository revisions:")
    for repo, rev in repo_revs["updated"].items():
        print(f"  {repo}: {rev} (updated)")
    for repo, rev in repo_revs["static"].items():
        print(f"  {repo}: {rev} (static)")


if __name__ == "__main__":
    main()
