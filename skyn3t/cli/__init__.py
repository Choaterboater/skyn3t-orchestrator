"""SkyN3t CLI package."""

from __future__ import annotations

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from skyn3t.cli.main import app as cli_app

        return cli_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
