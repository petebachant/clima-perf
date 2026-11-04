"""Compare external GitHub activity after each blog post to its prior rate.

For every post that names a tracked package in its title, counts the
package's external GitHub events (stars, forks, opened issues, merged PRs,
from results/github-engagement.csv) in the 28 days from the post date, and
compares that to the count its rate over the 90 days before the post
predicts. Windows still in progress are compared over the days observed so
far. This is a before/after comparison per package, not a controlled one:
releases, conferences, and other posts in the same weeks move the same
counts.

Writes:

- ``results/blog-post-engagement.csv``: one row per (post, featured package)
- ``figures/blog-post-engagement.json``: a dumbbell chart of expected vs.
  actual events per post
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from clima_repos import CLIMA_REPOS

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "data" / "blog" / "posts.jsonl"
EVENTS = ROOT / "results" / "github-engagement.csv"
REPO_STATS = ROOT / "data" / "github" / "repo_stats"

AFTER_DAYS = 28
BASELINE_DAYS = 90
# Packages whose expected count tops this get their own panel and scale, so
# Oceananigans doesn't flatten everything else against zero.
HIGH_ACTIVITY = 20

EVENT_TYPES = ["star", "fork", "issue", "pr_merged"]

# Two shades of one hue (validated as an ordinal pair on the light surface)
EXPECTED_COLOR = "#86b6ef"
ACTUAL_COLOR = "#1c5cab"
CONNECTOR_COLOR = "#c3c2b7"


def data_through() -> pd.Timestamp:
    """The last UTC day the GitHub data covers (newest repo-stats snapshot)."""
    newest = sorted(REPO_STATS.glob("*.jsonl"))[-1]
    return pd.Timestamp(newest.stem)


def post_rows(events: pd.DataFrame, through: pd.Timestamp) -> pd.DataFrame:
    tracked = set(CLIMA_REPOS)
    rows = []
    for line in POSTS.read_text().splitlines():
        post = json.loads(line)
        day = pd.Timestamp(post["date"])
        observed = min(AFTER_DAYS, (through - day).days + 1)
        if observed < 1:
            continue
        for pkg in post["featured_packages"]:
            if pkg not in tracked:
                continue
            ev = events[events["repo"] == pkg]
            base = ev[
                (ev["day"] >= day - pd.Timedelta(days=BASELINE_DAYS))
                & (ev["day"] < day)
            ]
            after = ev[
                (ev["day"] >= day)
                & (ev["day"] < day + pd.Timedelta(days=observed))
            ]
            row = {
                "post_date": post["date"],
                "package": pkg,
                "title": post["title"],
                "link": post["link"],
                "days_observed": observed,
                "baseline_events": len(base),
                "expected_events": round(
                    len(base) * observed / BASELINE_DAYS, 1
                ),
                "actual_events": len(after),
            }
            for t in EVENT_TYPES:
                row[f"actual_{t}"] = int((after["event_type"] == t).sum())
            row["excess_events"] = round(
                row["actual_events"] - row["expected_events"], 1
            )
            rows.append(row)
    return pd.DataFrame(rows)


def make_figure(df: pd.DataFrame, through: pd.Timestamp) -> go.Figure:
    df = df.sort_values(["post_date", "package"], ascending=False)
    df = df.assign(
        label=[
            f"{pd.Timestamp(d):%b %d, %Y} · {p}"
            + (f" ({n} of {AFTER_DAYS} days)" if n < AFTER_DAYS else "")
            for d, p, n in zip(
                df["post_date"], df["package"], df["days_observed"]
            )
        ]
    )
    high = df["expected_events"].combine(df["actual_events"], max)
    panels = [df[high > HIGH_ACTIVITY], df[high <= HIGH_ACTIVITY]]
    panels = [p for p in panels if not p.empty]
    fig = make_subplots(
        rows=len(panels),
        cols=1,
        row_heights=[len(p) for p in panels],
        vertical_spacing=0.12 if len(panels) > 1 else 0,
    )
    for i, panel in enumerate(panels, start=1):
        # Connectors first so the dots sit on top
        for _, r in panel.iterrows():
            fig.add_scatter(
                x=[r["expected_events"], r["actual_events"]],
                y=[r["label"], r["label"]],
                mode="lines",
                line=dict(color=CONNECTOR_COLOR, width=2),
                hoverinfo="skip",
                showlegend=False,
                row=i,
                col=1,
            )
        hover = [
            f"<b>{r['package']}</b>, posted {r['post_date']}<br>"
            f"{r['title']}<br>"
            f"Expected from prior {BASELINE_DAYS} days: "
            f"{r['expected_events']}<br>"
            f"Actual in {r['days_observed']} days: {r['actual_events']} "
            f"({r['actual_star']} stars, {r['actual_fork']} forks, "
            f"{r['actual_issue']} issues, {r['actual_pr_merged']} PRs)"
            for _, r in panel.iterrows()
        ]
        for col, name, color in (
            ("expected_events", "Expected from prior rate", EXPECTED_COLOR),
            ("actual_events", "Actual after post", ACTUAL_COLOR),
        ):
            fig.add_scatter(
                x=panel[col],
                y=panel["label"],
                mode="markers",
                name=name,
                marker=dict(
                    color=color, size=11, line=dict(color="#fcfcfb", width=2)
                ),
                hovertext=hover,
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=i == 1,
                legendgroup=name,
                row=i,
                col=1,
            )
        # Pad both ends so dots at zero aren't clipped by the axis edge
        top = max(panel["expected_events"].max(), panel["actual_events"].max())
        pad = max(0.3, 0.04 * top)
        fig.update_xaxes(
            title_text=(
                f"External GitHub events in the {AFTER_DAYS} days from the post"
                if i == len(panels)
                else None
            ),
            range=[-pad, top + pad],
            row=i,
            col=1,
        )
    day = f"{through:%b} {through.day}, {through.year}"
    fig.update_layout(
        title="External GitHub activity after each blog post"
        f"<br><sup>Stars, forks, issues, and merged PRs vs. the count the "
        f"prior {BASELINE_DAYS}-day rate predicts; data through {day}</sup>",
        height=140 + 34 * len(df) + 60 * len(panels),
        margin=dict(t=90, b=50, l=20, r=20),
        legend=dict(
            orientation="h", y=1.0, yanchor="bottom", x=1, xanchor="right"
        ),
        hovermode="closest",
    )
    return fig


def main() -> None:
    through = data_through()
    events = pd.read_csv(EVENTS)
    events["repo"] = events["repo"].str.removesuffix(".jl")
    events["day"] = (
        pd.to_datetime(events["event_at"], utc=True, format="ISO8601")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    df = post_rows(events, through)
    out = ROOT / "results" / "blog-post-engagement.csv"
    df.sort_values(["post_date", "package"]).to_csv(out, index=False)
    make_figure(df, through).write_json(
        ROOT / "figures" / "blog-post-engagement.json"
    )
    series = df[df["post_date"] >= "2026-07-28"]
    print(
        f"{len(df)} post-package pairs; since the series began: "
        f"{series['actual_events'].sum()} actual vs. "
        f"{series['expected_events'].sum():.1f} expected events"
    )


if __name__ == "__main__":
    main()
