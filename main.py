"""Source-checkout launcher; importing this module performs no application startup."""




def main():
    import os
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        os.environ["LOGFIRE_PYDANTIC_RECORD"] = "off"
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent
        # A shared editable environment must not mix another checkout's packages.
        source = root / "src"
        sys.path[:] = [str(source), *[
            path for path in sys.path
            if Path(path).resolve() != source
            and not (Path(path).name == "src" and (Path(path) / "redlotus").is_dir())
        ]]
    os.environ.setdefault("REDLOTUS_CONFIG_FILE", str(root / "src/redlotus/config.json"))
    os.environ.setdefault("REDLOTUS_DOTENV_FILE", str(root / ".env"))
    from redlotus.terminal.console import main as run

    run()


if __name__ == "__main__":
    main()
