"""Export two spreadsheet columns as Anki-importable cards."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - only exercised without deps
    raise SystemExit(
        "Missing dependency: pandas. Run .\\run_sheet_to_anki.ps1 so dependencies "
        "are installed into the project .venv."
    ) from exc


class SheetToAnkiError(Exception):
    """User-facing error for invalid input or options."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export an Excel or CSV file to an Anki-importable tab-separated "
            "text file."
        )
    )
    parser.add_argument("input", type=Path, help="Input .xlsx or .csv file.")
    parser.add_argument(
        "--sheet",
        help=(
            "Excel sheet name to read for both front and back columns. Defaults to "
            "the first sheet for .xlsx."
        ),
    )
    parser.add_argument(
        "--front-sheet",
        help="Excel sheet name to read for card fronts. Overrides --sheet.",
    )
    parser.add_argument(
        "--back-sheet",
        help="Excel sheet name to read for card backs. Overrides --sheet.",
    )
    parser.add_argument("--front", required=True, help="Column name for card fronts.")
    parser.add_argument("--back", required=True, help="Column name for card backs.")
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        type=Path,
        help="Output .txt file for Anki import.",
    )
    return parser.parse_args(argv)


def read_table(input_path: Path, sheet_name: str | None) -> pd.DataFrame:
    if not input_path.exists():
        raise SheetToAnkiError(f"Input file does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        if sheet_name:
            print("Warning: --sheet is ignored for CSV input.", file=sys.stderr)
        return pd.read_csv(input_path, dtype=object, keep_default_na=False)

    if suffix == ".txt":
        if sheet_name:
            print("Warning: --sheet is ignored for TXT input.", file=sys.stderr)
        return read_txt_table(input_path)

    if suffix in {".xlsx", ".xlsm", ".xls"}:
        engine = "xlrd" if suffix == ".xls" else "openpyxl"
        try:
            workbook = pd.ExcelFile(input_path, engine=engine)
        except ImportError as exc:
            raise SheetToAnkiError(
                f"Reading {suffix} files requires {engine}. Install dependencies with: "
                ".\\run_sheet_to_anki.ps1 or .\\run_web_panel.ps1."
            ) from exc

        target_sheet = sheet_name or workbook.sheet_names[0]
        if target_sheet not in workbook.sheet_names:
            available = ", ".join(workbook.sheet_names)
            raise SheetToAnkiError(
                f"Sheet not found: {target_sheet}\nAvailable sheets: {available}"
            )
        return pd.read_excel(
            workbook,
            sheet_name=target_sheet,
            dtype=object,
            keep_default_na=False,
        )

    raise SheetToAnkiError(
        f"Unsupported input file type: {suffix or '(none)'}. "
        "Use .xlsx, .xlsm, .xls, .csv, or .txt."
    )


def read_txt_table(input_path: Path) -> pd.DataFrame:
    text, encoding = read_text_file(input_path)
    sample = "\n".join(text.splitlines()[:20])
    delimiter = detect_txt_delimiter(sample)

    if not delimiter:
        raise SheetToAnkiError(
            "TXT input must be a delimited table. Supported delimiters: tab, comma, "
            "semicolon, or pipe."
        )

    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = True

    read_kwargs = {
        "sep": delimiter,
        "dtype": object,
        "keep_default_na": False,
        "engine": "python",
        "encoding": encoding,
    }
    if has_header:
        return pd.read_csv(input_path, **read_kwargs)

    df = pd.read_csv(input_path, header=None, **read_kwargs)
    df.columns = [f"Column {index + 1}" for index in range(len(df.columns))]
    return df


def read_text_file(input_path: Path) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return input_path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise SheetToAnkiError("Could not decode TXT input. Use UTF-8 or GBK encoding.")


def detect_txt_delimiter(sample: str) -> str | None:
    delimiters = ["\t", ",", ";", "|"]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=delimiters)
        return dialect.delimiter
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in delimiters}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        return delimiter if count else None


def require_columns(df: pd.DataFrame, *columns: str) -> None:
    missing = [name for name in dict.fromkeys(columns) if name not in df.columns]
    if not missing:
        return

    available = ", ".join(str(column) for column in df.columns)
    raise SheetToAnkiError(
        f"Missing column(s): {', '.join(missing)}\nAvailable columns: {available}"
    )


def clean_cell(value: object) -> str:
    if pd.isna(value):
        return ""

    text = str(value).strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ")
    return "<br>".join(text.split("\n"))


def write_anki_cards(
    df: pd.DataFrame,
    front_column: str,
    back_column: str,
    output_path: Path,
) -> int:
    require_columns(df, front_column, back_column)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
        for _, row in df.iterrows():
            front = clean_cell(row[front_column])
            back = clean_cell(row[back_column])
            if not front or not back:
                continue

            output_file.write(f"{front}\t{back}\n")
            written += 1

    return written


def write_anki_cards_from_tables(
    front_df: pd.DataFrame,
    back_df: pd.DataFrame,
    front_column: str,
    back_column: str,
    output_path: Path,
) -> int:
    require_columns(front_df, front_column)
    require_columns(back_df, back_column)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    row_count = min(len(front_df), len(back_df))
    with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
        for index in range(row_count):
            front = clean_cell(front_df.iloc[index][front_column])
            back = clean_cell(back_df.iloc[index][back_column])
            if not front or not back:
                continue

            output_file.write(f"{front}\t{back}\n")
            written += 1

    return written


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        front_sheet = args.front_sheet or args.sheet
        back_sheet = args.back_sheet or args.sheet
        if front_sheet == back_sheet:
            df = read_table(args.input, front_sheet)
            count = write_anki_cards(df, args.front, args.back, args.output)
        else:
            front_df = read_table(args.input, front_sheet)
            back_df = read_table(args.input, back_sheet)
            count = write_anki_cards_from_tables(
                front_df,
                back_df,
                args.front,
                args.back,
                args.output,
            )
    except SheetToAnkiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {count} card(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
