"""Gauntlet: adversarial multi-agent development harness."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gauntlet-spec")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
