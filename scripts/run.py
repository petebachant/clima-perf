"""Run a CliMA performance benchmark for a given date."""

import argparse
import os
import shutil
import subprocess
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


def run_julia_command(env_dir: str, command: str):
    """Run a Julia command in a specific environment."""
    cmd = ["julia", "--project=" + env_dir, "-e", command]
    subprocess.run(cmd, check=True)


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
    # Normalize date to YYYY-MM-DD format
    date = datetime.fromisoformat(date).strftime("%Y-%m-%d")
    repo_revs = get_repo_revs_at_date(date)
    print(f"Running benchmark for date: {date}")
    print("Repository revisions:")
    for repo, rev in repo_revs["updated"].items():
        print(f"  {repo}: {rev} (updated)")
    for repo, rev in repo_revs["static"].items():
        print(f"  {repo}: {rev} (static)")
    # Create Julia environment based on ClimaCoupler's ClimaEarth environment
    env_dir = os.path.join("envs", date)
    src_project = os.path.join(
        "repos", "ClimaCoupler.jl", "experiments", "ClimaEarth", "Project.toml"
    )
    os.makedirs(env_dir, exist_ok=True)
    dst_project = os.path.join(env_dir, "Project.toml")
    shutil.copy(src_project, dst_project)
    # Instantiate the environment
    run_julia_command(env_dir, "using Pkg; Pkg.instantiate();")
    # Add the rev for each package to the environment
    julia_cmd_template = (
        'using Pkg; Pkg.add(Pkg.PackageSpec(;name="{pkg_name}", rev="{rev}"))'
    )
    for repo, rev in repo_revs["updated"].items():
        pkg_name = repo.replace(".jl", "")
        julia_cmd = julia_cmd_template.format(pkg_name=pkg_name, rev=rev)
        run_julia_command(env_dir, julia_cmd)
    for repo, rev in repo_revs["static"].items():
        pkg_name = repo.replace(".jl", "")
        julia_cmd = julia_cmd_template.format(pkg_name=pkg_name, rev=rev)
        run_julia_command(env_dir, julia_cmd)
    # Resolve the environment
    run_julia_command(env_dir, "using Pkg; Pkg.resolve();")
    # Add MPI
    run_julia_command(env_dir, 'using Pkg; Pkg.add("MPI");')
    # Precompile and print the env status
    run_julia_command(env_dir, "using Pkg; Pkg.precompile();")
    run_julia_command(env_dir, "using Pkg; Pkg.status();")
    # Now run
    benchmark_config_path = "./repos/ClimaCoupler.jl/config/benchmark_configs"
    cmd = (
        f"julia --threads=3 --color=yes --project={env_dir} "
        "repos/ClimaCoupler.jl/experiments/ClimaEarth/run_amip.jl "
        "--config_file "
        f"{benchmark_config_path}/amip_progedmf_1m_land_he16.yml "
        "--job_id gpu_amip_progedmf_1M_land_he16"
    )
    subprocess.run(cmd, shell=True, check=True)


if __name__ == "__main__":
    main()
