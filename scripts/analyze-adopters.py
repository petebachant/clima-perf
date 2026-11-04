"""Who depends on CliMA packages outside CliMA, and which packages.

An adopter is a GitHub account (a person or an organization) with at least
one repo whose Project.toml lists a CliMA package as a direct dependency,
from the weekly code search, or that publishes a registered package that
does, from the Julia registry. Accounts count once per package however many
repos they have, so one lab with ten experiment repos doesn't outweigh ten
labs.

Each account gets one segment, from the first rule its public profile
(company, bio, org description, blog, location) matches, unless
data/adopter-labels.csv labels it by hand. Accounts that aren't outside
adoption are set aside rather than counted: CliMA's own org and members
(internal), groups run by CliMA developers (affiliated), and copies of CliMA
repos (copy).

An adopter is active if any of its repos using the package was pushed to in
the last 180 days, a rough retention signal.

Writes:

- ``results/external-adopters.csv``: one row per (account, package)
- ``results/adopter-accounts.csv``: one row per account, with its segment
  and the profile text that set it
- ``results/adopters-summary.json``: headline counts
- ``figures/external-adopters.json``: adopters per package by segment
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "data" / "github-code-dependents"
REGISTRY = ROOT / "data" / "julia-registry" / "dependents.jsonl"
MEMBERS = ROOT / "data" / "github" / "clima_members.json"
LABELS = ROOT / "data" / "adopter-labels.csv"

ACTIVE_DAYS = 180

# Counted segments in display order, with colors validated as a categorical
# palette; Unknown is a neutral gray, not a categorical slot.
SEGMENTS = {
    "Academic": "#2a78d6",
    "Research lab or government": "#eb6834",
    "Climate tech or carbon removal": "#1baf7a",
    "Industry": "#eda100",
    "Open-source project": "#e87ba4",
    "Unknown": "#a8a7a1",
}
EXCLUDED = ["Internal", "Affiliated", "Copy"]

# First match wins, so the more specific segments come first: a carbon
# removal startup is climate tech, not industry, and a postdoc with a lab
# fellowship, or an institute inside a university, is academic.
RULES = [
    (
        "Climate tech or carbon removal",
        r"carbon (dioxide )?removal|carbon capture|\bcdr\b|alkalinity|\boae\b"
        r"|\bmrv\b|\[c\]worthy",
    ),
    (
        "Industry",
        r"co-?founder|\bceo\b|\bcto\b|\binc\b|\bllc\b|\bltd\b|\bgmbh\b"
        r"|startup|consult",
    ),
    (
        "Academic",
        r"universi|college|\bschool\b|\beth\b|epfl|\bmit\b|caltech|imperial"
        r"|institute of technology|\bnjit\b|\bucl\b|\.edu\b|\.ac\.[a-z]{2}\b"
        r"|ph\.?d|postdoc|professor|lecturer|student|faculty|@uci-",
    ),
    (
        "Research lab or government",
        r"national lab|laborator|argonne|\bnoaa\b|\bnasa\b|\bncar\b|\bllnl\b"
        r"|\blanl\b|\bornl\b|\bpnnl\b|met office|met norway|ecmwf|nansen"
        r"|\bniva\b|institut|\bcnrs\b|\bcsiro\b|max planck|helmholtz"
        r"|\.gov\b|research cent|aircentre|geological survey",
    ),
    (
        "Open-source project",
        r"^julia|julia language|open[- ]source|community of|^luxdl$|^sciml$",
    ),
]
PROFILE_FIELDS = ["company", "bio", "description", "blog", "location"]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def classify(owner: dict, members: set[str], labels: dict) -> tuple[str, str]:
    """(segment, basis) for an account; basis says what set the segment."""
    login = owner["login"]
    if login in labels:
        return labels[login]["segment"], "labeled: " + labels[login]["note"]
    if login in members or login.lower() == "clima":
        return "Internal", "CliMA org or member"
    fields = {k: (owner.get(k) or "") for k in PROFILE_FIELDS}
    text = " | ".join([login] + list(fields.values()))
    # The login leads the text, so ^-anchored patterns match on it
    for segment, pattern in RULES:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return segment, f"matched '{m.group(0)}'"
    # An org whose description names a Julia package is that package's home
    desc = fields["description"]
    if owner.get("type") == "Organization" and re.search(r"\w\.jl\b", desc):
        return "Open-source project", "org describes a .jl package"
    if fields["company"]:
        return "Industry", f"company '{fields['company']}'"
    return "Unknown", "no affiliation in profile"


def repo_name_from_url(url: str | None) -> str | None:
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else None


def load_uses(members: set[str]) -> pd.DataFrame:
    """One row per (package, repo) with how it was found."""
    rows = [
        {
            "package": h["clima_package"],
            "repo": h["repo"],
            "source": "code search",
            "first_seen_at": h["first_seen_at"],
        }
        for h in read_jsonl(CODE / "hits.jsonl")
    ]
    for d in read_jsonl(REGISTRY):
        repo = repo_name_from_url(d.get("dependent_repo"))
        if repo and not d.get("is_clima"):
            rows.append(
                {
                    "package": d["clima_package"],
                    "repo": repo,
                    "source": "registry",
                    "first_seen_at": d["first_seen_at"],
                }
            )
    df = pd.DataFrame(rows)
    df = df[df["repo"].str.split("/").str[0].str.lower() != "clima"]
    df["repo_key"] = df["repo"].str.lower().str.removesuffix(".jl")
    return (
        df.groupby(["package", "repo_key"])
        .agg(
            repo=("repo", "first"),
            sources=("source", lambda s: ", ".join(sorted(set(s)))),
            first_seen_at=("first_seen_at", "min"),
        )
        .reset_index()
    )


def make_figure(adopters: pd.DataFrame) -> go.Figure:
    counted = adopters[adopters["segment"].isin(SEGMENTS)]
    pivot = (
        counted.groupby(["package", "segment"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=list(SEGMENTS), fill_value=0)
    )
    pivot = pivot.loc[pivot.sum(axis=1).sort_values().index]
    totals = pivot.sum(axis=1)
    fig = go.Figure()
    for segment, color in SEGMENTS.items():
        if pivot[segment].sum() == 0:
            continue
        fig.add_bar(
            y=pivot.index,
            x=pivot[segment],
            name=segment,
            orientation="h",
            marker=dict(color=color, line=dict(color="#fcfcfb", width=1)),
            hovertemplate="%{y}: %{x} " + segment.lower() + "<extra></extra>",
        )
    # Totals at the bar ends, in text ink, so the counts don't depend on
    # telling the lighter segment colors apart
    fig.add_scatter(
        y=pivot.index,
        x=totals,
        mode="text",
        text=[f"  {n}" for n in totals],
        textposition="middle right",
        textfont=dict(color="#52514e", size=11),
        hoverinfo="skip",
        showlegend=False,
    )
    n_accounts = counted["login"].nunique()
    fig.update_layout(
        barmode="stack",
        title="Who depends on CliMA packages outside CliMA"
        f"<br><sup>Distinct GitHub accounts ({n_accounts} in all) with a "
        "repo listing the package as a direct dependency</sup>",
        height=220 + 22 * len(pivot),
        margin=dict(t=90, b=120, l=20, r=40),
        # Below the plot, so it doesn't crowd the two-line title
        legend=dict(
            orientation="h",
            y=-0.12,
            yanchor="top",
            x=0,
            xanchor="left",
            traceorder="normal",
        ),
        bargap=0.25,
    )
    fig.update_xaxes(title_text="Accounts", range=[0, totals.max() * 1.12])
    return fig


def main() -> None:
    members = set(json.loads(MEMBERS.read_text())["members"])
    with LABELS.open(newline="") as f:
        labels = {r["login"]: r for r in csv.DictReader(f)}
    repos = {
        r["full_name"].lower().removesuffix(".jl"): r
        for r in read_jsonl(CODE / "repos.jsonl")
    }
    owners = {o["login"]: o for o in read_jsonl(CODE / "owners.jsonl")}

    uses = load_uses(members)
    uses["login"] = [
        (repos.get(k) or {}).get("owner") or r.split("/")[0]
        for k, r in zip(uses["repo_key"], uses["repo"])
    ]
    uses["pushed_at"] = [
        (repos.get(k) or {}).get("pushed_at") for k in uses["repo_key"]
    ]

    accounts = []
    for login in sorted(set(uses["login"])):
        prof = owners.get(login, {"login": login})
        segment, basis = classify(prof, members, labels)
        accounts.append(
            {
                "login": login,
                "type": prof.get("type"),
                "segment": segment,
                "basis": basis,
                **{k: prof.get(k) for k in PROFILE_FIELDS},
            }
        )
    accounts = pd.DataFrame(accounts)
    seg = dict(zip(accounts["login"], accounts["segment"]))

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=ACTIVE_DAYS)
    uses["active"] = pd.to_datetime(uses["pushed_at"], utc=True) >= cutoff
    adopters = (
        uses.groupby(["login", "package"])
        .agg(
            n_repos=("repo", "size"),
            repos=("repo", lambda s: " ".join(sorted(s))),
            sources=(
                "sources",
                lambda s: ", ".join(sorted(set(", ".join(s).split(", ")))),
            ),
            first_seen_at=("first_seen_at", "min"),
            last_pushed_at=(
                "pushed_at",
                lambda s: s.dropna().max() if s.notna().any() else None,
            ),
            active=("active", "any"),
        )
        .reset_index()
    )
    adopters["segment"] = adopters["login"].map(seg)

    acct_pkgs = adopters.groupby("login")["package"].apply(
        lambda s: " ".join(sorted(s))
    )
    accounts["packages"] = accounts["login"].map(acct_pkgs)
    (ROOT / "results").mkdir(exist_ok=True)
    adopters.sort_values(["package", "segment", "login"]).to_csv(
        ROOT / "results" / "external-adopters.csv", index=False
    )
    accounts.sort_values(["segment", "login"]).to_csv(
        ROOT / "results" / "adopter-accounts.csv", index=False
    )

    counted = adopters[adopters["segment"].isin(SEGMENTS)]
    acct = accounts[accounts["segment"].isin(SEGMENTS)]
    by_pkg = (
        counted.groupby("package")["login"]
        .nunique()
        .sort_values(ascending=False)
    )
    by_seg = acct["segment"].value_counts()
    known = acct[acct["segment"] != "Unknown"]
    active = counted[counted["active"]]["login"].nunique()
    summary = {
        "external_accounts": int(acct["login"].nunique()),
        "excluded_accounts": {
            s: int((accounts["segment"] == s).sum()) for s in EXCLUDED
        },
        "accounts_by_segment": {s: int(by_seg.get(s, 0)) for s in SEGMENTS},
        "academic_pct_of_known": round(
            100 * float((known["segment"] == "Academic").mean())
        ),
        "known_accounts": int(len(known)),
        # Flat keys for citing in calkit.yaml answers
        "academic_accounts": int(by_seg.get("Academic", 0)),
        "lab_accounts": int(by_seg.get("Research lab or government", 0)),
        "climate_tech_accounts": int(
            by_seg.get("Climate tech or carbon removal", 0)
        ),
        "industry_accounts": int(by_seg.get("Industry", 0)),
        "open_source_accounts": int(by_seg.get("Open-source project", 0)),
        "unknown_accounts": int(by_seg.get("Unknown", 0)),
        "active_accounts": int(active),
        "active_share": round(active / max(1, acct["login"].nunique()), 2),
        "top_package": by_pkg.index[0],
        "top_package_accounts": int(by_pkg.iloc[0]),
        "second_package": by_pkg.index[1],
        "second_package_accounts": int(by_pkg.iloc[1]),
        "accounts_by_package": {k: int(v) for k, v in by_pkg.items()},
        # The ESM components, which show up as zero when nobody uses them
        **{
            f"{pkg.lower()}_accounts": int(by_pkg.get(pkg, 0))
            for pkg in ("ClimaAtmos", "ClimaLand", "ClimaCoupler")
        },
        "accounts_without_oceananigans": int(
            counted[counted["package"] != "Oceananigans"]["login"].nunique()
        ),
    }
    (ROOT / "results" / "adopters-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    (ROOT / "figures").mkdir(exist_ok=True)
    make_figure(adopters).write_json(
        ROOT / "figures" / "external-adopters.json"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "accounts_by_package"},
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
