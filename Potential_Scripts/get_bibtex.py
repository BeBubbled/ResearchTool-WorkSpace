#!/usr/bin/env python
# -*- coding: utf-8 -*-

import time
import sys
from pathlib import Path
from typing import Optional

from scholarly import scholarly


def fetch_bibtex_for_title(title: str, sleep_sec: float = 5.0) -> Optional[str]:
    """
    给定论文标题，使用 scholarly 搜索并返回 BibTeX。
    如果失败或没有结果，返回 None。
    """
    title = title.strip()
    if not title:
        return None

    print(f"\n[INFO] Searching: {title}", file=sys.stderr)
    try:
        search_iter = scholarly.search_pubs(title)
        pub = next(search_iter, None)
        if pub is None:
            print(f"[WARN] No result for: {title}", file=sys.stderr)
            return None

        bibtex_str = scholarly.bibtex(pub)
        print(f"[OK] Got BibTeX for: {title}", file=sys.stderr)

        # 简单限速，避免被 Google Scholar 封
        time.sleep(sleep_sec)
        return bibtex_str

    except Exception as e:
        print(f"[ERROR] Failed for title: {title} | {e}", file=sys.stderr)
        return None


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

    titles = [
        line.strip()
        for line in titles_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if not titles:
        print("[FATAL] No titles found in input file.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Total titles: {len(titles)}", file=sys.stderr)

    bibtex_list = []
    for idx, title in enumerate(titles, start=1):
        print(f"[INFO] ({idx}/{len(titles)}) Processing", file=sys.stderr)
        bib = fetch_bibtex_for_title(title)
        if bib:
            bibtex_list.append(bib)

    if not bibtex_list:
        print("[WARN] No BibTeX entries collected.", file=sys.stderr)
        sys.exit(0)

    output_text = "\n\n".join(bibtex_list)
    output_path.write_text(output_text, encoding="utf-8")
    print(f"[DONE] Saved {len(bibtex_list)} entries to: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()