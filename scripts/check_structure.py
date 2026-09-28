"""Check the project's source budget independently of regression-test size."""

import ast
from collections import Counter
import difflib
import io
from pathlib import Path
import subprocess
import tokenize


ROOT = Path(__file__).resolve().parents[1]
BASE = "b14d055"


def effective_lines(source):
    documentation = {
        line
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for line in range(node.lineno, node.end_lineno + 1)
    }
    return sum(token.type == tokenize.NEWLINE and token.start[0] not in documentation
               for token in tokenize.generate_tokens(io.StringIO(source).readline))


def application_file(name):
    return name.startswith("src/redlotus/") and name.endswith(".py") and "/skills/" not in name


def check():
    names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT, text=True, encoding="utf-8").splitlines()
    before = {name: subprocess.check_output(["git", "show", f"{BASE}:{name}"], cwd=ROOT).decode("utf-8")
              for name in names if application_file(name)}
    after = {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8-sig")
             for path in (ROOT / "src/redlotus").rglob("*.py")
             if application_file(path.relative_to(ROOT).as_posix())}
    sizes = {name: effective_lines(source) for name, source in after.items()}
    modules = Counter(name.split("/")[2] for name in after)
    added = deleted = 0
    for name in before.keys() | after.keys():
        for op, start, end, new_start, new_end in difflib.SequenceMatcher(
                a=before.get(name, "").splitlines(), b=after.get(name, "").splitlines(), autojunk=False).get_opcodes():
            if op != "equal":
                added += new_end - new_start
                deleted += end - start
    baseline, total = sum(map(effective_lines, before.values())), sum(sizes.values())
    print(f"Application: {baseline} -> {total} effective lines ({total - baseline:+d}); physical +{added}/-{deleted}")
    print(f"Modules: {len(modules)}; files: {len(after)}; maximum file: {max(sizes.values())}")
    assert len(modules) <= 10 and max(modules.values()) <= 5, modules
    assert max(sizes.values()) <= 500, {name: size for name, size in sizes.items() if size > 500}
    for label, paths in (("Tests", (ROOT / "tests").glob("*.py")),
                         ("Verification", (ROOT / "scripts").glob("*.py"))):
        separate = {path.name: effective_lines(path.read_text(encoding="utf-8-sig")) for path in paths}
        print(f"{label}: {sum(separate.values())} effective lines across {len(separate)} files")
        assert max(separate.values(), default=0) <= 500, separate


if __name__ == "__main__":
    check()
