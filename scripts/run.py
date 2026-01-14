"""Run a CliMA performance benchmark for a given date."""

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta

import git

REPOS = [
    "ClimaCoupler.jl",
    "ClimaAtmos.jl",
    "ClimaCore.jl",
    "ClimaTimesteppers.jl",
    "Thermodynamics.jl",
    "RRTMGP.jl",
]


def get_latest_commit_at_date(repo_path: str, date: str) -> dict | None:
    """Return the latest commit hash of the repository as of a specific date.

    If no commits were made, return None.

    Note that we include commits made on the given date by adding one day to
    the ``until`` parameter.
    """
    # Don't allow running for the current day, since that can cause
    # irreproducible results
    if datetime.fromisoformat(date).date() >= datetime.now().date():
        raise ValueError("Date must be in the past")
    repo = git.Repo(repo_path)
    repo.remotes.origin.fetch()
    commits = repo.iter_commits(
        "origin/main",
        until=datetime.fromisoformat(date) + timedelta(days=1),
    )
    for commit in commits:
        return {
            "rev": commit.hexsha,
            "timestamp": commit.committed_datetime.isoformat(),
        }


def get_repo_revs_at_date(date: str) -> dict:
    """Return a dictionary mapping repository names to their respective commit
    hashes at a specific date.
    """
    commits = {}
    for repo in REPOS:
        repo_path = os.path.join("./repos", repo)
        rev = get_latest_commit_at_date(repo_path, date)
        if rev is None:
            raise ValueError(
                f"No commits found for repository {repo} at date {date}"
            )
        commits[repo] = rev
    return commits


def run_julia_command(env_dir: str, command: str, check: bool = True):
    """Run a Julia command in a specific environment."""
    cmd = ["julia", "--project=" + env_dir, "-e", command]
    subprocess.run(cmd, check=check)


def copy_file_at_rev(repo_path: str, rev: str, src_path: str, dest_path: str):
    """Get the contents of a file at a specific Git revision."""
    repo = git.Repo(repo_path)
    file_contents = repo.git.show(f"{rev}:{src_path}")
    with open(dest_path, "w") as f:
        f.write(file_contents)


def copy_repo_at_rev(repo_path: str, rev: str, dest_path: str):
    """Copy entire repository at a specific revision to destination."""
    os.makedirs(dest_path, exist_ok=True)
    # Use git archive and extract in one go
    cmd = f"git -C {repo_path} archive {rev} | tar -x -C {dest_path}"
    subprocess.run(cmd, shell=True, check=True)


