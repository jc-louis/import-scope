import ast
import importlib.util
import subprocess
import sys
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path


# Modules whose imports should be ignored
_IGNORE_MODULES: list[str] = []

# Imports that should be ignored across the whole project
_IGNORE_IMPORTS = {
    "shared.apps.fhir_decorators.models",  # Undirect import is deliberate
    "training.constants",  # Constants which are actually not constants
    "apps.com_apicrypt_text_extract.constants",  # Constants which are actually not constants
    "apps.clinical_data.db.base",  # SQL Alchemy manipulations ?
    "shared.clinical_data.db.base",  # SQL Alchemy manipulations ?
    "apps.irene.predictions.address.tours_other_pages",  # Messy mock imports
    "apps.irene.predictions.address.prepare_choose_recipients",  # Messy mock imports
    "apps.irene.predictions.predict",  # Messy mock imports
}



try:
    _POETRY_VENV_LOCATION = Path(
        subprocess.run(
            ["poetry", "env", "info", "-p"], capture_output=True, text=True, check=True
        ).stdout.strip()
    ).resolve()
except Exception as err:
    print(f"Unable to find the current interpreter location: {err}")
    _POETRY_VENV_LOCATION = Path("/")


@lru_cache(maxsize=10000)
def _get_module_exports(module_path: Path) -> set[str]:
    """Get all exported names from a module using AST parsing.

    If an explicit __all__ is not provided, considers:
    - Any locally defined function, class, variable
    - Re-exports of global variables, iff they are aliased

    Note: This uses ast.walk() to find all definitions, which is needed for the indirect
    imports checker. For top-level exports only, use _get_module_top_level_exports().
    """
    try:
        with module_path.open(encoding="utf-8") as file:
            tree = ast.parse(file.read())
    except (SyntaxError, UnicodeDecodeError):
        return set()

    exports = set()

    # Check for explicit __all__ definition
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exports.add(elt.value)
                    return exports  # If __all__ is defined, use only that

    # If no __all__, collect all top-level definitions
    for node in ast.walk(tree):
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










@lru_cache(maxsize=10000)
def _module_path_from_name(module_name: str, root: Path) -> Path | str:
    """Convert a python import name (dot separated) to a file path or to an external
    library name.
    """
    parts = module_name.split(".")

    # Try as a file
    file_path = root / "/".join(parts[:-1]) / f"{parts[-1]}.py"
    if file_path.exists():
        return file_path

    # Try as a package
    package_path = root / "/".join(parts) / "__init__.py"
    if package_path.exists():
        return package_path

    # Try as standard lib
    if parts[0] in sys.stdlib_module_names:
        return parts[0]

    # Check if defined outside project
    try:
        spec = importlib.util.find_spec(parts[0])
        if (
            spec is not None
            and spec.origin is not None
            and not set(Path(spec.origin).parents).intersection({root, _POETRY_VENV_LOCATION})
        ):
            return Path(spec.origin)
    except (ImportError, ValueError, ModuleNotFoundError) as err:
        print(err)

    # By default, consider the module is a declared dependency (we have to do this, because some
    # poetry groups could not be currently installed and thus importlib would not be able to find
    # them even though they are allowed imports)
    return parts[0]


def _find_definition_file(module_name: str, import_name: str, root: Path) -> Path | str:
    """Return the file where the import is defined or the name of its original library."""
    if isinstance(
        submodule_path := _module_path_from_name(f"{module_name}.{import_name}", root), Path
    ):
        # The import_name is a locally defined submodule, definition file found
        return submodule_path

    module_path = _module_path_from_name(module_name, root)
    if not isinstance(module_path, Path):
        # The module_name is a library, dont investigate further
        return module_path

    exports = _get_module_exports(module_path)
    if import_name in exports:
        # The import_name is exported by a local module, definition file found
        return module_path

    parent_module = next(
        (module for module, names in _parse_imports(module_path) if import_name in names), None
    )
    if parent_module is not None:
        # The import_name is reexported by a local module, looking for definition file recursively
        return _find_definition_file(parent_module, import_name, root)

    raise ValueError(f"Found no definition nor import for name {import_name} in {module_path}")


def check_indirect_imports(root: Path) -> None:
    """Check all imports in the project to see if they are defined where they are imported from."""

    n_files, n_imports = 0, 0
    logs = []

    files_to_skip_raw = [
        _module_path_from_name(module_name, root) for module_name in _IGNORE_MODULES
    ]
    if incorrect := [str(name) for name in files_to_skip_raw]:
        raise ValueError(
            f"_IGNORE_MODULES should only contain local modules and submodules. "
            f"Found {', '.join(incorrect)} which could not be resolved to local modules."
        )
    files_to_skip = {
        path.parent if path.name == "__init__.py" else path
        for path in files_to_skip_raw
        if isinstance(path, Path)
    }

    for file in _find_python_files(root):
        if file in files_to_skip or files_to_skip.intersection(file.parents):
            continue

        n_files += 1
        file_logs = []
        try:
            for module_name, imports in _parse_imports(file):
                for import_name in imports:
                    if (
                        module_name in _IGNORE_IMPORTS
                        or f"{module_name}.{import_name}" in _IGNORE_IMPORTS
                    ):
                        continue

                    definition_file = _find_definition_file(module_name, import_name, root)
                    n_imports += 1

                    if isinstance(definition_file, str):
                        if module_name.split(".")[0] != definition_file:
                            file_logs.append(
                                f"Import should be direct: {import_name} imported from {module_name} "
                                f"instead of {definition_file}"
                            )
                    elif root not in definition_file.parents:
                        file_logs.append(
                            f"{import_name} imported from {definition_file!s} which is outside "
                            f"the current project"
                        )
                    else:
                        if definition_file.name == "__init__.py":
                            definition_file = definition_file.parent
                        relative_path = (
                            str(definition_file.relative_to(root))
                            .replace("/", ".")
                            .removesuffix(".py")
                        )
                        module_path = f"{module_name}.{import_name}"
                        if relative_path not in {module_name, module_path}:
                            file_logs.append(
                                f"Import should be direct: {import_name} imported from {module_name} instead of {relative_path}"
                            )

        except Exception as e:
            file_logs.append(f"error checking {import_name} in file {file}\n{e!s}")

        if file_logs:
            logs.append(f"{file}:1\n" + "\n".join(file_logs) + "\n")

    print(f"Checked {n_files:,} files and {n_imports:,} imports")
    if logs:
        print("Found invalid python imports:\n\n" + "\n".join(logs))
        sys.exit(1)
    print("Check imports passed!")




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


