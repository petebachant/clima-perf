"""Outside pull and internal effort for each job to be done.

A job to be done is a user problem CliMA's packages and papers solve, e.g.,
calibrating a model against data. config/jobs.yaml maps every tracked repo
and every CliMA paper to one job, so pull can be compared across problems
rather than repos: a job may be served by several packages, or so far by
papers alone.

Outside accounts are followed by login through a funnel, each counted once
per job at the deepest stage it reached:

1. Watcher: starred or forked a repo
2. Asker: opened an issue or a discussion
3. User: has a public repo listing one of the job's packages as a direct
   dependency (from analyze-adopters' external-adopters.csv, which already
   leaves out CliMA's own, affiliated, and copied repos)
4. Contributor: had a pull request merged

Stars, forks, issues, discussions, and contributions count over the last
two years of data; users are the current snapshot. The stages are linked by
login, so a lab whose repos sit under an organization account counts apart
from its members' personal stars, and the funnel understates conversion.
Page views and installs can't be linked to accounts, so they aren't stages.

Internal effort is the number of pull requests by CliMA members merged into
the job's repos over the same two years. Outside citations are citing works
by authors outside CliMA, published in that window, of the job's papers. OpenAlex indexes new works with a lag, which
undercounts the latest months for every job alike, so the change in
citations over the last year compares jobs better than it measures any
one job's trend.

Writes:

- ``results/jobs.csv``: one row per job
- ``results/jobs-summary.json``: headline values for the jobs question
- ``figures/job-funnel.json``: outside accounts per job by deepest stage
- ``figures/job-scorecard.json``: effort, users, contributors, and
  citations per job, side by side
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import yaml
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from clima_repos import CLIMA_REPOS  # noqa: E402

CONFIG = ROOT / "config" / "jobs.yaml"
ENGAGEMENT = ROOT / "results" / "github-engagement.csv"
ADOPTERS = ROOT / "results" / "external-adopters.csv"
CITATIONS = ROOT / "results" / "external-citations.csv"
CLIMA_PUBS = ROOT / "data" / "openalex" / "clima-pubs.jsonl"
PRS = ROOT / "data" / "github" / "prs"
MEMBERS = ROOT / "data" / "github" / "clima_members.json"

WINDOW_DAYS = 730
MIN_PRIOR_CITATIONS = 10

# Ordinal blue ramp, validated with --ordinal: deeper stages are darker
STAGES = {
    "Watcher": "#86b6ef",
    "Asker": "#3987e5",
    "User": "#1c5cab",
    "Contributor": "#0d366b",
}
STAGE_OF_EVENT = {
    "star": "Watcher",
    "fork": "Watcher",
    "issue": "Asker",
    "discussion": "Asker",
    "pr_merged": "Contributor",
}
EFFORT_COLOR = "#7a7974"
PULL_COLOR = "#2a78d6"
INK = "#52514e"
SURFACE = "#fcfcfb"


def short_id(openalex_id: str) -> str:
    return openalex_id.rsplit("/", 1)[-1]


def load_config() -> tuple[dict, dict[str, str], dict[str, str]]:
    """The jobs, plus repo -> job and paper -> job (or not-job kind)."""
    config = yaml.safe_load(CONFIG.read_text())
    jobs = config["jobs"]
    job_of_repo: dict[str, str] = {}
    job_of_paper: dict[str, str] = {}
    problems = []
    for key, job in jobs.items():
        for pkg in job.get("packages") or []:
            if pkg in job_of_repo:
                problems.append(f"{pkg} is in {job_of_repo[pkg]} and {key}")
            job_of_repo[pkg] = key
        for paper in job.get("papers") or []:
            if paper in job_of_paper:
                problems.append(
                    f"{paper} is in {job_of_paper[paper]} and {key}"
                )
            job_of_paper[paper] = key
    for kind, papers in (config.get("not_jobs") or {}).items():
        for paper in papers or []:
            if paper in job_of_paper:
                problems.append(
                    f"{paper} is in {job_of_paper[paper]} and {kind}"
                )
            job_of_paper[paper] = kind
    missing = [r for r in CLIMA_REPOS if r not in job_of_repo]
    if missing:
        problems.append(f"repos missing from {CONFIG.name}: {missing}")
    unknown = [r for r in job_of_repo if r not in CLIMA_REPOS]
    if unknown:
        problems.append(f"untracked repos in {CONFIG.name}: {unknown}")
    if problems:
        raise SystemExit("\n".join(problems))

    pubs = {
        short_id(json.loads(line)["openalex_id"]): json.loads(line)
        for line in CLIMA_PUBS.read_text().splitlines()
        if line.strip()
    }
    for paper in sorted(set(pubs) - set(job_of_paper)):
        title = pubs[paper]["publications_page_title"]
        print(f"WARN paper not in {CONFIG.name}: {paper} {title}")
    for paper in sorted(set(job_of_paper) - set(pubs)):
        print(f"WARN {CONFIG.name} paper not among CliMA papers: {paper}")
    return jobs, job_of_repo, job_of_paper


def internal_prs(job_of_repo: dict[str, str], start, end) -> pd.Series:
    """PRs by CliMA members merged into each job's repos in the window."""
    members = set(json.loads(MEMBERS.read_text())["members"]) | {"CliMA"}
    canonical = {r.lower(): r for r in CLIMA_REPOS}
    seen = set()
    counts: dict[str, int] = {}
    for repo_dir in PRS.iterdir():
        repo = canonical.get(repo_dir.name.lower())
        if repo is None:
            continue
        for f in repo_dir.glob("*.json"):
            for pr in json.loads(f.read_text()):
                author = pr.get("author") or {}
                merged = pr.get("mergedAt")
                if (
                    not merged
                    or author.get("is_bot")
                    or author.get("login") not in members
                    or (repo, pr["number"]) in seen
                ):
                    continue
                seen.add((repo, pr["number"]))
                if start <= pd.Timestamp(merged) <= end:
                    job = job_of_repo[repo]
                    counts[job] = counts.get(job, 0) + 1
    return pd.Series(counts, dtype=int)


