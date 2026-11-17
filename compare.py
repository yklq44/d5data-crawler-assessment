"""Cross-check the CSV files written by the two crawlers, matching posts by URL.

Usage: python compare.py output/mit_blogs_playwright.csv output/mit_blogs_drissionpage.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mitblogs.models import CSV_COLUMNS, REQUIRED_COLUMNS
from mitblogs.store import read_csv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Cross-check two crawler CSV files, matching posts by URL.')
    parser.add_argument('left', type=Path)
    parser.add_argument('right', type=Path)
    parser.add_argument('--examples', type=int, default=3, help='differing posts to show per column (default: 3)')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    tables = {path: read_csv(path) for path in (args.left, args.right)}
    for path, rows in tables.items():
        header = tuple(rows[0]) if rows else ()
        status = 'OK' if header[: len(REQUIRED_COLUMNS)] == REQUIRED_COLUMNS else f'unexpected: {header}'
        print(f'{path}: {len(rows)} rows, header {status}')

    left, right = ({row['URL']: row for row in rows} for rows in tables.values())
    common = sorted(left.keys() & right.keys())
    only_left, only_right = left.keys() - right.keys(), right.keys() - left.keys()
    print(
        f'\nPosts in both files: {len(common)}; only in the left one: {len(only_left)}; '
        f'only in the right one: {len(only_right)}'
    )
    for url in sorted(only_left)[: args.examples]:
        print(f'  only left:  {url}')
    for url in sorted(only_right)[: args.examples]:
        print(f'  only right: {url}')

    print('\n| Column | Identical | Different |\n|---|---:|---:|')
    differences = {}
    for column in CSV_COLUMNS:
        urls = [url for url in common if left[url].get(column) != right[url].get(column)]
        differences[column] = urls
        print(f'| {column} | {len(common) - len(urls)} | {len(urls)} |')

    for column, urls in differences.items():
        for url in urls[: args.examples]:
            a, b = left[url].get(column) or '', right[url].get(column) or ''
            print(f'\n{column} differs for {url}')
            print(f'  left:  {_excerpt(a, b)}')
            print(f'  right: {_excerpt(b, a)}')

    identical = not only_left and not only_right and not any(differences.values())
    print('\nThe two files are identical.' if identical else '')
    return 0 if identical else 1


def _excerpt(text: str, other: str, context: int = 60) -> str:
    """The part of ``text`` around its first difference with ``other``."""
    pairs = enumerate(zip(text, other, strict=False))
    index = next((i for i, (x, y) in pairs if x != y), min(len(text), len(other)))
    start = max(0, index - context)
    snippet = text[start : index + context].replace('\n', '\\n')
    return f'{"…" if start else ""}{snippet}{"…" if index + context < len(text) else ""} (length {len(text)})'


if __name__ == '__main__':
    sys.exit(main())
