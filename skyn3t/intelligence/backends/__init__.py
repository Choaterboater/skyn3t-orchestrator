"""Remote execution backends package.

Exports the core protocol/base/result + registry helpers, and attempts to
register the optional per-host adapters (ssh/modal/daytona/e2b) — each
behind a guarded import so a missing optional SDK or an adapter module
that hasn't landed yet can NEVER break ``import skyn3t.intelligence.backends``.

The adapters themselves are owned by BACKENDS_ADAPTERS and live in
sibling modules; this package only wires them into the registry when they
exist and import cleanly. Until then, ``available_backends()`` simply
returns an empty list and the orchestrator degrades to local/docker.
"""

from __future__ import annotations

import importlib
import logging
from typing import List, Tuple

from skyn3t.intelligence.backends.base import (
    BACKEND_STATUSES,
    BackendResult,
    BaseRemoteBackend,
    RemoteBackend,
    available_backends,
    get_remote_backend,
    register_backend,
    registered_backends,
)

logger = logging.getLogger("skyn3t.intelligence.backends")

__all__ = [
    "RemoteBackend",
    "BaseRemoteBackend",
    "BackendResult",
    "BACKEND_STATUSES",
    "get_remote_backend",
    "available_backends",
    "registered_backends",
    "register_backend",
]


# (module, attribute) pairs for the optional adapters. Each is imported
# lazily and guarded: ImportError (SDK/module absent) or any registration
# error is logged and skipped. The adapter modules self-register on import
# via the @register_backend decorator; we additionally call register here
# defensively in case the module exposes the class without decorating.
_OPTIONAL_ADAPTERS: Tuple[Tuple[str, str], ...] = (
    ("skyn3t.intelligence.backends.ssh_backend", "SSHBackend"),
    ("skyn3t.intelligence.backends.modal_backend", "ModalBackend"),
    ("skyn3t.intelligence.backends.daytona_backend", "DaytonaBackend"),
    ("skyn3t.intelligence.backends.e2b_backend", "E2BBackend"),
)


def _load_optional_adapters() -> List[str]:
    """Import + register whatever optional adapters are present. Returns
    the names that loaded. Never raises — a broken/absent adapter is
    skipped so the package import always succeeds."""
    loaded: List[str] = []
    for module_name, attr in _OPTIONAL_ADAPTERS:
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            logger.debug("optional backend module %s not present; skipping", module_name)
            continue
        except Exception:
            logger.warning("optional backend module %s failed to import; skipping",
                           module_name, exc_info=True)
            continue
        cls = getattr(mod, attr, None)
        if cls is None:
            logger.debug("module %s has no %s; skipping", module_name, attr)
            continue
        try:
            # Idempotent — safe even if the module already self-registered.
            register_backend(cls)
            loaded.append(getattr(cls, "name", attr))
        except Exception:
            logger.warning("failed to register backend %s.%s; skipping",
                           module_name, attr, exc_info=True)
            continue
    return loaded


# Best-effort wiring at import time. Guarded so it can never break import.
try:
    _load_optional_adapters()
except Exception:  # pragma: no cover - defensive belt-and-suspenders
    logger.debug("optional backend autoload failed; continuing with none", exc_info=True)
