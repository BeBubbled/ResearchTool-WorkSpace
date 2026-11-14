#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Titles (one per line) -> BibTeX.

Sources, in order: Crossref and Semantic Scholar (fast, rarely rate limited, title-checked),
then Google Scholar through scholarly when it is installed, and finally a minimal local
@misc entry marked for manual completion, so every title produces an entry.
"""

import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from researchtool.citations import CitationResolver, local_bibtex  # noqa: E402

try:
    from scholarly import scholarly
except ImportError:  # Google Scholar is only the third source.
    scholarly = None


def fetch_from_scholar(title: str, sleep_sec: float = 5.0) -> Optional[str]:
    if scholarly is None:
        return None
    try:
        pub = next(scholarly.search_pubs(title), None)
        if pub is None:
            return None
        bibtex_str = scholarly.bibtex(pub)
        time.sleep(sleep_sec)  # Google Scholar rate limits aggressively.
        return bibtex_str
    except Exception as exc:
        print(f"[WARN] Google Scholar failed for: {title} | {exc}", file=sys.stderr)
        return None


def fetch_bibtex_for_title(title: str, resolver: CitationResolver) -> tuple[str, str]:
    """(bibtex, source) — source is crossref, semantic_scholar, google_scholar or local."""
    found = resolver.resolve(title)
    if found["verified"]:
        return found["bibtex"], found["source"]
    scholar = fetch_from_scholar(title)
    if scholar:
        return scholar, "google_scholar"
    return local_bibtex(title), "local"


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Usage:\n"
            "  python get_bibtex.py paper_titles.txt ref.bib\n\n"
            "paper_titles.txt: 每行一个论文标题\n"
            "ref.bib: 输出的 BibTeX 文件",
            file=sys.stderr,
        )
        sys.exit(1)

    titles_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    if not titles_path.is_file():
        print(f"[FATAL] Not found: {titles_path}", file=sys.stderr)
        sys.exit(1)

    titles = [line.strip() for line in titles_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not titles:
        print("[FATAL] No titles found in input file.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Total titles: {len(titles)}", file=sys.stderr)
    import os
    resolver = CitationResolver(semantic_scholar_key=lambda: os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""))
    entries, sources = [], {}
    for idx, title in enumerate(titles, start=1):
        print(f"[INFO] ({idx}/{len(titles)}) {title}", file=sys.stderr)
        bib, source = fetch_bibtex_for_title(title, resolver)
        sources[source] = sources.get(source, 0) + 1
        level = "WARN" if source == "local" else "OK"
        print(f"[{level}] {source}: {title}", file=sys.stderr)
        entries.append(bib.strip())

    output_path.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    summary = ", ".join(f"{name} {count}" for name, count in sources.items())
    print(f"[DONE] Saved {len(entries)} entries to: {output_path} ({summary})", file=sys.stderr)
    if sources.get("local"):
        print("[WARN] Entries from 'local' were not found online; complete them by hand.", file=sys.stderr)


if __name__ == "__main__":
    main()
