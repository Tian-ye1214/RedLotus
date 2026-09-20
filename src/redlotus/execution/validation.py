"""Execution validation responsibilities."""

from __future__ import annotations

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
    def __init__(self, inspect_command, *, restricted):
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
        if isinstance(node, (ast.List, ast.Tuple)):
            items = []
            for item in node.elts:
                value = self.literal(item.value if isinstance(item, ast.Starred) else item)
                if value is None or (isinstance(item, ast.Starred) and not isinstance(value, (list, tuple))):
                    return None
                items.extend(value if isinstance(item, ast.Starred) else [value])
            return items
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.literal(node.left), self.literal(node.right)
            if isinstance(left, (str, list, tuple)) and type(left) is type(right):
                return left + right
            return None
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    def command(self, value):
        """Known launch APIs require a completely resolved command before execution."""
        explicit = isinstance(value, str) and bool(value)
        explicit = explicit or (
            isinstance(value, (list, tuple)) and bool(value)
            and all(isinstance(part, str) for part in value) and bool(value[0])
        )
        if explicit:
            self.inspect_command(value)
        elif self.restricted:
            raise PermissionError("Use an explicit command: the process target or arguments cannot be resolved before execution.")

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
        owner, _, method = (name or "").rpartition(".")
        process_operation = name in {"os.kill", "os.killpg", "signal.pthread_kill"} or (
            owner in {"psutil.Process", "subprocess.Popen", "multiprocessing.Process"}
            and method in {"kill", "terminate"}
        )
        if self.restricted and process_operation:
            raise PermissionError(
                f"Permission denied: restricted process operation {name}."
            )
        if name in {
            "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen",
            "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
        }:
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
            if name == "asyncio.create_subprocess_exec":
                argument = ast.List(elts=node.args)
            command = self.literal(argument)
            for item in node.keywords:
                if item.arg == "executable":
                    executable = self.literal(item.value)
                    self.command([executable])
                if item.arg == "shell" and self.literal(item.value) is True and isinstance(command, list):
                    command = " ".join(command)
            self.command(command)
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


class JavaScriptCommandCheck(PythonCommandCheck):
    """Resolve known child_process imports and literal arguments; never execute source."""

    def check(self, source):
        tokens = re.findall(
            r'''//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|[\w$]+|\.\.\.|=>|\n|[^\s]''',
            source,
        )
        tokens = [token for token in tokens if not token.startswith(("//", "/*"))]
        wrappers = {
            "exec": "shell", "execSync": "shell", "spawn": "argv",
            "spawnSync": "argv", "execFile": "argv", "execFileSync": "argv", "fork": "script",
        }
        for index, token in enumerate(tokens):
            if token == "import":
                end = next((n for n in range(index + 1, len(tokens)) if tokens[n] == "from"), None)
                if end is not None and self._module(tokens[end + 1:end + 2]):
                    self._bind(tokens[index + 1:end], imported=True)
            if token == "=":
                end = self._expression_end(tokens, index + 1)
                expression = tokens[index + 1:end]
                if index and tokens[index - 1] == "}":
                    start = index - 2
                    while start >= 0 and tokens[start] != "{":
                        start -= 1
                    if self._qualified(expression) == "child_process":
                        self._bind(tokens[start:index])
                elif index and re.fullmatch(r"[\w$]+", tokens[index - 1]):
                    name = tokens[index - 1]
                    self.names.pop(name, None)
                    self.values.pop(name, None)
                    self.names[name] = self._qualified(expression)
                    self.values[name] = self._value(expression)
            if token != "(":
                continue
            name = self._callee(tokens, index)
            if not name or not name.startswith("child_process."):
                continue
            kind = wrappers.get(name.split(".")[-1])
            if kind is None:
                continue
            arguments, _ = self._arguments(tokens, index + 1)
            program = self._value(arguments[0]) if arguments else None
            if kind == "shell":
                self.command(program)
                continue
            argv = self._value(arguments[1]) if len(arguments) > 1 else []
            if len(arguments) > 1 and ("=>" in arguments[1] or arguments[1][:1] == ["function"]):
                argv = []
            command = [program, *argv] if isinstance(program, str) and isinstance(argv, list) else None
            if kind == "script" and command:
                command.insert(0, "node")
            options = arguments[2] if len(arguments) > 2 else []
            if command and any(options[n:n + 3] == ["shell", ":", "true"] for n in range(len(options))):
                command = " ".join(command)
            self.command(command)

    def _module(self, tokens):
        return self._value(tokens) in {"child_process", "node:child_process"}

    def _qualified(self, tokens):
        if tokens[:2] == ["require", "("] and len(tokens) >= 4 and self._module(tokens[2:3]) and tokens[3] == ")":
            base, rest = "child_process", tokens[4:]
        elif tokens:
            base, rest = self.names.get(tokens[0]), tokens[1:]
        else:
            return None
        return base + "." + rest[1] if base and len(rest) >= 2 and rest[0] == "." else base

    def _callee(self, tokens, end):
        start = end - 1
        if start >= 2 and tokens[start - 1] == ".":
            start -= 2
            if tokens[start] == ")" and start >= 3:
                start -= 3
        return self._qualified(tokens[start:end]) if start >= 0 else None

    def _bind(self, tokens, *, imported=False):
        if tokens[:1] == ["{"]:
            for group in " ".join(tokens[1:-1]).split(","):
                names = group.split()
                if names:
                    self.names[names[-1]] = "child_process." + names[0]
        elif tokens:
            self.names[tokens[-1] if imported and tokens[0] == "*" else tokens[0]] = "child_process"

    @staticmethod
    def _expression_end(tokens, start):
        depth = 0
        for index in range(start, len(tokens)):
            token = tokens[index]
            if depth == 0 and token in {";", ",", "\n"}:
                return index
            depth += (token in {"(", "[", "{"}) - (token in {")", "]", "}"})
            if depth < 0:
                return index
        return len(tokens)

    @staticmethod
    def _arguments(tokens, start):
        groups, current, depth = [], [], 0
        for index in range(start, len(tokens)):
            token = tokens[index]
            if depth == 0 and token in {",", ")"}:
                groups.append(current)
                current = []
                if token == ")":
                    return groups, index
                continue
            current.append(token)
            depth += (token in {"(", "[", "{"}) - (token in {")", "]", "}"})
        return [], len(tokens)

    def _value(self, tokens):
        try:
            expression = " ".join("*" if token == "..." else token for token in tokens)
            return self.literal(ast.parse(expression, mode="eval").body)
        except SyntaxError:
            return None
