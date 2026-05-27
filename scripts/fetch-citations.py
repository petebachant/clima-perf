"""Snapshot OpenAlex citation data for every paper listed on
https://clima.caltech.edu/publications/.

Flow:

1. Scrape the publications page and extract every ``doi.org/<DOI>`` link.
   This is the source of truth for "what counts as a CliMA paper" — adding
   a paper to the publications page is enough to start tracking it here on
   the next nightly run.
2. For each DOI, fetch the OpenAlex work record (cited_by_count,
   counts_by_year, authors, venue, etc.).
3. Build the union of CliMA-paper author IDs across all works and use it to
   split each paper's citations into total / recent / external (citations
   from works whose authors are disjoint from the CliMA-author set).
4. Write the snapshot to ``data/openalex/citations/<YYYY-MM-DD>.json`` keyed
   by the UTC day it represents. No-op if that day's file already exists.

We deliberately do **not** group papers by CliMA package in this script —
that mapping (package → DOI(s)) is hard-coded in the analysis notebook,
where the no-software / package=None bucket is also handled.
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "openalex" / "citations"

TODAY_UTC = datetime.now(timezone.utc).date()
LAST_DAY = TODAY_UTC - timedelta(days=1)

PUBLICATIONS_URL = "https://clima.caltech.edu/publications/"
OPENALEX_WORK_BY_DOI = "https://api.openalex.org/works/doi:"
OPENALEX_WORKS = "https://api.openalex.org/works"
# OpenAlex's polite pool — gives a higher rate-limit tier and a contact if
# something misbehaves.
MAILTO = os.environ.get("OPENALEX_MAILTO", "petebachant@gmail.com")
RECENT_YEARS = 2
PER_PAGE = 200

# Canonical software-paper DOIs that aren't linked from the CliMA publications
# page but should still be tracked. The Oceananigans JOSS paper is the
# headline case — most of Oceananigans' citation signal is on that DOI but
# the page only links to the newer GPU JAMES paper and the arXiv preprint.
ADDITIONAL_DOIS = [
    "10.21105/joss.02018",  # Ramadhan et al. 2020, Oceananigans JOSS
]

# Matches a doi.org URL and captures the DOI itself. DOIs can contain almost
# any character; we stop at whitespace / quote / closing angle or paren so
# we don't over-eat the surrounding HTML.
DOI_URL_RE = re.compile(
    r'https?://(?:dx\.)?doi\.org/(10\.[^\s"\'<>)]+)',
    re.IGNORECASE,
)


def scrape_dois(url: str) -> list[str]:
    """Return the deduped, normalized list of DOIs found on a page."""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    found: list[str] = []
    seen: set[str] = set()
    for m in DOI_URL_RE.finditer(resp.text):
        raw = m.group(1)
        # Drop trailing punctuation that commonly hangs off the end of a
        # bare URL in prose: "...).", "...),", etc.
        doi = raw.rstrip(").,;]")
        # Normalize case: DOIs are case-insensitive but conventionally
        # lower-cased.
        key = doi.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(doi)
    return found


def fetch_work(doi: str) -> dict:
    resp = requests.get(
        OPENALEX_WORK_BY_DOI + doi,
        params={"mailto": MAILTO},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_citing_works(cited_work_id: str) -> list[dict]:
    """Return all works that cite the given OpenAlex work ID."""
    short_id = cited_work_id.rsplit("/", 1)[-1]
    cursor = "*"
    out: list[dict] = []
    while cursor:
        params = {
            "filter": f"cites:{short_id}",
            "per-page": PER_PAGE,
            "cursor": cursor,
            "select": "id,authorships,publication_year,publication_date",
            "mailto": MAILTO,
        }
        resp = requests.get(OPENALEX_WORKS, params=params, timeout=60)
        resp.raise_for_status()
        page = resp.json()
        out.extend(page.get("results", []))
        cursor = page.get("meta", {}).get("next_cursor")
        if not cursor or not page.get("results"):
            break
    return out


def author_ids(work: dict) -> set[str]:
    ids: set[str] = set()
    for a in work.get("authorships", []) or []:
        aid = (a.get("author") or {}).get("id")
        if aid:
            ids.add(aid)
    return ids


def quarter_of(work: dict) -> str | None:
    d = work.get("publication_date")
    if d:
        try:
            y, m = int(d[:4]), int(d[5:7])
            return f"{y}-Q{(m - 1) // 3 + 1}"
        except (ValueError, IndexError):
            pass
    y = work.get("publication_year")
    return f"{y}-Q1" if y else None


def recent_citations(counts_by_year: list[dict], years: int) -> int:
    if not counts_by_year:
        return 0
    cutoff_year = date.today().year - (years - 1)
    return sum(
        c["cited_by_count"] for c in counts_by_year if c["year"] >= cutoff_year
    )


def summarize(work: dict) -> dict:
    """Pull the citation-relevant fields out of an OpenAlex work record."""
    venue = (
        ((work.get("primary_location") or {}).get("source") or {}).get(
            "display_name"
        )
    )
    authors = [
        {
            "id": (a.get("author") or {}).get("id"),
            "display_name": (a.get("author") or {}).get("display_name"),
        }
        for a in (work.get("authorships") or [])
    ]
    return {
        "openalex_id": work.get("id"),
        "title": work.get("title"),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "venue": venue,
        "authors": authors,
        "cited_by_count": work.get("cited_by_count", 0),
        "cited_by_count_recent": recent_citations(
            work.get("counts_by_year", []), RECENT_YEARS
        ),
        "counts_by_year": work.get("counts_by_year", []),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{LAST_DAY.isoformat()}.json"
    if out_path.exists():
        print(f"Already have snapshot for {LAST_DAY} at {out_path}")
        return 0

    print(f"Scraping DOIs from {PUBLICATIONS_URL}")
    try:
        dois = scrape_dois(PUBLICATIONS_URL)
    except requests.RequestException as err:
        print(f"  ERROR scraping publications page: {err}", file=sys.stderr)
        return 1
    print(f"  found {len(dois)} unique DOIs on the publications page")

    # Union in DOIs that aren't on the publications page but we still want
    # to track (canonical software papers, mostly).
    have = {d.lower() for d in dois}
    for extra in ADDITIONAL_DOIS:
        if extra.lower() not in have:
            dois.append(extra)
            have.add(extra.lower())
    print(f"  total DOIs to fetch (with ADDITIONAL_DOIS): {len(dois)}")

    # --- Pass 1: fetch each work, build CliMA-author union ------------------
    print("Pass 1: fetching OpenAlex works + collecting CliMA author IDs...")
    work_cache: dict[str, dict] = {}
    not_found: list[dict] = []
    clima_authors: set[str] = set()
    for doi in dois:
        try:
            w = fetch_work(doi)
        except requests.HTTPError as err:
            status = err.response.status_code if err.response is not None else "?"
            print(f"  WARN: {doi} -> HTTP {status}", file=sys.stderr)
            not_found.append({"doi": doi, "error": f"HTTP {status}"})
            continue
        except requests.RequestException as err:
            print(f"  WARN: {doi} -> {err}", file=sys.stderr)
            not_found.append({"doi": doi, "error": str(err)})
            continue
        work_cache[doi] = w
        clima_authors.update(author_ids(w))
    print(
        f"  fetched {len(work_cache)} works "
        f"({len(not_found)} not found), "
        f"{len(clima_authors)} distinct CliMA authors."
    )

    # --- Pass 2: enumerate citing works for external-citation counts --------
    print("Pass 2: enumerating citing works per paper for external split...")
    papers: list[dict] = []
    for doi, work in work_cache.items():
        summary = summarize(work)
        summary["doi"] = doi

        try:
            citing = fetch_citing_works(work["id"])
        except requests.HTTPError as err:
            print(f"  WARN: {doi} citing-works -> {err}", file=sys.stderr)
            summary["citing_works_status"] = f"error: {err}"
            summary["cited_by_count_external"] = None
            summary["citing_works"] = []
        else:
            ext_n = 0
            ext_by_q: dict[str, int] = {}
            citing_records: list[dict] = []
            for cw in citing:
                is_internal = bool(author_ids(cw) & clima_authors)
                q = quarter_of(cw)
                # Slim per-citing-work record so the analyze notebook can
                # build event-level CSVs without re-querying OpenAlex.
                citing_records.append({
                    "openalex_id": cw.get("id"),
                    "publication_date": cw.get("publication_date"),
                    "publication_year": cw.get("publication_year"),
                    "quarter": q,
                    "is_internal": is_internal,
                })
                if is_internal:
                    continue
                ext_n += 1
                if q:
                    ext_by_q[q] = ext_by_q.get(q, 0) + 1
            summary["citing_works_fetched"] = len(citing)
            summary["cited_by_count_external"] = ext_n
            summary["external_by_quarter"] = dict(sorted(ext_by_q.items()))
            summary["citing_works"] = citing_records

        ext_disp = summary.get("cited_by_count_external")
        print(
            f"  {doi}: {summary['cited_by_count']} total / "
            f"{summary['cited_by_count_recent']} in last {RECENT_YEARS}y / "
            f"{ext_disp if ext_disp is not None else '?'} external"
        )
        papers.append(summary)

    # Stable ordering: most-cited first, then DOI for ties.
    papers.sort(key=lambda p: (-p.get("cited_by_count", 0), p["doi"]))

    snapshot = {
        "snapshot_date": LAST_DAY.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": PUBLICATIONS_URL,
        "recent_window_years": RECENT_YEARS,
        "clima_author_set_size": len(clima_authors),
        "papers": papers,
        "not_found": not_found,
    }
    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    print(
        f"Wrote {out_path.relative_to(REPO_ROOT)}: "
        f"{len(papers)} papers, {len(not_found)} not found in OpenAlex."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
