"""Make ``python -m eventcontracts.cli`` invoke :func:`main`."""

from eventcontracts.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
