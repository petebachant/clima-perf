"""Rank the nightly AMIP's GPU kernels and track the top ones over time.

Reads the Nsight Systems ``cuda_gpu_kern_sum`` CSVs mirrored by
``fetch-nightly-nsys.py`` (one per scheduled build of
``climacoupler-coarse-nightly-amip``) and writes:

- ``results/amip-gpu-kernels-latest.csv``: every kernel from the latest
  nightly that produced a summary, most expensive first. This file is
  rewritten in place each night, so its Git history is the history of the
  ranking.
- ``results/amip-gpu-kernels-latest.md``: the top of that table with a link
  to the build, for the project page.
- ``figures/amip-gpu-kernels-latest.json``: the latest night's most
  expensive kernels as a ranked bar chart, with each one's rank the night
  before in its hover.
- ``figures/amip-gpu-kernels.json``: total GPU kernel time and the time of
  each of the latest top kernels, per nightly, over the profiled window.

Kernel names come from ``CLIMA_NAME_CUDA_KERNELS_FROM_STACK_TRACE``, e.g.,
``set_environment_1m_tendency__FILE_ClimaAtmos_59wdn_src_cache_microphysics_cache_jl_L1116``.
The five characters after the package name are the package's depot slug,
which changes with its version, and the line number moves with any edit
above it, so neither can identify a kernel across nights. The trend groups
kernels by function and source file instead, summing a function's kernels
at different lines of one file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PIPELINE = "climacoupler-coarse-nightly-amip"
DATA_DIR = Path("data/buildkite-nightly-nsys") / PIPELINE
BUILD_URL = f"https://buildkite.com/clima/{PIPELINE}/builds/{{}}"

N_MARKDOWN = 25
N_BARS = 20

# Fixed-order categorical palette shared with analyze-weekly, validated for
# color-vision deficiency on adjacent pairs. Eight slots, so the trend
# shows the eight most expensive kernels.
PALETTE = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]

# <Package>_<slug>_<path with / and . as _>_jl_L<line>
SOURCE = re.compile(
    r"^(?P<pkg>[A-Za-z0-9]+)_(?P<slug>[A-Za-z0-9]{5})_(?P<path>.+)_jl"
    r"_L(?P<line>\d+)$"
)
SLUG = re.compile(r"_FILE_([A-Za-z0-9]+)_[A-Za-z0-9]{5}_")


def parse_name(name: str) -> dict:
    """Split a kernel name into a readable function, package, file, line.

    The file path can't be fully recovered, since ``/`` and ``_`` both
    became ``_``, so it keeps the underscores and only restores ``.jl``.
    """
    func, package, file, line = name, "", "", None
    if "_FILE_" in name:
        func, _, source = name.partition("_FILE_")
        match = SOURCE.match(source)
        if match:
            package = match["pkg"]
            file = match["path"] + ".jl"
            line = int(match["line"])
    elif name.endswith("_NVTX"):
        # Named after the enclosing NVTX range rather than a source line
        func = name.removesuffix("_NVTX")
    elif "(" in name:
        # A C++-style signature, e.g., RRTMGP's solvers and GPUArrays
        # broadcasts; the argument types run to thousands of characters
        func = name.split("(", 1)[0]
    func = func.strip("_") or "(anonymous)"
    return {"kernel": func, "package": package, "file": file, "line": line}


def load() -> pd.DataFrame:
    frames = []
    for index_path in sorted(DATA_DIR.glob("*.json")):
        index = json.loads(index_path.read_text())
        # A retried nsys job leaves one summary per attempt; the last to
        # finish stands for the build so its kernels aren't counted twice
        artifacts = sorted(
            index["artifacts"], key=lambda a: a["job_finished_at"] or ""
        )[-1:]
        for artifact in artifacts:
            df = pd.read_csv(DATA_DIR / artifact["local_path"])
            df = df.rename(
                columns={
                    "Time (%)": "time_pct",
                    "Total Time (ns)": "total_ns",
                    "Instances": "instances",
                    "Avg (ns)": "avg_ns",
                    "Med (ns)": "median_ns",
                    "Min (ns)": "min_ns",
                    "Max (ns)": "max_ns",
                    "StdDev (ns)": "stddev_ns",
                    "Name": "name",
                }
            )
            df["build"] = index["build_number"]
            df["date"] = pd.to_datetime(index["build_created_at"]).date()
            df["commit"] = index["commit"]
            df["job_id"] = artifact["job_id"]
            frames.append(df)
    if not frames:
        raise RuntimeError(f"No kernel summaries under {DATA_DIR}")
    df = pd.concat(frames, ignore_index=True)
    parsed = pd.DataFrame([parse_name(n) for n in df["name"]])
    parsed["line"] = parsed["line"].astype("Int64")
    return pd.concat([df, parsed], axis=1)


def source_label(row) -> str:
    if not row["package"]:
        return ""
    return f"{row['package']} {row['file']}:{row['line']}"


def write_latest(latest: pd.DataFrame) -> None:
    latest = latest.sort_values("total_ns", ascending=False).reset_index(
        drop=True
    )
    out = pd.DataFrame(
        {
            "rank": latest.index + 1,
            "kernel": latest["kernel"],
            "source": latest.apply(source_label, axis=1),
            "time_pct": latest["time_pct"],
            "total_ms": (latest["total_ns"] / 1e6).round(3),
            "instances": latest["instances"],
            "avg_us": (latest["avg_ns"] / 1e3).round(2),
            "median_us": (latest["median_ns"] / 1e3).round(2),
            "min_us": (latest["min_ns"] / 1e3).round(2),
            "max_us": (latest["max_ns"] / 1e3).round(2),
            "date": latest["date"],
            "build": latest["build"],
            "name": latest["name"],
        }
    )
    out.to_csv("results/amip-gpu-kernels-latest.csv", index=False)

    build = int(latest["build"].iloc[0])
    date = latest["date"].iloc[0]
    commit = latest["commit"].iloc[0]
    total_ms = latest["total_ns"].sum() / 1e6
    lines = [
        "# Most expensive GPU kernels in the nightly AMIP",
        "",
        f"From the {date} nightly, [build {build}]({BUILD_URL.format(build)})"
        f" of `{PIPELINE}` at ClimaCoupler.jl `{commit[:8]}`: the Nsight "
        "Systems kernel summary of the profiled steps of the "
        "`amip_progedmf_1m_land_he16` configuration, "
        f"{total_ms:,.0f} ms of GPU kernel time in all. The top "
        f"{N_MARKDOWN} of {len(out)} kernels are shown; "
        "`results/amip-gpu-kernels-latest.csv` has them all.",
        "",
        "| # | Kernel | Source | GPU time (%) | Total (ms) | Calls "
        "| Avg (µs) |",
        "|--:|---|---|--:|--:|--:|--:|",
    ]
    for _, row in out.head(N_MARKDOWN).iterrows():
        source = f"`{row['source']}`" if row["source"] else ""
        lines.append(
            f"| {row['rank']} | `{row['kernel']}` | {source} "
            f"| {row['time_pct']:.1f} | {row['total_ms']:,.1f} "
            f"| {row['instances']:,} | {row['avg_us']:,.1f} |"
        )
    Path("results/amip-gpu-kernels-latest.md").write_text(
        "\n".join(lines) + "\n"
    )


def write_trend(df: pd.DataFrame, latest_build: int) -> None:
    df = df.assign(
        group=df["kernel"]
        + df["package"].where(df["package"] == "", " (" + df["package"])
        + df["file"].where(df["file"] == "", " " + df["file"] + ")")
    )
    by_group = (
        df.groupby(["build", "date", "group"], as_index=False)["total_ns"]
        .sum()
        .assign(total_ms=lambda d: d["total_ns"] / 1e6)
    )
    totals = (
        df.groupby(["build", "date"], as_index=False)["total_ns"]
        .sum()
        .assign(total_ms=lambda d: d["total_ns"] / 1e6)
        .sort_values("date")
    )
    top = (
        by_group[by_group["build"] == latest_build]
        .nlargest(len(PALETTE), "total_ns")["group"]
        .tolist()
    )

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.1,
        row_heights=[0.3, 0.7],
        subplot_titles=[
            "Total GPU kernel time (ms) over the profiled steps",
            f"The latest {len(top)} most expensive kernels (ms)",
        ],
    )
    fig.add_trace(
        go.Scatter(
            x=totals["date"],
            y=totals["total_ms"],
            name="All kernels",
            showlegend=False,
            mode="lines+markers",
            marker=dict(color="#52514e", size=8),
            line=dict(color="#52514e", width=2),
            customdata=totals[["build"]],
            hovertemplate=(
                "<b>All kernels</b><br>%{x|%Y-%m-%d}<br>%{y:,.0f} ms<br>"
                "build %{customdata[0]}<extra></extra>"
            ),
        ),
        row=1,
        col=1,
    )
    for i, group in enumerate(top):
        sub = by_group[by_group["group"] == group].sort_values("date")
        fig.add_trace(
            go.Scatter(
                x=sub["date"],
                y=sub["total_ms"],
                name=group,
                mode="lines+markers",
                marker=dict(color=PALETTE[i], size=8),
                line=dict(color=PALETTE[i], width=2),
                customdata=sub[["build"]],
                hovertemplate=(
                    f"<b>{group}</b><br>%{{x|%Y-%m-%d}}<br>"
                    "%{y:,.1f} ms<br>build %{customdata[0]}<extra></extra>"
                ),
            ),
            row=2,
            col=1,
        )
    fig.update_yaxes(rangemode="tozero")
    fig.update_xaxes(tickformat="%b %-d, %Y")
    if (totals["date"].max() - totals["date"].min()).days < 14:
        # Otherwise a short history gets hourly ticks between nightlies
        fig.update_xaxes(dtick=86400000)
    fig.update_layout(
        title="Nightly AMIP GPU kernel time",
        template="plotly_white",
        height=750,
        hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.08, x=0),
    )
    fig.write_json("figures/amip-gpu-kernels.json")


def site_key(name: str) -> str:
    """A kernel's raw name without the package slug, to match it across
    nights while its version changes. A line number that moved still
    breaks the match."""
    return SLUG.sub(r"_FILE_\1_", name)


def ranked(df: pd.DataFrame, build: int) -> pd.DataFrame:
    night = df[df["build"] == build].sort_values("total_ns", ascending=False)
    return night.assign(
        rank=range(1, len(night) + 1), site=night["name"].map(site_key)
    )


def write_bars(df: pd.DataFrame, latest_build: int) -> None:
    """The latest night's most expensive kernels, as a ranked bar chart.

    Each bar's hover gives the kernel's rank and time the night before, so
    a kernel that has climbed the ranking stands out.
    """
    latest = ranked(df, latest_build).head(N_BARS)
    earlier = sorted(b for b in df["build"].unique() if b < latest_build)
    if earlier:
        previous = ranked(df, earlier[-1]).set_index("site")
        latest = latest.join(
            previous[["rank", "total_ns"]].rename(
                columns={"rank": "prev_rank", "total_ns": "prev_ns"}
            ),
            on="site",
        )
        # Fallback for a kernel whose launch site didn't match: a moved
        # line, or a kernel renamed between its source line and its NVTX
        # range, or split across lines, which nsys reports as separate rows
        prev_by_func = previous.groupby("kernel")["total_ns"].sum()

    def pct(now: float, before: float) -> str:
        change = round((now - before) / before * 100)
        return f"{change:+d}%" if change else "unchanged"

    def was(row) -> str:
        if not earlier:
            return "First night profiled"
        if pd.isna(row["prev_rank"]):
            before = prev_by_func.get(row["kernel"])
            if before is None:
                return "Not in the previous night's profile"
            return (
                "Launch site not in the previous night's profile; "
                f"its {row['kernel']} kernels took {before / 1e6:,.1f} ms "
                f"({pct(row['total_ns'], before)})"
            )
        return (
            f"Previous night: #{int(row['prev_rank'])}, "
            f"{row['prev_ns'] / 1e6:,.1f} ms "
            f"({pct(row['total_ns'], row['prev_ns'])})"
        )

    labels = [f"{r}. {k}" for r, k in zip(latest["rank"], latest["kernel"])]
    hover = pd.DataFrame(
        {
            "source": latest.apply(source_label, axis=1).replace("", "—"),
            "total_ms": latest["total_ns"] / 1e6,
            "instances": latest["instances"],
            "avg_us": latest["avg_ns"] / 1e3,
            "was": latest.apply(was, axis=1),
        }
    )
    date = latest["date"].iloc[0]
    fig = go.Figure(
        go.Bar(
            x=latest["time_pct"],
            y=labels,
            orientation="h",
            marker=dict(color=PALETTE[0], cornerradius=4),
            customdata=hover,
            hovertemplate=(
                "<b>%{y}</b><br>%{customdata[0]}<br>"
                "%{x:.1f}% of GPU time, %{customdata[1]:,.1f} ms<br>"
                "%{customdata[2]:,} calls, %{customdata[3]:,.1f} µs each<br>"
                "%{customdata[4]}<extra></extra>"
            ),
        )
    )
    fig.update_yaxes(autorange="reversed", automargin=True)
    fig.update_xaxes(title="Share of GPU kernel time (%)", rangemode="tozero")
    fig.update_layout(
        title=(
            f"Most expensive GPU kernels in the nightly AMIP "
            f"({date}, build {latest_build})"
        ),
        template="plotly_white",
        height=160 + 28 * len(latest),
        bargap=0.25,
        showlegend=False,
    )
    fig.write_json("figures/amip-gpu-kernels-latest.json")


def main() -> None:
    df = load()
    latest_build = int(df["build"].max())
    Path("results").mkdir(exist_ok=True)
    Path("figures").mkdir(exist_ok=True)
    write_latest(df[df["build"] == latest_build])
    write_bars(df, latest_build)
    write_trend(df, latest_build)
    print(f"{df['build'].nunique()} nightlies; latest is build {latest_build}")


if __name__ == "__main__":
    main()
