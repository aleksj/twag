#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib

from twag_clickhouse.terminal_assets import build_terminal_static


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the static TWAG browser terminal shell with hashed asset URLs."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=pathlib.Path,
        help="Directory to write index.html, app.js, and styles.css into.",
    )
    parser.add_argument(
        "--asset-base",
        default=".",
        help="Asset URL base in generated index.html. Use '.' for same-directory assets.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the output directory before writing the static terminal files.",
    )
    args = parser.parse_args()

    build_terminal_static(args.output, asset_base=args.asset_base, clean=args.clean)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
