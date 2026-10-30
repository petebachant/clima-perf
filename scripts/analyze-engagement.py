"""Count external engagement with CliMA's products over time.

Reads the OpenAlex citations, GitHub activity, and Julia-registry dependents
the fetch stages write, keeps only external events, and writes one event
table per stream plus quarterly (all-time), monthly (last 24 months),
and weekly GitHub (last 52 weeks) figures:

- ``results/external-citations.csv``
- ``results/github-engagement.csv``
- ``results/new-registry-dependents.csv``
- ``figures/external-engagement.json``
- ``figures/external-engagement-monthly.json``
- ``figures/github-engagement-weekly.json``
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.colors as pc
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path.cwd()
DATA = ROOT / "data"

# Hand-curated map: canonical paper DOI (lowercased) -> CliMA software package.
# Used to color the software-paper panel of the external-engagement figure.
# Papers whose DOI is not in this map (or whose entry on the publications
# page has no DOI at all) implicitly fall outside the software-stacked panel
# but are still counted in the "all citations" total.
PACKAGE_BY_DOI: dict[str, str] = {
    "10.21105/joss.02018": "Oceananigans",  # Ramadhan et al. 2020, JOSS
    "10.1029/2024ms004465": "Oceananigans",  # Silvestri et al. 2024, JAMES
    "10.48550/arxiv.2502.14148": "Oceananigans",  # Wagner et al. 2025, arXiv preprint
    "10.21105/joss.04869": "EnsembleKalmanProcesses",  # Dunbar et al. 2022, JOSS
    "10.21105/joss.06372": "CalibrateEmulateSample",  # Dunbar et al. 2024, JOSS
    "10.1029/2025ms005118": "ClimaLand",  # Deck et al. 2026, JAMES
    "10.1029/2025ms005014": "ClimaCore",  # atmosphere dynamical core
    "10.5194/gmd-15-6259-2022": "ClimaAtmos",  # ClimateMachine v0.2.0 lineage
}


def package_for_doi(doi) -> str | None:
    if not isinstance(doi, str):
        return None
    return PACKAGE_BY_DOI.get(doi.lower())


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def quarter_key(dt: datetime) -> str:
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def date_to_quarter(value) -> str | None:
    # Accepts str ("YYYY-MM-DD"), pandas Timestamp, or NaN. pd.read_json
    # autoconverts ISO-date strings to Timestamps, so we coerce defensively.
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value)[:10]
    if len(text) < 7:
        return None
    try:
        y, m = int(text[:4]), int(text[5:7])
    except ValueError:
        return None
    return f"{y}-Q{(m - 1) // 3 + 1}"


# Anything before 2018-Q1 is noise from old papers that predate the CliMA
# packaging effort; the time-series axis is cropped to that floor.
MIN_QUARTER = "2018-Q1"


# Bot accounts whose ``is_bot`` field is false on the GitHub API but which
# are clearly automated — usually Julia ecosystem bots.
EXTRA_BOTS: set[str] = {"JuliaTagBot", "attobot"}

# Pinned colors for the highest-profile repos so they read the same in
# every figure; the remaining names draw from the palette, which has these
# hues removed so nothing else reuses them.
PINNED_COLORS = {
    "Oceananigans": "#1F77B4",  # blue
    "ClimaAtmos": "#EDB120",  # yellow
    "ClimaCore": "#D62728",  # red
    "ClimaLand": "#2CA02C",  # green
}


MONTHS_SHOWN = 24


def month_start(values: pd.Series) -> pd.Series:
    """First day of the month for each date or ISO timestamp (NaT if bad)."""
    ts = pd.to_datetime(values, errors="coerce", utc=True).dt.tz_localize(None)
    return ts.dt.to_period("M").dt.to_timestamp()


def stacked_pivot(
    df: pd.DataFrame, period_col: str, group_col: str, periods
) -> pd.DataFrame:
    """Event counts per period (rows) and group (columns)."""
    if df.empty:
        return pd.DataFrame(index=periods)
    return (
        df.groupby([period_col, group_col])
        .size()
        .unstack(fill_value=0)
        .reindex(periods, fill_value=0)
    )


def make_engagement_figure(
    cit_events_df: pd.DataFrame,
    eng_events_df: pd.DataFrame,
    deps_df: pd.DataFrame,
    period_col: str,
    periods,
    unit: str,
    color_for: dict[str, str],
    title: str,
) -> go.Figure:
    """Four stacked-bar panels of external engagement, one bar per period.

    Each event table needs a ``period_col`` column; events outside
    ``periods`` are dropped.
    """
    all_by_period = (
        cit_events_df.groupby(period_col).size().reindex(periods, fill_value=0)
        if not cit_events_df.empty
        else pd.Series(0, index=periods)
    )
    panels = [
        (
            2,
            stacked_pivot(
                cit_events_df[cit_events_df["cited_package"] != ""],
                period_col,
                "cited_package",
                periods,
            ),
        ),
        (3, stacked_pivot(eng_events_df, period_col, "repo", periods)),
        (4, stacked_pivot(deps_df, period_col, "clima_package", periods)),
    ]

    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=(
            "All paper citations",
            "Software paper citations",
            "GitHub stars/forks/issues/PRs",
            "New Julia registry dependents",
        ),
        row_heights=[0.24, 0.24, 0.24, 0.24],
    )
    fig.add_bar(
        name="All papers",
        x=periods,
        y=all_by_period.tolist(),
        marker_color="#888",
        showlegend=False,
        row=1,
        col=1,
    )
    # Same name -> same legend entry across panels, so each package or repo
    # shows once and toggles everywhere.
    legend_shown: set[str] = set()
    for row, pivot in panels:
        totals = pivot.sum().sort_values(ascending=False, kind="stable")
        for name in totals[totals > 0].index:
            fig.add_bar(
                name=name,
                x=periods,
                y=pivot[name].tolist(),
                marker_color=color_for[name],
                legendgroup=name,
                showlegend=name not in legend_shown,
                row=row,
                col=1,
            )
            legend_shown.add(name)

    fig.update_layout(
        barmode="stack",
        title=title,
        height=900,
        margin=dict(t=50, b=40, l=60, r=20),
        legend=dict(tracegroupgap=0, itemsizing="constant", font=dict(size=11)),
    )
    fig.update_yaxes(title_text=f"Citations/{unit}", row=1, col=1)
    fig.update_yaxes(title_text=f"Citations/{unit}", row=2, col=1)
    fig.update_yaxes(title_text=f"Events/{unit}", row=3, col=1)
    fig.update_yaxes(title_text=f"New deps/{unit}", row=4, col=1)
    fig.update_xaxes(title_text=unit.capitalize(), row=4, col=1)
    return fig


WEEKS_SHOWN = 52


def make_weekly_github_figure(
    eng_events_df: pd.DataFrame, color_for: dict[str, str]
) -> go.Figure:
    """Stacked weekly external GitHub events by repo over the last year."""
    ts = pd.to_datetime(eng_events_df["event_at"], utc=True).dt.tz_localize(
        None
    )
    week = ts.dt.normalize() - pd.to_timedelta(ts.dt.weekday, unit="D")
    end = week.max()  # Start of the newest (possibly partial) week
    start = end - pd.Timedelta(weeks=WEEKS_SHOWN)
    weeks = pd.date_range(start, end, freq="7D", inclusive="left")
    in_window = (week >= start) & (week < end)
    pivot = (
        eng_events_df[in_window]
        .assign(week=week[in_window])
        .groupby(["week", "repo"])
        .size()
        .unstack(fill_value=0)
        .reindex(weeks, fill_value=0)
    )
    order = pivot.sum().sort_values(ascending=False, kind="stable")
    fig = go.Figure()
    for repo in order[order > 0].index:
        fig.add_bar(
            name=repo,
            x=weeks,
            y=pivot[repo].tolist(),
            marker_color=color_for[repo],
            hovertemplate="Week of %{x|%b %d, %Y}<br>%{y} events<extra>"
            + repo
            + "</extra>",
        )
    fig.update_layout(
        barmode="stack",
        title="External GitHub stars/forks/issues/PRs, last 52 weeks",
        height=600,
        margin=dict(t=50, b=40, l=60, r=20),
        # List the biggest repos first rather than in stack order.
        legend=dict(
            traceorder="normal",
            tracegroupgap=0,
            itemsizing="constant",
            font=dict(size=11),
        ),
    )
    fig.update_yaxes(title_text="Events/week")
    fig.update_xaxes(title_text="Week starting")
    return fig


def main() -> None:
    # Load the four normalized OpenAlex tables written by fetch-citations.
    # resolved-pubs is the master pubs table (every work fetched, CliMA pubs +
    # the works citing them, deduped). clima-pubs is the slim extras keyed by
    # openalex_id — join on openalex_id to recover full CliMA-pub records.
    openalex_dir = DATA / "openalex"
    resolved_pubs_df = pd.read_json(
        openalex_dir / "resolved-pubs.jsonl", lines=True
    )
    clima_extras_df = pd.read_json(openalex_dir / "clima-pubs.jsonl", lines=True)
    citation_edges_df = pd.read_json(openalex_dir / "citations.jsonl", lines=True)
    clima_pubs_df = clima_extras_df.merge(
        resolved_pubs_df, on="openalex_id", how="left"
    )
    print(
        f"resolved_pubs={len(resolved_pubs_df)} | clima_pubs={len(clima_pubs_df)} | "
        f"citation_edges={len(citation_edges_df)}"
    )

    # ----- Internal-user set (canonical CliMA org members) ---------------
    members_path = DATA / "github" / "clima_members.json"
    clima_members: set[str] = {"CliMA"}
    if members_path.exists():
        clima_members.update(
            json.loads(members_path.read_text()).get("members", [])
        )


    def is_external(login: str | None) -> bool:
        if not login:
            return False
        if login.endswith("[bot]") or login in EXTRA_BOTS:
            return False
        return login not in clima_members


    # ----- CamelCase repo lookup -----------------------------------------
    camel_by_lc: dict[str, str] = {}
    creation_path = DATA / "github" / "repo_creation_dates.jsonl"
    if creation_path.exists():
        for line in creation_path.read_text().splitlines():
            if not line.strip():
                continue
            name = json.loads(line)["name"].replace(".jl", "")
            camel_by_lc[name.lower()] = name

    # Some tracked repos (e.g. Oceananigans, which lives outside the CliMA org)
    # are missing from the creation-date file, so also seed from the daily stats
    # snapshots, which carry canonical casing for every tracked repo. Without
    # this the lowercase on-disk directory name leaks through for those repos and
    # they split into two separate series.
    stats_dir = DATA / "github" / "repo_stats"
    for snap_path in sorted(stats_dir.glob("*.jsonl")):
        for line in snap_path.read_text().splitlines():
            if not line.strip():
                continue
            name = json.loads(line)["name"].replace(".jl", "")
            camel_by_lc.setdefault(name.lower(), name)


    def camel(name: str) -> str:
        return camel_by_lc.get(name.lower(), name)


    # ----- Citation event table -------------------------------------------
    # "Internal" = the citing pub is itself a CliMA pub (a self-citation by the
    # org), or it shares a real author OpenAlex ID with any CliMA pub. Computed
    # downstream so the filter can be tuned without refetching.
    #
    # OpenAlex assigns a placeholder "Anonymous" author (no ORCID) to works whose
    # authorship it hasn't attributed yet — common for brand-new preprints. That
    # junk ID is shared across unrelated works, so leaving it in the CliMA author
    # set would falsely flag any "Anonymous"-attributed citing pub as internal and
    # silently drop real external citations. Exclude it from the author match; the
    # self-citation rule still catches CliMA pubs that only carry the placeholder.
    placeholder_author_ids: set[str] = set()
    authors_path = openalex_dir / "authors.jsonl"
    if authors_path.exists():
        for line in authors_path.read_text().splitlines():
            if not line.strip():
                continue
            au = json.loads(line)
            if au.get("orcid") is None and au.get("display_name") == "Anonymous":
                placeholder_author_ids.add(au["openalex_id"])

    clima_pub_ids: set[str] = set(clima_pubs_df["openalex_id"])
    clima_author_ids: set[str] = set()
    for ids in clima_pubs_df["author_ids"]:
        clima_author_ids.update(ids or [])
    clima_author_ids -= placeholder_author_ids

    resolved_pubs_df = resolved_pubs_df.assign(
        is_internal=resolved_pubs_df.apply(
            lambda r: r["openalex_id"] in clima_pub_ids
            or bool(set(r["author_ids"] or []) & clima_author_ids),
            axis=1,
        )
    )
    cited_lookup = clima_pubs_df[["openalex_id", "doi"]].rename(
        columns={"openalex_id": "cited_openalex_id", "doi": "cited_doi"}
    )
    joined = citation_edges_df.merge(
        resolved_pubs_df[
            ["openalex_id", "publication_date", "quarter", "is_internal"]
        ].rename(columns={"openalex_id": "citing_openalex_id"}),
        on="citing_openalex_id",
        how="left",
    ).merge(cited_lookup, on="cited_openalex_id", how="left")
    joined = joined[~joined["is_internal"].fillna(False)]
    joined = joined[joined["quarter"].notna() & (joined["quarter"] >= MIN_QUARTER)]
    joined["cited_package"] = joined["cited_doi"].map(package_for_doi).fillna("")
    cit_events_df = joined[
        [
            "quarter",
            "cited_doi",
            "cited_package",
            "citing_openalex_id",
            "publication_date",
        ]
    ].reset_index(drop=True)

    # ----- Event-level GitHub engagement rows -----------------------------
    eng_event_rows: list[dict] = []


    def add_eng_event(
        repo: str, event_type: str, login: str | None, iso_ts: str | None
    ) -> None:
        if not iso_ts:
            return
        try:
            dt = parse_iso(iso_ts)
        except ValueError:
            return
        q = quarter_key(dt)
        if q < MIN_QUARTER:
            return
        eng_event_rows.append(
            {
                "quarter": q,
                "repo": repo,
                "event_type": event_type,
                "login": login or "",
                "event_at": iso_ts,
            }
        )


    for f in (DATA / "github" / "stargazers").glob("*.jsonl"):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            login = ev.get("login")
            if not is_external(login):
                continue
            repo = camel((ev.get("repo") or "").replace(".jl", ""))
            add_eng_event(repo, "star", login, ev.get("starred_at"))

    for f in (DATA / "github" / "forks").glob("*.jsonl"):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            fk = json.loads(line)
            if (
                fk.get("fork_owner_type") == "Organization"
                and fk.get("fork_owner") == "CliMA"
            ):
                continue
            login = fk.get("fork_owner")
            if not is_external(login):
                continue
            repo = camel((fk.get("repo") or "").replace(".jl", ""))
            add_eng_event(repo, "fork", login, fk.get("fork_created"))

    for repo_dir in (DATA / "github" / "issues").iterdir():
        repo = camel(repo_dir.name)
        for f in repo_dir.glob("*.json"):
            for iss in json.loads(f.read_text()):
                a = iss.get("author") or {}
                login = a.get("login")
                if a.get("is_bot") or not is_external(login):
                    continue
                add_eng_event(repo, "issue", login, iss.get("createdAt"))

    prs_root = DATA / "github" / "prs"
    for repo_dir in prs_root.iterdir():
        repo = camel(repo_dir.name)
        for f in repo_dir.glob("*.json"):
            for pr in json.loads(f.read_text()):
                a = pr.get("author") or {}
                login = a.get("login")
                if a.get("is_bot") or not is_external(login):
                    continue
                add_eng_event(repo, "pr_merged", login, pr.get("mergedAt"))

    eng_events_df = pd.DataFrame(eng_event_rows)

    # ----- New external Julia-registry dependents -------------------------
    # Read the event-log table written by fetch-registry. Group by the quarter
    # of ``first_seen_at`` to get "new external dependents per quarter, by
    # CliMA package". External = the dependent's repo is not in the CliMA org.
    deps_path = DATA / "julia-registry" / "dependents.jsonl"
    deps_df = (
        pd.read_json(deps_path, lines=True)
        if deps_path.exists()
        else pd.DataFrame()
    )
    if not deps_df.empty:
        deps_df = deps_df[~deps_df["is_clima"]].copy()
        deps_df["quarter"] = deps_df["first_seen_at"].map(date_to_quarter)
        deps_df = deps_df[
            deps_df["quarter"].notna() & (deps_df["quarter"] >= MIN_QUARTER)
        ]

    # ----- Shared repo/package colors -----------------------------------
    # Same package name gets the same color in every panel and figure,
    # ranked by all-time external engagement.
    combined_totals: dict[str, float] = defaultdict(float)
    for names in (
        cit_events_df.loc[cit_events_df["cited_package"] != "", "cited_package"]
        if not cit_events_df.empty
        else [],
        eng_events_df["repo"] if not eng_events_df.empty else [],
        deps_df["clima_package"] if not deps_df.empty else [],
    ):
        for name in names:
            combined_totals[name] += 1
    # Ties break by name so colors don't shuffle between runs.
    unified_order = sorted(
        combined_totals, key=lambda k: (-combined_totals[k], k)
    )

    palette = (
        list(pc.qualitative.Plotly)
        + list(pc.qualitative.D3)
        + list(pc.qualitative.Light24)
        + list(pc.qualitative.Dark24)
    )
    palette = [
        c
        for c in palette
        if c.upper() not in {v.upper() for v in PINNED_COLORS.values()}
    ]
    color_for: dict[str, str] = {}
    _next = 0
    for _name in unified_order:
        if _name in PINNED_COLORS:
            color_for[_name] = PINNED_COLORS[_name]
        else:
            color_for[_name] = palette[_next % len(palette)]
            _next += 1

    # ----- Quarterly figure, all time -------------------------------------
    all_quarters = sorted(
        set(cit_events_df.get("quarter", []))
        | set(eng_events_df.get("quarter", []))
        | set(deps_df.get("quarter", []))
    )
    fig = make_engagement_figure(
        cit_events_df,
        eng_events_df,
        deps_df,
        period_col="quarter",
        periods=all_quarters,
        unit="quarter",
        color_for=color_for,
        title="External engagement",
    )

    # ----- Monthly figure, last 24 months ---------------------------------
    # The window ends at the last complete month before the newest GitHub
    # event (the GitHub data is refreshed daily), so a partial month doesn't
    # read as a dip.
    newest_month = month_start(eng_events_df["event_at"]).max()
    months = pd.date_range(
        end=newest_month - pd.DateOffset(months=1),
        periods=MONTHS_SHOWN,
        freq="MS",
    )
    monthly_fig = make_engagement_figure(
        cit_events_df.assign(
            month=month_start(cit_events_df.get("publication_date"))
        ),
        eng_events_df.assign(month=month_start(eng_events_df["event_at"])),
        deps_df.assign(month=month_start(deps_df["first_seen_at"]))
        if not deps_df.empty
        else deps_df,
        period_col="month",
        periods=months,
        unit="month",
        color_for=color_for,
        title=f"External engagement, last {MONTHS_SHOWN} months",
    )

    # ----- Weekly GitHub engagement over the last year --------------------
    # Weeks start on Monday. The window ends at the last complete week
    # before the newest event, so a partial week doesn't read as a dip.
    weekly_fig = make_weekly_github_figure(eng_events_df, color_for)

    # ----- Persist figure + event-level CSVs ------------------------------
    os.makedirs(ROOT / "figures", exist_ok=True)
    os.makedirs(ROOT / "results", exist_ok=True)
    fig.write_json(ROOT / "figures/external-engagement.json")
    monthly_fig.write_json(ROOT / "figures/external-engagement-monthly.json")
    weekly_fig.write_json(ROOT / "figures/github-engagement-weekly.json")

    cit_events_df.sort_values(
        ["quarter", "cited_doi", "citing_openalex_id"]
    ).to_csv(ROOT / "results/external-citations.csv", index=False)
    eng_events_df.sort_values(
        ["quarter", "repo", "event_type", "event_at"]
    ).to_csv(ROOT / "results/github-engagement.csv", index=False)
    if not deps_df.empty:
        deps_df[
            [
                "quarter",
                "clima_package",
                "dependent_name",
                "dependent_repo",
                "first_seen_at",
            ]
        ].sort_values(["quarter", "clima_package", "dependent_name"]).to_csv(
            ROOT / "results/new-registry-dependents.csv", index=False
        )

    print(
        f"{len(cit_events_df)} citation events | "
        f"{len(eng_events_df)} engagement events | "
        f"{len(deps_df)} new external registry dependents"
    )


if __name__ == "__main__":
    main()
