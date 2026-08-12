"""Allow ``python -m quickaccess`` during development."""

from .app import main


if __name__ == "__main__":
    raise SystemExit(main())

