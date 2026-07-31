"""Entry point for `python -m mailvault` and the PyInstaller build."""

from mailvault.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
