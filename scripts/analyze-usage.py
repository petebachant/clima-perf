"""Chart repo traffic, Julia installs, and use of CliMA packages in repos.

Reads the snapshots the fetch stages accumulate and writes five figures:

- ``figures/repo-traffic.json``: daily GitHub views (stacked by repo) and
  unique visitors (lines) with blog post dates marked
- ``figures/repo-referrers.json``: where visitors came from over the newest
  14-day window
- ``figures/julia-installs.json``: average daily unique installs per CliMA
  package, user vs. CI
- ``figures/julia-installs-peers.json``: Oceananigans' daily user installs
  against Julia peers
- ``figures/repo-usage.json``: outside repos that list a CliMA package as a
  direct dependency, by the year the repo was created and whether it's
  still active

Unique counts (visitors, IP addresses) are per repo or package and per day,
so they can't be added up without counting the same people more than once;
the charts show them per repo or package rather than as totals.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Categorical palette in fixed order (validated for adjacent pairs); Other
# is a neutral gray, not a slot. The first three slots also validate for
# all pairs, so line charts use at most three.
PALETTE = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
]
OTHER = "#a8a7a1"
SURFACE = "#fcfcfb"
INK_MUTED = "#52514e"
TOP_REPOS = 7
ACTIVE_DAYS = 180


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def blog_posts(start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """Package-series posts dated inside the window, newest first."""
    posts = []
    for p in read_jsonl(DATA / "blog" / "posts.jsonl"):
        day = pd.Timestamp(p["date"])
        if start <= day <= end and p["featured_packages"]:
            posts.append(
                {"day": day, "label": ", ".join(p["featured_packages"])}
            )
    return posts


def mark_posts(fig: go.Figure, posts: list[dict], rows: list[int]) -> None:
    for p in posts:
        for row in rows:
            fig.add_vline(
                x=p["day"],
                line=dict(color=INK_MUTED, width=1),
                row=row,
                col=1,
            )
        # Inside the top panel, so it doesn't collide with its title
        fig.add_annotation(
            x=p["day"],
            y=0.98,
            yref="y domain",
            text=f" Blog: {p['label']}",
            showarrow=False,
            xanchor="left",
            yanchor="top",
            font=dict(size=11, color=INK_MUTED),
        )


def traffic_figure() -> go.Figure:
    views = pd.read_csv(DATA / "github-traffic" / "views.csv")
    views["date"] = pd.to_datetime(views["date"])
    views["repo"] = views["repo"].str.removesuffix(".jl")
    totals = views.groupby("repo")["count"].sum().sort_values(ascending=False)
    top = list(totals.index[:TOP_REPOS])
    days = pd.date_range(views["date"].min(), views["date"].max())

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            "Page views per day, by repo",
            "Unique visitors per day",
        ),
    )
    stacked = views.assign(
        group=views["repo"].where(views["repo"].isin(top), "Other")
    )
    pivot = (
        stacked.pivot_table(
            index="date", columns="group", values="count", aggfunc="sum"
        )
        .reindex(days, fill_value=0)
        .fillna(0)
    )
    for i, repo in enumerate(top + ["Other"]):
        if repo not in pivot:
            continue
        fig.add_bar(
            x=days,
            y=pivot[repo],
            name=repo,
            legendgroup=repo,
            marker=dict(
                color=PALETTE[i] if repo != "Other" else OTHER,
                line=dict(color=SURFACE, width=1),
            ),
            hovertemplate="%{x|%b %d}: %{y} views<extra>" + repo + "</extra>",
            row=1,
            col=1,
        )

    uniq = views.pivot_table(
        index="date", columns="repo", values="uniques", aggfunc="sum"
    ).reindex(days, fill_value=0)
    for i, repo in enumerate(top[:3]):
        fig.add_scatter(
            x=days,
            y=uniq[repo],
            name=repo,
            legendgroup=repo,
            showlegend=False,
            mode="lines+markers",
            line=dict(color=PALETTE[i], width=2),
            marker=dict(size=8, line=dict(color=SURFACE, width=2)),
            hovertemplate="%{x|%b %d}: %{y} unique visitors<extra>"
            + repo
            + "</extra>",
            row=2,
            col=1,
        )
    rest = uniq.drop(columns=top[:3]).sum(axis=1)
    fig.add_scatter(
        x=days,
        y=rest,
        name="Other repos (sum)",
        mode="lines",
        line=dict(color=OTHER, width=2),
        hovertemplate="%{x|%b %d}: %{y} unique visitors, summed across "
        "the other repos<extra></extra>",
        row=2,
        col=1,
    )
    mark_posts(fig, blog_posts(days[0], days[-1]), rows=[1, 2])
    fig.update_layout(
        barmode="stack",
        bargap=0.15,
        title="GitHub traffic to CliMA repos<br><sup>Views stack by repo; "
        "unique visitors are per repo and per day, so the gray line "
        "counts someone visiting several other repos more than once</sup>",
        height=720,
        margin=dict(t=110, b=40, l=60, r=20),
        hovermode="closest",
    )
    fig.update_yaxes(title_text="Views", row=1, col=1)
    fig.update_yaxes(title_text="Unique visitors", row=2, col=1)
    return fig


def referrers_figure() -> go.Figure:
    refs = pd.read_csv(DATA / "github-traffic" / "referrers.csv")
    latest = refs[refs["fetched_on"] == refs["fetched_on"].max()]
    by_ref = latest.groupby("referrer")["uniques"].sum().sort_values().tail(12)
    fig = go.Figure()
    fig.add_bar(
        y=by_ref.index,
        x=by_ref.values,
        orientation="h",
        marker=dict(color=PALETTE[0], line=dict(color=SURFACE, width=1)),
        text=[f"  {v}" for v in by_ref.values],
        textposition="outside",
        textfont=dict(color=INK_MUTED),
        hovertemplate="%{y}: %{x} unique visitors<extra></extra>",
    )
    fetched = pd.Timestamp(latest["fetched_on"].iloc[0])
    start = fetched - pd.Timedelta(days=14)
    fig.update_layout(
        title="Where visitors to CliMA repos come from<br><sup>Unique "
        f"visitors summed over repos, {start:%b %d} to {fetched:%b %d}, "
        f"{fetched.year}; GitHub reports only each repo's top 10</sup>",
        height=140 + 28 * len(by_ref),
        margin=dict(t=90, b=40, l=20, r=40),
        showlegend=False,
    )
    fig.update_xaxes(
        title_text="Unique visitors", range=[0, by_ref.max() * 1.15]
    )
    return fig


def installs_figure() -> go.Figure:
    dl = pd.read_csv(DATA / "julia-downloads" / "by-date.csv")
    with (DATA / "julia-downloads" / "packages.csv").open() as f:
        groups = {r["package"]: r["group"] for r in csv.DictReader(f)}
    dl["date"] = pd.to_datetime(dl["date"])
    dl["client_type"] = dl["client_type"].fillna("unknown")
    end = dl["date"].max()
    window = dl[dl["date"] > end - pd.Timedelta(days=28)]
    n_days = window["date"].nunique()
    # Unique IPs per package per day, summed over the two kept statuses
    per_day = (
        window.groupby(["package", "client_type"])["request_addrs"].sum()
        / n_days
    ).unstack(fill_value=0)
    clima = per_day[[groups.get(p) == "clima" for p in per_day.index]]
    # Ranked by user installs, the human signal
    clima = clima.loc[clima.get("user", 0).sort_values().index]
    clima = clima[clima.sum(axis=1) >= 1]

    fig = go.Figure()
    for name, col, color in (
        ("User", "user", PALETTE[0]),
        ("CI", "ci", OTHER),
    ):
        fig.add_bar(
            y=clima.index,
            x=clima.get(col, 0),
            name=name,
            orientation="h",
            marker=dict(color=color, line=dict(color=SURFACE, width=1)),
            hovertemplate="%{y}: %{x:.1f} " + name.lower() + " installs/day"
            "<extra></extra>",
        )
    fig.update_layout(
        barmode="stack",
        title=f"Julia installs of CliMA packages, last {n_days} days"
        "<br><sup>Average unique IP addresses per day from the Julia package "
        "servers. Most CI installs are CliMA's own; packages pulled in by<br>"
        "others (e.g., CubedSphere by Oceananigans) count too</sup>",
        height=160 + 22 * len(clima),
        margin=dict(t=150, b=40, l=20, r=20),
        legend=dict(
            orientation="h", y=1.0, yanchor="bottom", x=0, traceorder="normal"
        ),
    )
    fig.update_xaxes(title_text="Installs per day")
    return fig


def peer_installs_figure() -> go.Figure:
    dl = pd.read_csv(DATA / "julia-downloads" / "by-date.csv")
    dl["date"] = pd.to_datetime(dl["date"])
    user = dl[dl["client_type"] == "user"]
    daily = user.pivot_table(
        index="date", columns="package", values="request_addrs", aggfunc="sum"
    )
    fig = go.Figure()
    for i, pkg in enumerate(["Oceananigans", "SpeedyWeather", "Trixi"]):
        if pkg not in daily:
            continue
        fig.add_scatter(
            x=daily.index,
            y=daily[pkg],
            name=pkg,
            mode="lines",
            line=dict(color=PALETTE[i], width=2),
            hovertemplate="%{x|%b %d}: %{y} user installs<extra>"
            + pkg
            + "</extra>",
        )
    fig.update_layout(
        title="Oceananigans user installs next to Julia peers"
        "<br><sup>Unique IP addresses per day from user (non-CI) clients; "
        "SpeedyWeather is a Julia atmosphere model and Trixi a Julia PDE "
        "framework</sup>",
        height=440,
        margin=dict(t=100, b=40, l=60, r=20),
        legend=dict(orientation="h", y=1.0, yanchor="bottom", x=0),
    )
    fig.update_yaxes(title_text="User installs per day", rangemode="tozero")
    return fig


def usage_figure() -> go.Figure:
    adopters = pd.read_csv(ROOT / "results" / "external-adopters.csv")
    counted = adopters[
        ~adopters["segment"].isin(["Internal", "Affiliated", "Copy"])
    ]
    repo_names = {r for s in counted["repos"] for r in s.split()}
    meta = {
        r["full_name"]: r
        for r in read_jsonl(DATA / "github-code-dependents" / "repos.jsonl")
    }
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=ACTIVE_DAYS)
    rows = []
    for name in repo_names:
        m = meta.get(name)
        if not m or not m.get("created_at"):
            continue  # e.g., registered packages found only via the registry
        pushed = pd.to_datetime(m.get("pushed_at"), utc=True)
        rows.append(
            {
                "repo": name,
                "year": pd.Timestamp(m["created_at"]).year,
                "active": bool(pd.notna(pushed) and pushed >= cutoff),
            }
        )
    df = pd.DataFrame(rows)
    years = list(range(df["year"].min(), df["year"].max() + 1))
    counts = (
        df.groupby(["year", "active"]).size().unstack(fill_value=0)
    ).reindex(years, fill_value=0)
    fig = go.Figure()
    for active, name, color in (
        (True, f"Pushed in the last {ACTIVE_DAYS} days", PALETTE[0]),
        (False, "Not pushed since", OTHER),
    ):
        fig.add_bar(
            x=years,
            y=counts.get(active, 0),
            name=name,
            marker=dict(color=color, line=dict(color=SURFACE, width=1)),
            hovertemplate="%{x}: %{y} repos<extra>" + name + "</extra>",
        )
    total = counts.sum(axis=1)
    fig.add_scatter(
        x=years,
        y=total,
        mode="text",
        text=[str(n) if n else "" for n in total],
        textposition="top center",
        textfont=dict(color=INK_MUTED, size=11),
        hoverinfo="skip",
        showlegend=False,
    )
    n_active = int(df["active"].sum())
    fig.update_layout(
        barmode="stack",
        title="Outside repos that use CliMA packages, by year created"
        f"<br><sup>{len(df)} public repos outside CliMA ({n_active} still "
        "active) whose Project.toml lists a CliMA package; the year a repo "
        "was created is the earliest it could have started using one</sup>",
        height=480,
        margin=dict(t=100, b=40, l=60, r=20),
        legend=dict(
            orientation="h", y=1.0, yanchor="bottom", x=0, traceorder="normal"
        ),
    )
    this_year = pd.Timestamp.now().year
    fig.update_xaxes(
        title_text="Year the repo was created",
        tickvals=years,
        ticktext=[f"{y} so far" if y == this_year else str(y) for y in years],
    )
    fig.update_yaxes(title_text="Repos", range=[0, total.max() * 1.15])
    return fig


def main() -> None:
    out = ROOT / "figures"
    out.mkdir(exist_ok=True)
    traffic_figure().write_json(out / "repo-traffic.json")
    referrers_figure().write_json(out / "repo-referrers.json")
    installs_figure().write_json(out / "julia-installs.json")
    peer_installs_figure().write_json(out / "julia-installs-peers.json")
    usage_figure().write_json(out / "repo-usage.json")
    print("wrote 5 figures")


if __name__ == "__main__":
    main()