def log(*args):
    """Print log messages with flushing and a timestamp."""
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "-", *args, flush=True)


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
    parser.add_argument(
        "--env-only",
        action="store_true",
        default=False,
        help="Only set up the environment.",
    )
    args = parser.parse_args()
    date = args.date
    # Normalize date to YYYY-MM-DD format
    date = datetime.fromisoformat(date).strftime("%Y-%m-%d")
    repo_revs = get_repo_revs_at_date(date)
    log(f"Running benchmark for date: {date}")
    log("Git revs:")
    for repo, commit in repo_revs.items():
        rev = commit["rev"]
        log(f"  {repo}: {rev}")
    # Create Julia environment based on ClimaCoupler's ClimaEarth environment
    run_dir = os.path.join("runs", "amip", date)
    os.makedirs(run_dir, exist_ok=True)
    env_dir = os.path.join(
        run_dir, "ClimaCoupler.jl", "experiments", "ClimaEarth"
    )
    coupler_rev = repo_revs["ClimaCoupler.jl"]["rev"]
    log("Copying ClimaCoupler at rev:", coupler_rev)
    # Delete ClimaCoupler directory if it already exists
    coupler_dir = os.path.join(run_dir, "ClimaCoupler.jl")
    if os.path.exists(coupler_dir):
        log("Removing existing ClimaCoupler directory:", coupler_dir)
        shutil.rmtree(coupler_dir)
    copy_repo_at_rev(
        repo_path="./repos/ClimaCoupler.jl",
        rev=coupler_rev,
        dest_path=coupler_dir,
    )
    # Instantiate the environment
    log("Instantiating ClimaEarth environment at:", env_dir)
    run_julia_command(env_dir, "using Pkg; Pkg.instantiate();", check=True)
    # Add the rev for each package to the environment
    julia_cmd_template = (
        'using Pkg; Pkg.add(Pkg.PackageSpec(;url="{pkg_url}", rev="{rev}"))'
    )
    for repo in REPOS:
        if repo == "ClimaCoupler.jl":
            continue
        if repo not in repo_revs:
            raise ValueError(f"No revision found for repository {repo}")
        repo_url = f"https://github.com/CliMA/{repo}"
        rev = repo_revs[repo]["rev"]
        julia_cmd = julia_cmd_template.format(pkg_url=repo_url, rev=rev)
        log("Adding package:", repo, "at rev:", rev)
        run_julia_command(env_dir, julia_cmd)
    # Resolve the environment
    log("Resolving environment")
    run_julia_command(env_dir, "using Pkg; Pkg.resolve();")
    # Add MPI
    log("Adding MPI package")
    run_julia_command(env_dir, 'using Pkg; Pkg.add("MPI");')
    # Precompile and print the env status
    log("Precompiling packages")
    run_julia_command(env_dir, "using Pkg; Pkg.precompile();")
    run_julia_command(env_dir, "using Pkg; Pkg.status();")
    # Copy ClimaEarth manifest file back into run dir for record-keeping
    manifest_src = os.path.join(env_dir, "Manifest-v1.11.toml")
    manifest_dest = os.path.join(run_dir, "Manifest-v1.11.toml")
    shutil.copy2(manifest_src, manifest_dest)
    # Export detected Git revs to JSON file
    with open(os.path.join(run_dir, "repo-revs.json"), "w") as f:
        json.dump(repo_revs, f, indent=4)
    log("Environment setup complete")
    # If only setting up the environment, exit now
    if args.env_only:
        return
    # Copy in ClimaCoupler configs from the main repo
    log("Copying ClimaCoupler configs")
    configs = [
        "benchmark_configs/amip_progedmf_1m_land_he16.yml",
        "atmos_configs/climaatmos_progedmf_1m.yml",
    ]
    config_src_dir = "./repos/ClimaCoupler.jl/config"
    config_dest_dir = os.path.join(run_dir, "ClimaCoupler.jl", "config")
    for config in configs:
        src = os.path.join(config_src_dir, config)
        dest = os.path.join(config_dest_dir, config)
        log(f"  {src} -> {dest}")
        shutil.copy2(src, dest)
    # Copy in the TOML file
    log("Copying ClimaCoupler TOML file")
    toml_src = "./repos/ClimaCoupler.jl/toml/amip_progedmf_1m.toml"
    toml_dest = os.path.join(
        run_dir, "ClimaCoupler.jl", "toml", "amip_progedmf_1m.toml"
    )
    shutil.copy2(toml_src, toml_dest)
    # Now run
    log("Starting benchmark")
    config_dir = f"{run_dir}/ClimaCoupler.jl/config"
    cmd = [
        "julia",
        f"--project={env_dir}",
        f"{run_dir}/ClimaCoupler.jl/experiments/ClimaEarth/run_amip.jl",
        "--config_file",
        f"{config_dir}/benchmark_configs/amip_progedmf_1m_land_he16.yml",
        "--job_id",
        "gpu_amip_progedmf_1M_land_he16",
    ]
    subprocess.run(cmd, check=True)
    log("Benchmark complete")
    # Move artifacts into results directory
    output_dirs = ["output", "experiments/ClimaEarth/output"]
    artifact_dirs = [
        os.path.join(
            outdir,
            "gpu_amip_progedmf_1M_land_he16",
            "artifacts",
        )
        for outdir in output_dirs
    ]
    # Verify that only one artifacts directory exists
    artifacts_src = None
    for artifact_dir in artifact_dirs:
        if os.path.exists(artifact_dir):
            if artifacts_src is not None:
                log("Warning: Multiple artifacts directories found")
                return
            artifacts_src = artifact_dir
    if artifacts_src is None:
        log("Warning: No artifacts directory found")
        return
    artifacts_dest = os.path.join(run_dir, "artifacts")
    log(f"Moving artifacts from {artifacts_src} to run directory")
    shutil.move(artifacts_src, artifacts_dest)
    # Delete temporary copy of ClimaCoupler
    log("Removing temporary ClimaCoupler copy")
    shutil.rmtree(os.path.join(run_dir, "ClimaCoupler.jl"))


if __name__ == "__main__":
    main()