def deepest_stages(events: pd.DataFrame, users: pd.DataFrame) -> pd.DataFrame:
    """One row per (job, login) at the deepest stage reached."""
    rank = {s: i for i, s in enumerate(STAGES)}
    rows = pd.concat(
        [
            events.assign(stage=events["event_type"].map(STAGE_OF_EVENT))[
                ["job", "login", "stage"]
            ],
            users.assign(stage="User")[["job", "login", "stage"]],
        ]
    )
    rows["rank"] = rows["stage"].map(rank)
    deepest = rows.sort_values("rank").groupby(["job", "login"]).tail(1)
    return deepest.drop(columns="rank")


def citation_counts(
    cit: pd.DataFrame, job_of_paper: dict[str, str], through
) -> pd.DataFrame:
    cit = cit.assign(
        job=cit["cited_openalex_id"].map(short_id).map(job_of_paper),
        published=pd.to_datetime(cit["publication_date"], utc=True),
    )
    unmapped = cit["job"].isna().sum()
    if unmapped:
        print(f"WARN {unmapped} outside citations of unmapped papers")
    year = pd.Timedelta(days=365)
    window = cit[cit["published"] > through - pd.Timedelta(days=WINDOW_DAYS)]
    last = cit[cit["published"] > through - year]
    prior = cit[
        (cit["published"] > through - 2 * year)
        & (cit["published"] <= through - year)
    ]
    return pd.DataFrame(
        {
            "citations_2y": window.groupby("job").size(),
            "citations_all": cit.groupby("job").size(),
            "citations_last_year": last.groupby("job").size(),
            "citations_prior_year": prior.groupby("job").size(),
        }
    )


def funnel_figure(table: pd.DataFrame, through) -> go.Figure:
    # A job with no package has no repo to star or depend on, so no funnel
    paper_only = list(table.loc[table["n_packages"] == 0, "name"])
    table = table[table["n_packages"] > 0]
    order = table.index[::-1]
    names = table.loc[order, "name"]
    fig = go.Figure()
    for stage, color in STAGES.items():
        col = stage.lower() + "s"
        fig.add_bar(
            y=names,
            x=table.loc[order, col],
            name=stage,
            orientation="h",
            marker=dict(color=color, line=dict(color=SURFACE, width=1)),
            hovertemplate="%{y}: %{x} " + col + "<extra></extra>",
        )
    totals = table.loc[order, "outside_accounts"]
    deep = table.loc[order, "deep_pct"]
    fig.add_scatter(
        y=names,
        x=totals,
        mode="text",
        text=[
            f"  {n} ({p:.0f}% users or contributors)"
            for n, p in zip(totals, deep)
        ],
        textposition="middle right",
        textfont=dict(color=INK, size=11),
        hoverinfo="skip",
        showlegend=False,
    )
    fig.update_layout(
        barmode="stack",
        title="How far outside accounts go with each job"
        "<br><sup>Distinct GitHub accounts outside CliMA, each at the deepest "
        f"stage reached in the 2 years to {through:%Y-%m-%d}"
        + (
            f"<br>{' and '.join(paper_only)} have no CliMA package, so no "
            "funnel"
            if paper_only
            else ""
        )
        + "</sup>",
        height=240 + 34 * len(table),
        margin=dict(t=110, b=110, l=20, r=40),
        legend=dict(
            orientation="h",
            y=-0.14,
            yanchor="top",
            x=0,
            xanchor="left",
            traceorder="normal",
        ),
        bargap=0.3,
    )
    fig.update_xaxes(title_text="Accounts", range=[0, totals.max() * 1.45])
    return fig


