import ast
import sys
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path



# Modules/folders to ignore when checking for unimported symbols (prefix matching)
_IGNORE_UNIMPORTED_PUBLIC_IN_MODULES: set[str] = {
    "apps.watson.alembic",
    "apps.watson.modes.dashboards",
    "apps.irene.alembic",
    "apps.clinical_data.alembic",
    "apps.clinical_data",
    "tests",
    "tasks",
    "training.labs",
    "training.kubeflow.ci",
    "no_git",
}

# Specific symbols to ignore in the unimported symbols check (format: "module.symbol" or just "symbol")
_IGNORE_UNIMPORTED_PUBLIC_SYMBOLS: set[str] = {
    "apps.com_apicrypt_text_extract.constants.LIFEN_BEARER",
    "apps.irene.web.main.app",
    "shared.irene_utils.pseudonymisation.local_pseudo.pseudo_pdf",
}

# Symbol types to check for unimported symbols
# Available options: "functions", "classes", "variables"
# Set to empty set to check all types
_CHECK_SYMBOL_TYPES: dict[str, bool] = {
    "function": True,
    "variable": True,
    "class": False,
    "type_aliases": False,
    "": False,  # import alias
}





@lru_cache(maxsize=10000)
def _get_module_top_level_exports(module_path: Path) -> set[str]:
    """Get only top-level exported names from a module (not nested class members, etc.).

    This is used by the unimported symbols checker to avoid reporting enum members
    and other nested definitions as unimported.
    """
    try:
        with module_path.open(encoding="utf-8") as file:
            tree = ast.parse(file.read())
    except (SyntaxError, UnicodeDecodeError):
        return set()

    exports = set()

    # Check for explicit __all__ definition (only at top level)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exports.add(elt.value)
                    return exports  # If __all__ is defined, use only that

    # If no __all__, collect all top-level definitions (not nested in classes/functions)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exports.add(node.name)
        elif isinstance(node, ast.Assign):
            # Handle regular assignments: x = 1, a, b = 1, 2
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exports.add(target.id)
                elif isinstance(target, ast.Tuple):
                    # Handle tuple unpacking: a, b = 1, 2
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            exports.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # Handle annotated assignments: x: int = 1
            exports.add(node.target.id)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            # Handle re-exports of aliased global variables only
            for alias in node.names:
                if (name := alias.asname) is not None and name.isupper():
                    exports.add(name)
        elif isinstance(node, ast.TypeAlias):
            # Handle type aliases
            exports.add(node.name.id)

    return exports


def _get_public_symbols(module_path: Path) -> set[str]:
    """Get all public symbols (not starting with _) from a module."""
    exports = _get_module_top_level_exports(module_path)
    return {name for name in exports if not name.startswith("_")}


def _get_symbol_types(module_path: Path) -> dict[str, str]:
    """Get a mapping of symbol names to their types (function, class, or variable)."""
    try:
        with module_path.open(encoding="utf-8") as file:
            tree = ast.parse(file.read())
    except (SyntaxError, UnicodeDecodeError):
        return {}

    symbol_types = {}

    # Only look at top-level definitions
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol_types[node.name] = "function"
        elif isinstance(node, ast.ClassDef):
            symbol_types[node.name] = "class"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbol_types[target.id] = "variable"
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            symbol_types[elt.id] = "variable"
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbol_types[node.target.id] = "variable"
        elif isinstance(node, ast.TypeAlias):
            symbol_types[node.name.id] = "type_aliases"

    return symbol_types


def _get_functions_with_decorators(module_path: Path) -> set[str]:
    """Get all function names that have decorators."""
    try:
        with module_path.open(encoding="utf-8") as file:
            tree = ast.parse(file.read())
    except (SyntaxError, UnicodeDecodeError):
        return set()

    functions_with_decorators = set()

    # Only look at top-level definitions
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            functions_with_decorators.add(node.name)

    return functions_with_decorators


def _file_has_main_block(file_path: Path) -> bool:
    """Check if a file has an `if __name__ == "__main__":` block."""
    try:
        with file_path.open(encoding="utf-8") as file:
            tree = ast.parse(file.read())
    except (SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            # Check for `if __name__ == "__main__":`
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
            ):
                left = test.left
                comparator = test.comparators[0] if test.comparators else None
                if (
                    isinstance(left, ast.Name)
                    and left.id == "__name__"
                    and isinstance(comparator, ast.Constant)
                    and comparator.value == "__main__"
                ):
                    return True
    return False


