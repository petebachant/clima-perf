"""Fetch CliMA blog posts from the clima.caltech.edu WordPress API.

Writes ``data/blog/posts.jsonl``, one row per post, rewritten each run (the
whole blog is a few dozen posts). Post bodies aren't kept, only what the
analysis needs:

- ``featured_packages``: packages named in the title, e.g., "ClimaCore.jl
  and ClimaTimeSteppers.jl" gives both. The series that started in July
  2026 names each post's package this way.
- ``linked_repos``: CliMA GitHub repos the post links to, which includes
  passing mentions, so it's broader than the featured packages.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "blog" / "posts.jsonl"

API = "https://clima.caltech.edu/wp-json/wp/v2"
FIELDS = "id,date_gmt,modified_gmt,link,slug,title,categories,content"

PACKAGE_RE = re.compile(r"\b([A-Z][A-Za-z0-9]+)\.jl\b")
REPO_LINK_RE = re.compile(
    r"github\.com/CliMA/([A-Za-z0-9_.-]+?)(?:\.jl)?(?:\.git)?[\"/#?]"
)


def get_json(url: str) -> tuple[object, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "clima-perf"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp), dict(resp.headers)


def main() -> int:
    cats, _ = get_json(f"{API}/categories?per_page=100&_fields=id,name")
    cat_names = {c["id"]: c["name"] for c in cats}

    posts, page, n_pages = [], 1, 1
    while page <= n_pages:
        batch, headers = get_json(
            f"{API}/posts?per_page=100&page={page}&_fields={FIELDS}"
        )
        n_pages = int(headers.get("X-WP-TotalPages", 1))
        posts.extend(batch)
        page += 1
    if not posts:
        # The blog has dozens of posts, so none means the API moved or
        # broke. Keep the last good file instead of overwriting it.
        print("ERROR: the blog API returned no posts", file=sys.stderr)
        return 1

    rows = []
    for p in posts:
        title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"]))
        content = p["content"]["rendered"]
        rows.append(
            {
                "id": p["id"],
                "date": p["date_gmt"][:10],
                "modified": p["modified_gmt"][:10],
                "title": title,
                "link": p["link"],
                "slug": p["slug"],
                "categories": [
                    cat_names.get(c, str(c)) for c in p["categories"]
                ],
                "featured_packages": list(
                    dict.fromkeys(PACKAGE_RE.findall(title))
                ),
                "linked_repos": sorted(set(REPO_LINK_RE.findall(content))),
            }
        )
    rows.sort(key=lambda r: (r["date"], r["id"]))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    n_featured = sum(bool(r["featured_packages"]) for r in rows)
    print(f"✓ {len(rows)} posts, {n_featured} featuring a package by name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
