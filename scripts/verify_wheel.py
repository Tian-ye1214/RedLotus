"""Install one built wheel locally and run the regressions against that installation."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import venv
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    artifacts = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "dist"
    wheels = list(artifacts.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one wheel in the artifact directory")
    with zipfile.ZipFile(wheels[0]) as archive:
        if any(name.endswith(("/config.json", "/.env", "/api/config.yaml")) for name in archive.namelist()):
            raise SystemExit("Wheel includes private configuration")
    runtime = root / ".test-runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheel-check-", dir=runtime) as temporary:
        location = Path(temporary).resolve()
        if not location.is_relative_to(runtime.resolve()):
            raise RuntimeError("Wheel test environment escaped the project")
        environment = location / "env"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])], check=True)
        library = Path(subprocess.check_output(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
        # Reuse the caller's third-party test dependencies after the isolated install.
        (library / "test-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        env = dict(os.environ, REDLOTUS_TEST_INSTALL_ROOT=str(environment), PYTHONUTF8="1")
        subprocess.run([str(python), "-I", "-m", "pytest", "-q", "--import-mode=importlib",
                        "-c", str(root / "pyproject.toml"), "-o", "pythonpath=",
                        "--basetemp", str(location / "pytest"), str(root / "tests")],
                       cwd=location, env=env, check=True)


if __name__ == "__main__":
    main()