def _path_to_module_name(file_path: Path, root: Path) -> str:
    """Convert a file path to a Python module name."""
    relative = file_path.relative_to(root)
    if relative.name == "__init__.py":
        return str(relative.parent).replace("/", ".")
    return str(relative).replace("/", ".").removesuffix(".py")

def _find_python_files(root: Path) -> Iterator[Path]:
    """Recursively find all Python files in the root directory."""

    ignore_dirs = {"node_modules"}
    for path in root.iterdir():
        if path.is_file() and path.suffix == ".py":
            yield path
        if path.is_dir() and not path.name.startswith(".") and path.name not in ignore_dirs:
            yield from path.rglob("*.py")

def _parse_imports(file_path: Path) -> Iterator[tuple[str, list[str]]]:
    """Parse the imports from a Python file using the AST module."""
    with file_path.open(encoding="utf-8") as file:
        tree = ast.parse(file.read())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, [alias.name]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.module, [alias.name for alias in node.names]


def check_unimported_public_symbols(root: Path) -> None:  # noqa: PLR0914
    """Find all public symbols that are never imported anywhere in the project."""

    # Step 1: Collect all public symbols from each file, filtered by type
    all_symbols: dict[Path, set[str]] = {}
    for file in _find_python_files(root):
        module_name = _path_to_module_name(file, root)

        # Skip ignored modules (prefix matching)
        if any(
            module_name == ignored or module_name.startswith(f"{ignored}.")
            for ignored in _IGNORE_UNIMPORTED_PUBLIC_IN_MODULES
        ):
            continue

        public_symbols = _get_public_symbols(file)

        # Filter by symbol types if specified
        if _CHECK_SYMBOL_TYPES:
            symbol_types = _get_symbol_types(file)
            public_symbols = {
                name for name in public_symbols if _CHECK_SYMBOL_TYPES[symbol_types.get(name, "")]
            }

        if public_symbols:
            all_symbols[file] = public_symbols

    # Step 2: Collect all imports across the project
    imported_symbols: dict[str, set[str]] = {}  # module_name -> set of imported names
    for file in _find_python_files(root):
        for module_name, imports in _parse_imports(file):
            if module_name not in imported_symbols:
                imported_symbols[module_name] = set()
            imported_symbols[module_name].update(imports)

    # Step 3: Find unimported symbols
    logs = []
    n_files, n_symbols, n_unimported = 0, 0, 0

    for file, symbols in all_symbols.items():
        n_files += 1
        module_name = _path_to_module_name(file, root)
        file_unimported = []

        # Get what's imported from this module and its parent packages
        module_imports = imported_symbols.get(module_name, set())

        # Also check parent __init__.py imports (for re-exports)
        parts = module_name.split(".")
        for i in range(len(parts)):
            parent_module = ".".join(parts[: i + 1])
            module_imports.update(imported_symbols.get(parent_module, set()))

        # Get functions with decorators to skip them
        functions_with_decorators = _get_functions_with_decorators(file)

        for symbol in sorted(symbols):
            n_symbols += 1

            # Skip if in ignore list
            full_name = f"{module_name}.{symbol}"
            if (
                symbol in _IGNORE_UNIMPORTED_PUBLIC_SYMBOLS
                or full_name in _IGNORE_UNIMPORTED_PUBLIC_SYMBOLS
            ):
                continue

            # Skip functions with decorators (they may be used implicitly)
            if symbol in functions_with_decorators:
                continue

            # Check if symbol is imported anywhere
            if symbol not in module_imports:
                # Special case: check if this is a script entry point
                if symbol == "main" and _file_has_main_block(file):
                    continue

                file_unimported.append(symbol)
                n_unimported += 1

        if file_unimported:
            logs.append(f"{file}:1\n{', '.join(file_unimported)}\n")

    checked_types = ", ".join(sorted(_CHECK_SYMBOL_TYPES)) if _CHECK_SYMBOL_TYPES else "all types"
    print(f"Checked {n_files:,} files and {n_symbols:,} public symbols ({checked_types})")
    if logs:
        print("Found unimported public symbols:\n\n" + "\n".join(logs))
        n_files_with_errors = len(logs)
        print(
            f"Total: {n_unimported:,} unimported public symbol(s) in {n_files_with_errors:,} file(s), "
            "should add underscore _ in front of them to mark them as private."
        )
        sys.exit(1)
    print("Check unimported symbols passed!")