def scorecard_figure(table: pd.DataFrame, through) -> go.Figure:
    order = table.index[::-1]
    names = table.loc[order, "name"]
    # (column, title, color, whether it needs a package to be nonzero)
    panels = [
        ("internal_prs_2y", "CliMA PRs merged", EFFORT_COLOR, True),
        ("users", "Outside users", PULL_COLOR, True),
        ("contributors", "Outside contributors", PULL_COLOR, True),
        ("citations_2y", "Outside citations", PULL_COLOR, False),
    ]
    no_pkg = table.loc[order, "n_packages"] == 0
    fig = make_subplots(
        rows=1,
        cols=len(panels),
        shared_yaxes=True,
        horizontal_spacing=0.03,
        subplot_titles=[p[1] for p in panels],
    )
    for i, (col, title, color, needs_pkg) in enumerate(panels, start=1):
        values = table.loc[order, col]
        text = [
            " no package" if needs_pkg and none else f" {v}"
            for v, none in zip(values, no_pkg)
        ]
        fig.add_bar(
            y=names,
            x=values,
            orientation="h",
            marker=dict(color=color),
            text=text,
            textposition="outside",
            textfont=dict(color=INK, size=11),
            cliponaxis=False,
            hovertemplate="%{y}: %{x} " + title.lower() + "<extra></extra>",
            showlegend=False,
            row=1,
            col=i,
        )
        fig.update_xaxes(
            range=[0, max(values.max(), 1) * 1.35],
            showticklabels=False,
            showgrid=False,
            row=1,
            col=i,
        )
    fig.update_layout(
        title="Effort and outside pull by job to be done"
        "<br><sup>PRs, contributors, and citations over the 2 years to "
        f"{through:%Y-%m-%d}; users are outside accounts with a public repo"
        "<br>depending on the job's packages now</sup>",
        height=200 + 34 * len(table),
        margin=dict(t=125, b=30, l=20, r=20),
        bargap=0.3,
    )
    fig.update_annotations(font=dict(size=12, color=INK))
    return fig


