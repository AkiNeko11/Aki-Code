"""Command-line interface for PaperTrail Link Check."""

import argparse

from .checker import check_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local Markdown links.")
    parser.add_argument("documents", nargs="+", help="Markdown files to inspect")
    args = parser.parse_args()
    checked = missing = 0
    for document in args.documents:
        try:
            results = check_file(document)
        except (OSError, UnicodeError) as error:
            print(f"[ERROR] {document}: {error}")
            return 2
        for result in results:
            status = "OK" if result.exists else "MISSING"
            print(f"[{status}] {document}:{result.line} -> {result.target}")
            checked += 1
            missing += not result.exists
    print(f"Checked {checked} local links; {missing} missing.")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
