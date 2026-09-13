"""Source-checkout launcher; importing this module performs no application startup."""


def main():
    import os
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        os.environ["LOGFIRE_PYDANTIC_RECORD"] = "off"
    else:
        root = Path(__file__).resolve().parent
        os.environ.setdefault(
            "REDLOTUS_CONFIG_FILE", str(root / "src/redlotus/config.json")
        )
        os.environ.setdefault("REDLOTUS_DOTENV_FILE", str(root / ".env"))
        sys.path.insert(0, str(root / "src"))
    from redlotus.agent_core.entrypoint import main as run

    run()


if __name__ == "__main__":
    main()
