# -*- coding: utf-8 -*-
"""Shared row/column title and per-cell caption support for the PPT grid scripts.

Layout (all in cm), relative to the grid origin:

    margin | row-title band | cell  gap  cell ... | margin
    top margin
    column-title band
    cell row (cell height) + caption band
    gap
    ...

Bands are only reserved when at least one corresponding label is non-empty.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import List

from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Cm, Pt


def add_label_arguments(p: argparse.ArgumentParser) -> None:
    group = p.add_argument_group("titles and captions")
    group.add_argument("--row-titles", nargs="*", default=None, help="Row titles shown left of each row (top to bottom).")
    group.add_argument("--col-titles", nargs="*", default=None, help="Column titles shown above each column (left to right).")
    group.add_argument("--captions", nargs="*", default=None, help="Per-cell captions shown below each cell (row-major).")
    group.add_argument("--labels-json", type=str, default=None,
                       help='JSON file: {"rowTitles": [...], "colTitles": [...], "captions": [...]}. Overrides the flags above.')
    group.add_argument("--row-title-width-cm", type=float, default=2.5, help="Width of the row-title band.")
    group.add_argument("--col-title-height-cm", type=float, default=1.0, help="Height of the column-title band.")
    group.add_argument("--caption-height-cm", type=float, default=0.8, help="Height of the caption band below each cell.")
    group.add_argument("--title-font-pt", type=float, default=14.0, help="Font size for row/column titles.")
    group.add_argument("--caption-font-pt", type=float, default=11.0, help="Font size for per-cell captions.")
    group.add_argument("--font-name", type=str, default="", help="Font family for labels (default: theme font).")


def _clean(values, count: int) -> List[str]:
    items = [str(v).replace("\r", "").strip() for v in (values or [])][:count]
    return items + [""] * (count - len(items))


@dataclass
class GridLabels:
    rows: int
    cols: int
    row_titles: List[str] = field(default_factory=list)
    col_titles: List[str] = field(default_factory=list)
    captions: List[str] = field(default_factory=list)
    row_title_width_cm: float = 2.5
    col_title_height_cm: float = 1.0
    caption_height_cm: float = 0.8
    title_font_pt: float = 14.0
    caption_font_pt: float = 11.0
    font_name: str = ""

    @classmethod
    def from_args(cls, args, rows: int, cols: int) -> "GridLabels":
        row_titles, col_titles, captions = args.row_titles, args.col_titles, args.captions
        if args.labels_json:
            with open(args.labels_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            row_titles = data.get("rowTitles", row_titles)
            col_titles = data.get("colTitles", col_titles)
            captions = data.get("captions", captions)
        return cls(
            rows=rows,
            cols=cols,
            row_titles=_clean(row_titles, rows),
            col_titles=_clean(col_titles, cols),
            captions=_clean(captions, rows * cols),
            row_title_width_cm=max(0.0, args.row_title_width_cm),
            col_title_height_cm=max(0.0, args.col_title_height_cm),
            caption_height_cm=max(0.0, args.caption_height_cm),
            title_font_pt=args.title_font_pt,
            caption_font_pt=args.caption_font_pt,
            font_name=args.font_name or "",
        )

    # --- band sizes (0 when unused) ---
    @property
    def left_band_cm(self) -> float:
        return self.row_title_width_cm if any(self.row_titles) else 0.0

    @property
    def top_band_cm(self) -> float:
        return self.col_title_height_cm if any(self.col_titles) else 0.0

    @property
    def caption_band_cm(self) -> float:
        return self.caption_height_cm if any(self.captions) else 0.0

    def grid_size_cm(self, cw_cm: float, ch_cm: float, gap_cm: float, margin_cm: float):
        width = 2 * margin_cm + self.left_band_cm + self.cols * cw_cm + (self.cols - 1) * gap_cm
        height = (2 * margin_cm + self.top_band_cm
                  + self.rows * (ch_cm + self.caption_band_cm) + (self.rows - 1) * gap_cm)
        return width, height

    def cell_origin_cm(self, r: int, c: int, origin_left: float, origin_top: float,
                       cw_cm: float, ch_cm: float, gap_cm: float, margin_cm: float):
        left = origin_left + margin_cm + self.left_band_cm + c * (cw_cm + gap_cm)
        top = origin_top + margin_cm + self.top_band_cm + r * (ch_cm + self.caption_band_cm + gap_cm)
        return left, top

    # --- drawing ---
    def _textbox(self, slide, text: str, left, top, width, height, size_pt: float, anchor, bold=False):
        box = slide.shapes.add_textbox(Cm(left), Cm(top), Cm(max(width, 0.1)), Cm(max(height, 0.1)))
        frame = box.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = anchor
        frame.margin_left = frame.margin_right = Cm(0.05)
        frame.margin_top = frame.margin_bottom = Cm(0.02)
        for index, line in enumerate(text.split("\n")):
            para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            para.alignment = PP_ALIGN.CENTER
            run = para.add_run()
            run.text = line
            run.font.size = Pt(size_pt)
            run.font.bold = bold
            if self.font_name:
                run.font.name = self.font_name
        return box

    def draw(self, slide, origin_left: float, origin_top: float,
             cw_cm: float, ch_cm: float, gap_cm: float, margin_cm: float) -> None:
        for c, title in enumerate(self.col_titles):
            if not title:
                continue
            left, _ = self.cell_origin_cm(0, c, origin_left, origin_top, cw_cm, ch_cm, gap_cm, margin_cm)
            self._textbox(slide, title, left, origin_top + margin_cm, cw_cm, self.top_band_cm,
                          self.title_font_pt, MSO_ANCHOR.BOTTOM, bold=True)
        for r, title in enumerate(self.row_titles):
            if not title:
                continue
            _, top = self.cell_origin_cm(r, 0, origin_left, origin_top, cw_cm, ch_cm, gap_cm, margin_cm)
            self._textbox(slide, title, origin_left + margin_cm, top, self.left_band_cm, ch_cm,
                          self.title_font_pt, MSO_ANCHOR.MIDDLE, bold=True)
        for index, caption in enumerate(self.captions):
            if not caption:
                continue
            r, c = divmod(index, self.cols)
            left, top = self.cell_origin_cm(r, c, origin_left, origin_top, cw_cm, ch_cm, gap_cm, margin_cm)
            self._textbox(slide, caption, left, top + ch_cm, cw_cm, self.caption_band_cm,
                          self.caption_font_pt, MSO_ANCHOR.TOP)
