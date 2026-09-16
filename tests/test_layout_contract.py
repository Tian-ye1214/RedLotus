"""Enforce the user's source-size contract without hiding executable code."""

import json
import ast
import io
import tokenize
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "src" / "redlotus"
BOARDS = {"core", "tools", "api", "prompts", "memory"}


def effective_code_lines(source):
    """Count logical statements; ignore comments, blank lines and documentation strings."""
    tree = ast.parse(source)
    docs = {
        node.body[0] for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    doc_ends = {node.end_lineno for node in docs}
    logical = sum(
        token.type == tokenize.NEWLINE and token.start[0] not in doc_ends
        or token.type == tokenize.OP and token.string == ";"
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
    )
    # Keep one-line compound suites from hiding several executable statements.
    statements = sum(isinstance(node, ast.stmt) and node not in docs for node in ast.walk(tree))
    return max(logical, statements)


class EffectiveLineTests(unittest.TestCase):
    def test_parameters_and_continuations_are_one_logical_line(self):
        self.assertEqual(effective_code_lines("def f(\n    a,\n    b,\n):\n    return (\n        a + b\n    )\n"), 2)

    def test_comments_and_documentation_are_not_code(self):
        self.assertEqual(effective_code_lines('"""module docs"""\n# comment\n\ndef f():\n    """Explain the function."""\n    return 1 # explanation\n'), 2)

    def test_condensing_statements_does_not_reduce_the_count(self):
        self.assertEqual(effective_code_lines("def f(): a = 1; return a\n"), 3)


def application_files(directory):
    manifest = ROOT / "tools" / "third_party_skills.json"
    resources = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    excluded = [ROOT / "tools" / "skills" / name for name in resources]
    return [
        path for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
        and not any(path.is_relative_to(resource) for resource in excluded)
    ]


class SourceLayoutTests(unittest.TestCase):
    def test_only_the_five_functional_boards_exist(self):
        actual = {path.name for path in ROOT.iterdir() if path.is_dir() and path.name != "__pycache__"}
        self.assertEqual(actual, BOARDS)

    def test_each_board_contains_at_most_five_python_files(self):
        oversized = {
            board: len(application_files(ROOT / board))
            for board in BOARDS if len(application_files(ROOT / board)) > 5
        }
        self.assertEqual(oversized, {})

    def test_each_application_file_has_at_most_500_effective_lines(self):
        oversized = {}
        for path in application_files(ROOT):
            count = effective_code_lines(path.read_text(encoding="utf-8-sig"))
            if count > 500:
                oversized[str(path.relative_to(ROOT))] = count
        self.assertEqual(oversized, {})

    def test_package_root_does_not_hide_application_modules(self):
        self.assertEqual([path.name for path in ROOT.glob("*.py") if path.name != "__init__.py"], [])


if __name__ == "__main__":
    unittest.main()
