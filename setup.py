"""Ship the pinned speech worker in Windows x64 wheels only."""
import hashlib
import os
from pathlib import Path
import sysconfig

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist
from wheel.bdist_wheel import bdist_wheel


NATIVE = Path("src/redlotus/TTS/native")
WORKER = NATIVE / "redlotus_mambo.exe"
NOTICES = (NATIVE / "NOTICE.md", NATIVE / "UPSTREAM-LICENSE.txt",
           NATIVE / "THIRD-PARTY-NOTICES.md")
WORKER_SHA256 = "ce05b53e991313a7122b075035b94b33e8c2b2a31a25dad0b67f887c1b805f39"


class SpeechBuildPy(build_py):
    def find_data_files(self, package, src_dir):
        files = super().find_data_files(package, src_dir)
        if os.name == "nt":
            return files
        return [name for name in files if "/TTS/native/" not in Path(name).as_posix()]


class SpeechArtifacts:
    def validate(self):
        if sysconfig.get_platform() != "win-amd64":
            raise RuntimeError("The Mambo worker currently supports Windows x64 only")
        missing = [str(path) for path in (WORKER, *NOTICES) if not path.is_file()]
        if missing:
            raise RuntimeError(f"Windows x64 build requires the Mambo worker and notices: {missing}")
        if not any(path.is_file() and path.suffix.lower() in {".txt", ".md"}
                   for path in (NATIVE / "licenses").rglob("*")):
            raise RuntimeError("Windows x64 build requires the Mambo third-party license files")
        with WORKER.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != WORKER_SHA256:
                raise RuntimeError("Mambo worker does not match the trusted release SHA-256")


class SpeechSdist(sdist):
    def run(self):
        if os.name == "nt":
            SpeechArtifacts().validate()
        super().run()


class SpeechWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        if os.name == "nt":
            SpeechArtifacts().validate()
            self.root_is_pure = False

    def get_tag(self):
        python, abi, platform = super().get_tag()
        if os.name == "nt":
            if platform != "win_amd64":
                raise RuntimeError("The Mambo worker currently supports Windows x64 only")
            return "py3", "none", platform
        return python, abi, platform


setup(cmdclass={"build_py": SpeechBuildPy, "bdist_wheel": SpeechWheel, "sdist": SpeechSdist})
