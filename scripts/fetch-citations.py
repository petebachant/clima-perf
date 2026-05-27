"""Fetch CliMA-org publication + citation data from OpenAlex.

The CliMA publications page (https://clima.caltech.edu/publications/) lists
papers as free-form HTML — most have a ``doi.org`` link, but some are bare
references with only authors + year + title. We parse every entry from the
HTML (DOI or no DOI), resolve each one to an OpenAlex work (DOI lookup when
available, falling back to a title+year search), then fetch the full citing-
works graph for each CliMA paper.

The output is four normalized JSONL tables under ``data/openalex/``, joinable
on OpenAlex IDs:

    resolved-pubs.jsonl — one row per OpenAlex work we've fetched (CliMA
                         pubs and works that cite them, deduped — a paper
                         appears once even if it's both)
    clima-pubs.jsonl    — CliMA-specific extras (publications-page metadata,
                         cited_by_count, counts_by_year); openalex_id is a
                         foreign key into resolved-pubs.jsonl
    citations.jsonl     — cited_openalex_id → citing_openalex_id edges
    authors.jsonl       — one row per distinct OpenAlex author seen

We deliberately do NOT compute "is_internal" here. That filter (citing pub
shares any author with any CliMA pub) belongs downstream in the analyze
notebook, which already has the right context to apply it.

No-ops if ``data/openalex/.last_fetched`` already records yesterday's UTC
day, matching the resumable pattern used by fetch-github.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

import requests
from bs4 import BeautifulSoup, Tag

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "openalex"

TODAY_UTC = datetime.now(timezone.utc).date()
LAST_DAY = TODAY_UTC - timedelta(days=1)
COMPLETED_MARKER = OUT_DIR / ".last_fetched"

PUBLICATIONS_URL = "https://clima.caltech.edu/publications/"
OPENALEX_WORK_BY_DOI = "https://api.openalex.org/works/doi:"
OPENALEX_WORKS = "https://api.openalex.org/works"
MAILTO = os.environ.get("OPENALEX_MAILTO", "petebachant@gmail.com")
PER_PAGE = 200

# Slim OpenAlex select-lists keep payloads small for the citing-works graph,
# which dominates the bandwidth budget.
SELECT_FULL = (
    "id,doi,title,publication_year,publication_date,authorships,"
    "cited_by_count,counts_by_year,primary_location"
)
SELECT_CITING = "id,doi,publication_year,publication_date,authorships,primary_location,title"

DOI_URL_RE = re.compile(
    r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>)]+)", re.IGNORECASE
)
YEAR_COLON_RE = re.compile(r",\s*(20\d{2}|19\d{2})\s*:")


# --- HTTP helpers ----------------------------------------------------------

def get_json(url: str, params: dict | None = None, *, timeout: int = 60) -> dict:
    p = {"mailto": MAILTO, **(params or {})}
    resp = requests.get(url, params=p, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# --- Publications-page parser ----------------------------------------------

def parse_publications_page(url: str) -> list[dict]:
    """Return one record per pub entry: ``{title, year, doi, authors_text, raw}``.

    Each entry on https://clima.caltech.edu/publications/ is a ``<p>`` with a
    ``..., YYYY:`` author-year separator, an optional ``doi.org`` link, and a
    bolded or italicized title. We pull just enough text to match the work
    against OpenAlex.
    """
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pubs: list[dict] = []
    seen_titles: set[str] = set()
    for p in soup.find_all(["p", "li"]):
        text = p.get_text(separator=" ", strip=True)
        if len(text) < 80:
            continue
        m = YEAR_COLON_RE.search(text)
        if not m:
            continue
        year = int(m.group(1))

        # Title is usually the first <strong>/<b>; preprints are sometimes in
        # <i>/<em> instead. Skip if it's just a venue phrase like "submitted".
        title = None
        for tag in p.find_all(["strong", "b"]):
            t = tag.get_text(separator=" ", strip=True)
            if t and len(t) > 5 and not t.lower().startswith(("journal", "nature", "science")):
                title = t
                break
        if not title:
            for tag in p.find_all(["em", "i"]):
                t = tag.get_text(separator=" ", strip=True)
                if t and len(t) > 5 and not t.lower().startswith(("journal", "nature", "science", "submitted", "in press")):
                    title = t
                    break
        if not title:
            continue

        doi = None
        for a in p.find_all("a", href=True):
            mm = DOI_URL_RE.match(a["href"])
            if mm:
                doi = mm.group(1).rstrip(").,;]")
                break

        authors_text = text[: m.start()].rstrip(", ").strip()

        # Dedup by lowercase title — the page has occasional repeats.
        key = title.lower()[:80]
        if key in seen_titles:
            continue
        seen_titles.add(key)

        pubs.append(
            {
                "title": title,
                "year": year,
                "doi": doi.lower() if doi else None,
                "authors_text": authors_text,
                "raw": text[:400],
            }
        )
    return pubs


# --- OpenAlex resolution ---------------------------------------------------

def fetch_work_by_doi(doi: str) -> dict | None:
    try:
        return get_json(OPENALEX_WORK_BY_DOI + doi, {"select": SELECT_FULL})
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            return None
        raise


def _title_norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def fetch_work_by_title(title: str, year: int) -> dict | None:
    """Best-effort OpenAlex match using the search endpoint.

    Filter narrows to a +/-1 year window because publication dates often slip;
    we then confirm by comparing normalized titles. If no candidate's title
    closely matches, return None so the caller can record the miss.
    """
    target = _title_norm(title)
    if len(target) < 10:
        return None
    try:
        page = get_json(
            OPENALEX_WORKS,
            {
                "search": title,
                "filter": f"publication_year:{year - 1}|{year}|{year + 1}",
                "per-page": 10,
                "select": SELECT_FULL,
            },
        )
    except requests.HTTPError:
        return None
    for r in page.get("results", []):
        rt = _title_norm(r.get("title") or "")
        # Treat as a match if 30 chars match at the start, or one is a prefix
        # of the other — robust to subtle title variants.
        if not rt:
            continue
        if rt.startswith(target[:30]) or target.startswith(rt[:30]):
            return r
    return None


# --- Citing-works enumeration ---------------------------------------------

def fetch_citing_works(cited_work_id: str) -> Iterator[dict]:
    short = cited_work_id.rsplit("/", 1)[-1]
    cursor = "*"
    while cursor:
        page = get_json(
            OPENALEX_WORKS,
            {
                "filter": f"cites:{short}",
                "per-page": PER_PAGE,
                "cursor": cursor,
                "select": SELECT_CITING,
            },
        )
        yield from page.get("results", [])
        cursor = page.get("meta", {}).get("next_cursor")
        if not cursor or not page.get("results"):
            break


# --- Row builders ----------------------------------------------------------

def _venue(work: dict) -> str | None:
    return (
        ((work.get("primary_location") or {}).get("source") or {}).get(
            "display_name"
        )
    )


def _author_records(work: dict) -> tuple[list[str], list[dict]]:
    """Return (author_id_list, [author_metadata_records])."""
    ids: list[str] = []
    rows: list[dict] = []
    for a in work.get("authorships") or []:
        author = a.get("author") or {}
        aid = author.get("id")
        if not aid:
            continue
        ids.append(aid)
        rows.append(
            {
                "openalex_id": aid,
                "display_name": author.get("display_name"),
                "orcid": author.get("orcid"),
            }
        )
    return ids, rows


def _quarter(date_str: str | None) -> str | None:
    if not date_str or len(date_str) < 7:
        return None
    try:
        y, m = int(date_str[:4]), int(date_str[5:7])
    except ValueError:
        return None
    return f"{y}-Q{(m - 1) // 3 + 1}"


def resolved_pub_row(work: dict) -> dict:
    """The shared per-work metadata row. Used for both CliMA pubs and the
    works that cite them; deduped on ``openalex_id``."""
    author_ids, _ = _author_records(work)
    return {
        "openalex_id": work.get("id"),
        "doi": (work.get("doi") or "").replace("https://doi.org/", "").lower() or None,
        "title": work.get("title"),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "quarter": _quarter(work.get("publication_date"))
        or (f"{work['publication_year']}-Q1" if work.get("publication_year") else None),
        "venue": _venue(work),
        "author_ids": author_ids,
    }


def clima_pub_extras(work: dict, source: dict, resolution: str) -> dict:
    """The CliMA-specific extras keyed by ``openalex_id`` (FK to
    resolved-pubs). Carries citation counts + the link back to the
    publications-page entry the work was resolved from."""
    return {
        "openalex_id": work.get("id"),
        "cited_by_count": work.get("cited_by_count", 0),
        "counts_by_year": work.get("counts_by_year") or [],
        "publications_page_title": source.get("title"),
        "publications_page_year": source.get("year"),
        "publications_page_doi": source.get("doi"),
        "publications_page_authors": source.get("authors_text"),
        "resolution_method": resolution,  # "doi" | "title_search"
    }


# --- File helpers ----------------------------------------------------------

def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True))
            f.write("\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def read_marker(path: Path) -> date | None:
    if not path.exists():
        return None
    try:
        return date.fromisoformat(path.read_text().strip())
    except ValueError:
        return None


def write_marker(path: Path, d: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(d.isoformat() + "\n")


# --- Main ------------------------------------------------------------------

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if read_marker(COMPLETED_MARKER) == LAST_DAY:
        print(
            f"Already fetched citation data for UTC day {LAST_DAY}; nothing to do."
        )
        return 0

    # Load any prior snapshot so we can skip citing-works enumeration for
    # CliMA pubs whose ``cited_by_count`` is unchanged. The lightweight clima-
    # pub work fetch still runs every time so we always have a fresh count.
    cached_clima_extras = {
        r["openalex_id"]: r for r in read_jsonl(OUT_DIR / "clima-pubs.jsonl")
    }
    cached_resolved = {
        r["openalex_id"]: r for r in read_jsonl(OUT_DIR / "resolved-pubs.jsonl")
    }
    cached_edges_by_cited: dict[str, list[dict]] = {}
    for e in read_jsonl(OUT_DIR / "citations.jsonl"):
        cached_edges_by_cited.setdefault(e["cited_openalex_id"], []).append(e)
    cached_authors = {
        r["openalex_id"]: r for r in read_jsonl(OUT_DIR / "authors.jsonl")
    }
    print(
        f"Cache: {len(cached_clima_extras)} clima pubs, "
        f"{len(cached_resolved)} resolved pubs, "
        f"{sum(len(v) for v in cached_edges_by_cited.values())} edges, "
        f"{len(cached_authors)} authors."
    )

    print(f"Parsing publication entries from {PUBLICATIONS_URL}")
    sources = parse_publications_page(PUBLICATIONS_URL)
    print(
        f"  {len(sources)} entries "
        f"({sum(1 for s in sources if s['doi'])} with DOI, "
        f"{sum(1 for s in sources if not s['doi'])} without)"
    )

    print("Resolving CliMA pubs against OpenAlex...")
    clima_extras_rows: list[dict] = []
    new_resolved: dict[str, dict] = {}
    new_authors: dict[str, dict] = {}
    unresolved: list[dict] = []
    seen_works: set[str] = set()
    cache_hit: set[str] = set()
    cache_miss: list[dict] = []
    for src in sources:
        work: dict | None = None
        method = ""
        if src["doi"]:
            try:
                work = fetch_work_by_doi(src["doi"])
                method = "doi"
            except requests.RequestException as err:
                print(f"  WARN: DOI {src['doi']}: {err}", file=sys.stderr)
        if work is None:
            work = fetch_work_by_title(src["title"], src["year"])
            method = "title_search" if work else ""
        if work is None or not work.get("id"):
            unresolved.append(src)
            continue
        if work["id"] in seen_works:
            continue
        seen_works.add(work["id"])

        extras = clima_pub_extras(work, src, method)
        clima_extras_rows.append(extras)
        new_resolved[work["id"]] = resolved_pub_row(work)
        for arec in _author_records(work)[1]:
            new_authors.setdefault(arec["openalex_id"], arec)

        cid = work["id"]
        cached = cached_clima_extras.get(cid)
        cached_count = cached.get("cited_by_count") if cached else None
        if (
            cached_count is not None
            and cached_count == extras["cited_by_count"]
            and cid in cached_edges_by_cited
        ):
            cache_hit.add(cid)
        else:
            cache_miss.append(extras)
    print(
        f"  resolved {len(clima_extras_rows)} pubs "
        f"({len(cache_hit)} cache-hit, {len(cache_miss)} need refetch, "
        f"{len(unresolved)} unresolved)"
    )
    if unresolved:
        for u in unresolved[:5]:
            print(f"    unresolved: {u['title'][:80]!r} ({u['year']})")
        if len(unresolved) > 5:
            print(f"    ...and {len(unresolved) - 5} more")

    print(f"Enumerating citing works for {len(cache_miss)} pub(s) with new citations...")
    new_edges: list[dict] = []
    for clima in cache_miss:
        cited_id = clima["openalex_id"]
        before = len(new_edges)
        try:
            for cw in fetch_citing_works(cited_id):
                cw_id = cw.get("id")
                if not cw_id:
                    continue
                if cw_id not in new_resolved:
                    new_resolved[cw_id] = resolved_pub_row(cw)
                    for arec in _author_records(cw)[1]:
                        new_authors.setdefault(arec["openalex_id"], arec)
                new_edges.append(
                    {"cited_openalex_id": cited_id, "citing_openalex_id": cw_id}
                )
        except requests.HTTPError as err:
            print(f"  WARN: citing-works {cited_id}: {err}", file=sys.stderr)
            continue
        print(
            f"  {cited_id[-12:]}: "
            f"{len(new_edges) - before} citations "
            f"(cited_by_count={clima['cited_by_count']})"
        )

    # Stitch cache hits + new fetches into final tables.
    final_edges: list[dict] = list(new_edges)
    for cid in cache_hit:
        final_edges.extend(cached_edges_by_cited.get(cid, []))

    # resolved-pubs: every work referenced by any clima extras row OR any edge.
    final_resolved: dict[str, dict] = {}
    referenced_ids: set[str] = {r["openalex_id"] for r in clima_extras_rows}
    for e in final_edges:
        referenced_ids.add(e["cited_openalex_id"])
        referenced_ids.add(e["citing_openalex_id"])
    for wid in referenced_ids:
        if wid in new_resolved:
            final_resolved[wid] = new_resolved[wid]
        elif wid in cached_resolved:
            final_resolved[wid] = cached_resolved[wid]

    # authors: every author_id referenced by any resolved pub.
    needed_author_ids: set[str] = set()
    for r in final_resolved.values():
        needed_author_ids.update(r.get("author_ids") or [])
    final_authors: dict[str, dict] = {}
    for aid in needed_author_ids:
        if aid in new_authors:
            final_authors[aid] = new_authors[aid]
        elif aid in cached_authors:
            final_authors[aid] = cached_authors[aid]

    n_resolved = write_jsonl(OUT_DIR / "resolved-pubs.jsonl", final_resolved.values())
    n_clima = write_jsonl(OUT_DIR / "clima-pubs.jsonl", clima_extras_rows)
    n_edges = write_jsonl(OUT_DIR / "citations.jsonl", final_edges)
    n_authors = write_jsonl(OUT_DIR / "authors.jsonl", final_authors.values())
    if unresolved:
        (OUT_DIR / "unresolved-pubs.jsonl").write_text(
            "".join(json.dumps(u, sort_keys=True) + "\n" for u in unresolved)
        )
    else:
        # Clear a stale unresolved file from a previous run.
        (OUT_DIR / "unresolved-pubs.jsonl").unlink(missing_ok=True)

    write_marker(COMPLETED_MARKER, LAST_DAY)
    print(
        f"✓ Wrote {n_resolved} resolved pubs ({n_clima} clima), "
        f"{n_edges} citation edges, {n_authors} authors."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
