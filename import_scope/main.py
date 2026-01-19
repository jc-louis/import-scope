from pathlib import Path

from import_scope.indirect_imports import check_indirect_imports
from import_scope.unimported_public_symbols import check_unimported_public_symbols

import argparse

def main():
    parser = argparse.ArgumentParser(description="Python import linters")
    parser.add_argument(
        "check",
        nargs="?",
        default="all",
        choices=["indirect", "unimported_public", "all"],
        help="Which check to run: 'indirect' (default), 'unimported_public', or 'all'",
    )
    args = parser.parse_args()

    root = Path.cwd()
    if args.check in {"indirect", "all"}:
        check_indirect_imports(root)
    if args.check in {"unimported_public", "all"}:
        check_unimported_public_symbols(root)
