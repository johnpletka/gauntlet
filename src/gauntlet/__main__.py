"""Allow ``python -m gauntlet`` (used by the engine-managed judge subprocess)."""

from gauntlet.cli import app

if __name__ == "__main__":
    app()