def main() -> None:
    jobs, job_of_repo, job_of_paper = load_config()

    eng = pd.read_csv(ENGAGEMENT)
    eng["event_at"] = pd.to_datetime(
        eng["event_at"], utc=True, format="ISO8601"
    )
    through = eng["event_at"].max()
    start = through - pd.Timedelta(days=WINDOW_DAYS)
    eng["job"] = eng["repo"].map(job_of_repo)
    events = eng[(eng["event_at"] > start) & eng["job"].notna()]

    adopters = pd.read_csv(ADOPTERS)
    adopters = adopters.assign(job=adopters["package"].map(job_of_repo))
    adopters = adopters[adopters["job"].notna()]
    users = adopters.groupby(["job", "login"], as_index=False)["active"].any()

    deepest = deepest_stages(events, users)
    stage_counts = (
        deepest.groupby(["job", "stage"]).size().unstack(fill_value=0)
    ).reindex(columns=list(STAGES), fill_value=0)
    stage_counts.columns = [s.lower() + "s" for s in stage_counts.columns]

    table = pd.DataFrame(index=list(jobs))
    table["name"] = [jobs[k]["name"] for k in table.index]
    table["n_packages"] = [
        len(jobs[k].get("packages") or []) for k in table.index
    ]
    table["n_papers"] = [len(jobs[k].get("papers") or []) for k in table.index]
    table = table.join(stage_counts)
    table["users"] = users.groupby("job").size()
    table["active_users"] = users[users["active"]].groupby("job").size()
    stars = events[events["event_type"] == "star"]
    table["stargazers"] = stars.groupby("job")["login"].nunique()
    table["internal_prs_2y"] = internal_prs(job_of_repo, start, through)

    cit = pd.read_csv(CITATIONS)
    cites = citation_counts(cit, job_of_paper, through)
    table = table.join(cites)
    table = table.fillna(0)
    count_cols = [c for c in table.columns if c != "name"]
    table[count_cols] = table[count_cols].astype(int)

    table["outside_accounts"] = table[[s.lower() + "s" for s in STAGES]].sum(
        axis=1
    )
    # Users and contributors are the stages that show the job got done
    table["deep"] = table["users"] + table["contributors"]
    table["deep_pct"] = (
        100
        * table["deep"]
        / table["outside_accounts"].where(table["outside_accounts"] > 0)
    ).round(1)
    total_prs = table["internal_prs_2y"].sum()
    table["internal_pr_pct"] = (
        100 * table["internal_prs_2y"] / total_prs
    ).round(1)
    table["users_per_100_prs"] = (
        100
        * table["users"]
        / table["internal_prs_2y"].where(table["internal_prs_2y"] > 0)
    ).round(1)
    table["citation_change_pct"] = (
        100
        * (
            table["citations_last_year"]
            / table["citations_prior_year"].where(
                table["citations_prior_year"] > 0
            )
            - 1
        )
    ).round(0)
    table = table.sort_values(["deep", "outside_accounts"], ascending=False)

    out = table.drop(columns="deep").rename_axis("job").reset_index()
    out.to_csv(ROOT / "results" / "jobs.csv", index=False)

    other_cites = cites.reindex(["vision", "finding"]).fillna(0).astype(int)
    summary = summarize(table, other_cites, through)
    (ROOT / "results" / "jobs-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )

    funnel_figure(table, through).write_json(
        ROOT / "figures" / "job-funnel.json"
    )
    scorecard_figure(table, through).write_json(
        ROOT / "figures" / "job-scorecard.json"
    )
    print(
        out[
            [
                "job",
                "outside_accounts",
                "watchers",
                "askers",
                "users",
                "contributors",
                "internal_prs_2y",
                "users_per_100_prs",
                "citations_2y",
                "citation_change_pct",
            ]
        ].to_string(index=False)
    )


def summarize(table: pd.DataFrame, other_cites: pd.DataFrame, through) -> dict:
    """Flat values for the jobs question's answer."""
    s: dict = {"through": f"{through:%Y-%m-%d}"}
    for key, row in table.iterrows():
        k = key.replace("-", "_")
        for col in [
            "outside_accounts",
            "users",
            "active_users",
            "contributors",
            "internal_prs_2y",
            "internal_pr_pct",
            "users_per_100_prs",
            "citations_2y",
            "citation_change_pct",
            "deep_pct",
        ]:
            v = row[col]
            s[f"{k}_{col}"] = (
                None
                if pd.isna(v)
                else (float(v) if isinstance(v, float) else int(v))
            )
    top = table.index[0]
    rest = table.drop(index=top)
    s["top_job"] = table.loc[top, "name"]
    s["top_job_deep_share_pct"] = round(
        100 * table.loc[top, "deep"] / table["deep"].sum()
    )
    runner_up = rest.index[0]
    s["runner_up_job"] = table.loc[runner_up, "name"]
    s["runner_up_deep"] = int(table.loc[runner_up, "deep"])
    with_effort = rest[rest["internal_prs_2y"] > 0]
    best = with_effort["users_per_100_prs"].idxmax()
    s["best_ratio_job"] = table.loc[best, "name"]
    s["best_ratio"] = float(table.loc[best, "users_per_100_prs"])
    s["top_ratio"] = float(table.loc[top, "users_per_100_prs"])
    most_cited = table["citations_2y"].idxmax()
    s["most_cited_job"] = table.loc[most_cited, "name"]
    s["most_cited_n"] = int(table.loc[most_cited, "citations_2y"])
    # Growth is noisy on few citations, so only jobs cited at least
    # MIN_PRIOR_CITATIONS times in the prior year compete
    growing = table[table["citations_prior_year"] >= MIN_PRIOR_CITATIONS]
    k = growing["citation_change_pct"].idxmax()
    s["fastest_cited_job"] = table.loc[k, "name"]
    s["fastest_cited_pct"] = int(table.loc[k, "citation_change_pct"])
    s["fastest_cited_last_year"] = int(table.loc[k, "citations_last_year"])
    s["fastest_cited_prior_year"] = int(table.loc[k, "citations_prior_year"])
    s["fastest_cited_has_package"] = bool(table.loc[k, "n_packages"] > 0)
    esm = table.loc[["atmosphere", "coupled-esm"]]
    s["esm_pr_pct"] = round(float(esm["internal_pr_pct"].sum()))
    s["esm_users"] = int(esm["users"].sum())
    s["esm_contributors"] = int(esm["contributors"].sum())
    s["vision_citations_2y"] = int(other_cites.loc["vision", "citations_2y"])
    s["finding_citations_2y"] = int(other_cites.loc["finding", "citations_2y"])
    return s


if __name__ == "__main__":
    main()
