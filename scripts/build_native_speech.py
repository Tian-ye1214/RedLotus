"""Build the pinned CPU worker without installing or changing global tools."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


class NativeBuild:
    COMMIT = "384a2f995c48ef1903373ac8814569a8925779ba"

    def __init__(self, source, build_dir, sdk, cmake, rust_bin, cargo_home):
        self.project = Path(__file__).resolve().parents[1]
        self.source, self.build_dir, self.sdk = source.resolve(), build_dir.resolve(), sdk.resolve()
        self.cmake = str(cmake.resolve())
        self.native = self.project / "src/redlotus/TTS/native"
        self.env = {key: value for key, value in os.environ.items() if key.lower() != "path"}
        self.env.update(Path=str(rust_bin.resolve()) + os.pathsep + os.environ["PATH"],
                        CARGO_HOME=str(cargo_home.resolve()), CARGO_NET_OFFLINE="true",
                        MSBUILDDISABLENODEREUSE="1")

    def prepare_source(self):
        revision = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        if revision != self.COMMIT:
            raise ValueError("The native source does not match the pinned upstream commit")
        patch = str(self.native / "upstream.patch")
        git = ["git", "-C", str(self.source), "apply"]
        if subprocess.run([*git, "--reverse", "--check", patch], capture_output=True).returncode:
            subprocess.run([*git, "--check", patch], check=True)
            subprocess.run([*git, patch], check=True)
        actual = subprocess.check_output(["git", "-C", str(self.source), "diff", "--binary"]).decode("utf-8")
        untracked = subprocess.check_output(["git", "-C", str(self.source), "ls-files", "--others", "--exclude-standard"]).decode().splitlines()
        for name in untracked:
            if name not in {"include/GPTSoVITS/Utils/MamboDecoder.h", "include/GPTSoVITS/Utils/MamboSampling.h"}:
                raise ValueError(f"Unrecorded native source: {name}")
            actual += f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
            actual += "".join(difflib.unified_diff([], (self.source / name).read_text(encoding="utf-8").splitlines(keepends=True),
                                                  fromfile="/dev/null", tofile="b/" + name))
        if actual.replace("\r\n", "\n") != Path(patch).read_text(encoding="utf-8"):
            raise ValueError("Native source contains changes outside the recorded patch")
        subprocess.run(["git", "-C", str(self.source), "diff", "--cached", "--exit-code", "--quiet"], check=True)
        header = self.sdk / "include/onnxruntime_c_api.h"
        if "#define ORT_API_VERSION 28" not in header.read_text(encoding="utf-8"):
            raise ValueError("ONNX Runtime 1.28 SDK headers are required")

    def run(self):
        if os.name != "nt":
            raise RuntimeError("This worker build currently targets Windows x64")
        self.prepare_source()
        subprocess.run([self.cmake, "-S", str(self.source), "-B", str(self.build_dir),
            "-G", "Visual Studio 17 2022", "-A", "x64", "-DENABLE_CUDA=OFF", "-DUSE_TENSORRT=OFF",
            "-DUSE_ONNX=ON", "-DNO_TEST=ON", f"-DONNXRUNTIME_PATH={self.sdk.as_posix()}",
            f"-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={self.build_dir.as_posix()}",
            f"-DREDLOTUS_SOURCE_ROOT={self.project.as_posix()}"], env=self.env, check=True)
        subprocess.run([self.cmake, "--build", str(self.build_dir), "--config", "Release",
                        "--target", "redlotus_mambo", "--parallel", "4"], env=self.env, check=True)
        destination = self.native / "redlotus_mambo.exe"
        shutil.copyfile(self.build_dir / "Release/redlotus_mambo.exe", destination)
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        print(json.dumps({"commit": self.COMMIT, "worker": str(destination), "sha256": digest,
                          "bytes": destination.stat().st_size, "runtime_copied": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "build-dir", "sdk", "cmake", "rust-bin", "cargo-home"):
        parser.add_argument("--" + name, type=Path, required=True)
    options = parser.parse_args()
    NativeBuild(options.source, options.build_dir, options.sdk, options.cmake,
                options.rust_bin, options.cargo_home).run()
