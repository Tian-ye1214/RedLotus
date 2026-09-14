"""Inspect known process operations without treating business text as code."""

import ast
import io
import re
import tokenize
from pathlib import Path


def read_script(path: Path) -> str:
    data = path.read_bytes()
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        encoding = (
            tokenize.detect_encoding(io.BytesIO(data).readline)[0]
            if path.suffix.lower() in {".py", ".pyw"}
            else "utf-8-sig"
        )
        return data.decode(encoding)
    except (UnicodeError, SyntaxError, LookupError) as exc:
        raise ValueError(f"Cannot decode script '{path}': {exc}") from exc


def code_without_literals(source: str) -> str:
    """Mask ordinary strings/comments before checking known non-Python APIs."""
    return re.sub(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|/\*[\s\S]*?\*/|//[^\n]*|\#[^\n]*""",
        " ",
        source,
    )


class PythonCommandCheck(ast.NodeVisitor):
    def __init__(self, policy, inspect_command, *, restricted):
        self.policy = policy
        self.inspect_command = inspect_command
        self.restricted = restricted
        self.names = {}
        self.values = {}
        self.unwaited = set()

    def check(self, source: str) -> None:
        tree = ast.parse(source)
        self.visit(tree)
        if self.unwaited:
            raise PermissionError(
                "Background process launch requires an explicit wait or communicate in the same script."
            )

    def resolve(self, node):
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call):
            return self.resolve(node.func)
        return None

    def literal(self, node):
        if isinstance(node, ast.Name):
            return self.values.get(node.id)
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    def visit_Import(self, node):
        for alias in node.names:
            self.names[alias.asname or alias.name.split(".")[0]] = alias.name

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_Assign(self, node):
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.names.pop(target.id, None)
                self.values.pop(target.id, None)
                resolved = self.resolve(node.value)
                if resolved:
                    self.names[target.id] = resolved
                if isinstance(node.value, ast.Call) and resolved == "subprocess.Popen":
                    self.unwaited.discard(id(node.value))
                    self.unwaited.add(target.id)
                value = self.literal(node.value)
                if value is not None:
                    self.values[target.id] = value

    def visit_Call(self, node):
        self.generic_visit(node)
        name = self.resolve(node.func)
        if self.restricted and name in self.policy["blocked_python_calls"]:
            raise PermissionError(
                f"Permission denied: restricted process operation {name}."
            )
        if name in self.policy["command_wrappers"]:
            argument = (
                node.args[0]
                if node.args
                else next(
                    (
                        item.value
                        for item in node.keywords
                        if item.arg in {"args", "command", "cmd"}
                    ),
                    None,
                )
            )
            command = self.literal(argument)
            if isinstance(command, (str, list, tuple)):
                self.inspect_command(command)
        if name == "subprocess.Popen":
            self.unwaited.add(id(node))
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "wait",
            "communicate",
            "__exit__",
        }:
            owner = node.func.value
            if (
                isinstance(owner, ast.Call)
                and self.resolve(owner.func) == "subprocess.Popen"
            ):
                self.unwaited.discard(id(owner))
            elif isinstance(owner, ast.Name):
                self.unwaited.discard(owner.id)

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if self.resolve(item.context_expr) == "subprocess.Popen":
                self.unwaited.discard(id(item.context_expr))
                if isinstance(item.optional_vars, ast.Name):
                    self.names[item.optional_vars.id] = "subprocess.Popen"
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node):
        names, values, unwaited = self.names.copy(), self.values.copy(), self.unwaited
        self.unwaited = set()
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            self.names.pop(arg.arg, None)
            self.values.pop(arg.arg, None)
        self.generic_visit(node)
        pending = self.unwaited
        self.names, self.values, self.unwaited = names, values, unwaited
        if pending:
            raise PermissionError(
                "Background process launch requires an explicit wait or communicate in the same function."
            )

    visit_AsyncFunctionDef = visit_FunctionDef
