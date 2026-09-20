"""Guard the responsibility boundaries and reviewable implementation size."""

import ast
from collections import defaultdict
from pathlib import Path
from graphlib import TopologicalSorter


def test_model_module_initialization_is_acyclic():
    root = Path(__file__).resolve().parents[1] / "src" / "redlotus" / "models"
    trees = {
        f"redlotus.models.{path.stem}": ast.parse(path.read_text(encoding="utf-8"))
        for path in root.glob("*.py")
    }
    dependencies = {}
    for module, tree in trees.items():
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        dependencies[module] = imported & trees.keys()
    tuple(TopologicalSorter(dependencies).static_order())


def test_application_packages_keep_size_and_dependency_boundaries():
    root = Path(__file__).resolve().parents[1] / "src" / "redlotus"
    packages = defaultdict(list)
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if relative.parts[:2] == ("tools", "skills"):
            continue  # Packaged standalone Skill scripts are not application modules.
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        packages[relative.parts[0]].append(path)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr):
                    value = node.body[0].value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        docstrings.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            if relative.parts[0] in {"core", "runtime", "models", "storage", "documents", "tools", "memory"}:
                assert not any(module.startswith(("redlotus.terminal", "redlotus.presentation", "redlotus.api")) for module in modules), relative
            if relative.parts[0] in {"tools", "memory"}:
                assert not any(module.startswith("redlotus.core") for module in modules), relative
        effective = sum(
            bool(line.strip()) and not line.lstrip().startswith("#") and index not in docstrings
            for index, line in enumerate(source.splitlines(), 1)
        )
        assert effective <= 500, (str(relative), effective)
    assert all(len(files) <= 5 for files in packages.values()), packages
    worker = ast.parse((root / "tools" / "worker_tools.py").read_text(encoding="utf-8"))
    implementation = next(node for node in worker.body if isinstance(node, ast.ClassDef) and node.name == "WorkerOrchestrator")
    assert any(isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute" for node in implementation.body)
