"""Asset Agent - generate raster/vector assets and import design tokens.

This agent is the pluggable seam for *generated* visual assets (logos,
hero images, sprites, icons) and for *imported* design tokens
(image/Figma/Penpot exports). It deliberately does NOT bake in a single
provider — image generation rides behind a thin provider-hook layer
(Replicate/SDXL-style) gated by the ``SKYN3T_ASSET_GEN`` flag and the
``REPLICATE_API_TOKEN`` env var (read via ``os.getenv``).

Design rules (Phase 5A, "10x smarter builder"):

* **Never block a build.** When the flag is off, the key is absent, or a
  provider call fails, ``execute`` still returns successfully — each asset
  entry is marked ``skipped=True`` with a reason. The pipeline gets a
  predictable, structured result either way.
* **No network in the default/test path.** Generation only touches the
  network when ``SKYN3T_ASSET_GEN`` is on AND a key is present AND a
  provider hook is installed. Tests inject a fake provider to exercise the
  happy path without sockets.
* **Token import reuses existing seams.** ``import_design_tokens`` delegates
  image sources to :func:`skyn3t.agents.design_vision.extract` (CLI vision,
  no API key needed) and exposes Figma/Penpot as pluggable no-op stubs that
  degrade gracefully without keys.

The integration owner (runner) imports ``AssetAgent`` directly because
``agents/__init__.py`` is integration-owned.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger(__name__)

# Asset kinds we understand. Unknown kinds still produce a (skipped) entry
# so the caller's plan->result mapping never silently loses an item.
_ASSET_KINDS = ("image", "sprite", "icon", "logo")

# Sensible default geometry per kind when the spec omits w/h.
_DEFAULT_DIMS: Dict[str, tuple[int, int]] = {
    "image": (1024, 1024),
    "sprite": (512, 512),
    "icon": (256, 256),
    "logo": (512, 512),
}

_GEN_TIMEOUT_SECONDS = 120.0

# A provider hook takes a normalized request dict and returns the bytes of
# the generated asset (or ``None`` to signal "I couldn't do it"). It may be
# sync or async. Tests register a fake hook here; production registers a
# Replicate/SDXL hook lazily (only when key + flag are present).
ProviderHook = Callable[[Dict[str, Any]], Union[Optional[bytes], Awaitable[Optional[bytes]]]]

# Module-level registry so the integration owner / tests can plug a
# provider without subclassing. Keyed by provider name.
_PROVIDER_HOOKS: Dict[str, ProviderHook] = {}


def asset_gen_enabled() -> bool:
    """True when generated-asset production is enabled.

    Flag-gated and OFF by default. Reads ``SKYN3T_ASSET_GEN`` via
    ``os.getenv`` (never ``config.settings``). Accepts the usual truthy
    spellings so a developer can flip it without editing settings.
    """
    raw = (os.getenv("SKYN3T_ASSET_GEN") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _replicate_key() -> str:
    """The Replicate API token, or empty string when unset. ``os.getenv``
    only — config.settings is integration-owned and off-limits here."""
    return (os.getenv("REPLICATE_API_TOKEN") or "").strip()


def register_provider_hook(name: str, hook: ProviderHook) -> None:
    """Register a generation provider hook by name.

    The integration owner wires the real Replicate/SDXL client here; tests
    register a fake that writes deterministic bytes. Keeping this at module
    scope means we never have to import a heavy SDK at import time, and a
    missing SDK degrades to "no hook → skip" rather than an ImportError.
    """
    _PROVIDER_HOOKS[name] = hook


def unregister_provider_hook(name: str) -> None:
    """Remove a provider hook (test cleanup helper). No-op if absent."""
    _PROVIDER_HOOKS.pop(name, None)


def _active_provider() -> Optional[str]:
    """Return the provider name to use, or ``None`` when generation can't run.

    Generation requires ALL of: the flag on, a key present, and at least one
    registered hook. Any missing precondition → ``None`` (caller skips).
    """
    if not asset_gen_enabled():
        return None
    if not _replicate_key():
        return None
    if not _PROVIDER_HOOKS:
        return None
    # Prefer an explicitly-named replicate hook; otherwise first registered.
    if "replicate" in _PROVIDER_HOOKS:
        return "replicate"
    return next(iter(_PROVIDER_HOOKS))


async def _invoke_hook(hook: ProviderHook, request: Dict[str, Any]) -> Optional[bytes]:
    """Call a provider hook (sync or async) under a timeout. Any failure or
    timeout returns ``None`` so the caller degrades to skipped."""
    try:
        result = hook(request)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=_GEN_TIMEOUT_SECONDS)
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        return None
    except asyncio.TimeoutError:
        logger.warning("asset provider hook timed out after %.0fs", _GEN_TIMEOUT_SECONDS)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("asset provider hook raised; skipping asset", exc_info=True)
        return None


# ── Design-token import seam ────────────────────────────────────────────


async def import_design_tokens(
    *, source: str, ref: Union[str, Path]
) -> Optional[dict]:
    """Import design tokens from an external design source.

    * ``source='image'`` — delegate to
      :func:`skyn3t.agents.design_vision.extract`, which runs CLI vision
      (no API key) and returns a ``DesignReference``. We return its dict
      form so callers don't need the dataclass.
    * ``source='figma'`` / ``source='penpot'`` — pluggable stubs. Without
      the relevant key (``FIGMA_TOKEN`` / ``PENPOT_TOKEN`` via os.getenv)
      these no-op and return ``None`` so the pipeline degrades gracefully.

    Returns a tokens dict, or ``None`` when the source is unknown, the key
    is absent, or extraction fails. Never raises.
    """
    src = (source or "").strip().lower()
    if src == "image":
        return await _import_image_tokens(ref)
    if src == "figma":
        return await _import_figma_tokens(ref)
    if src == "penpot":
        return await _import_penpot_tokens(ref)
    logger.info("import_design_tokens: unknown source %r", source)
    return None


async def _import_image_tokens(ref: Union[str, Path]) -> Optional[dict]:
    try:
        from skyn3t.agents.design_vision import extract
    except Exception:  # noqa: BLE001
        logger.warning("design_vision unavailable for token import", exc_info=True)
        return None
    try:
        reference = await extract(Path(ref))
    except Exception:  # noqa: BLE001
        logger.warning("design_vision.extract raised during token import", exc_info=True)
        return None
    if reference is None:
        return None
    return _reference_to_tokens(reference)


def _reference_to_tokens(reference: Any) -> dict:
    """Normalize a ``DesignReference`` (or anything with the same fields)
    into a tokens dict the designer/runner can merge with palette.json."""
    palette: List[Dict[str, str]] = []
    for entry in getattr(reference, "palette", []) or []:
        palette.append(
            {
                "name": str(getattr(entry, "name", "") or ""),
                "hex": str(getattr(entry, "hex", "") or ""),
                "role": str(getattr(entry, "role", "") or "accent"),
            }
        )
    return {
        "source": "image",
        "palette": palette,
        "typography_vibe": str(getattr(reference, "typography_vibe", "") or ""),
        "layout_density": str(getattr(reference, "layout_density", "") or ""),
        "mood": list(getattr(reference, "mood", []) or []),
        "forbidden_words": list(getattr(reference, "forbidden_words", []) or []),
    }


async def _import_figma_tokens(ref: Union[str, Path]) -> Optional[dict]:
    """Figma import seam. No-ops without ``FIGMA_TOKEN`` (graceful skip).

    A real implementation pulls variables/styles from the Figma REST API;
    here we expose the pluggable hook + key check so wiring it later is a
    drop-in, and absent a key the pipeline simply gets ``None``.
    """
    key = (os.getenv("FIGMA_TOKEN") or os.getenv("FIGMA_API_TOKEN") or "").strip()
    if not key:
        logger.info("import_design_tokens(figma): no FIGMA_TOKEN; skipping")
        return None
    hook = _PROVIDER_HOOKS.get("figma_tokens")
    if hook is None:
        logger.info("import_design_tokens(figma): no figma_tokens hook installed; skipping")
        return None
    try:
        result = hook({"source": "figma", "ref": str(ref), "key": key})
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=_GEN_TIMEOUT_SECONDS)
        return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("figma token import hook raised; skipping", exc_info=True)
        return None


async def _import_penpot_tokens(ref: Union[str, Path]) -> Optional[dict]:
    """Penpot import seam. Mirror of the Figma stub; no-ops without
    ``PENPOT_TOKEN``."""
    key = (os.getenv("PENPOT_TOKEN") or os.getenv("PENPOT_API_TOKEN") or "").strip()
    if not key:
        logger.info("import_design_tokens(penpot): no PENPOT_TOKEN; skipping")
        return None
    hook = _PROVIDER_HOOKS.get("penpot_tokens")
    if hook is None:
        logger.info("import_design_tokens(penpot): no penpot_tokens hook installed; skipping")
        return None
    try:
        result = hook({"source": "penpot", "ref": str(ref), "key": key})
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=_GEN_TIMEOUT_SECONDS)
        return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("penpot token import hook raised; skipping", exc_info=True)
        return None


class AssetAgent(BaseAgent):
    """Generate images/sprites/icons/logos and import design tokens.

    Follows the DesignerAgent BaseAgent pattern. ``execute`` consumes
    ``task.input_data`` per AssetAgentAPI and always succeeds (graceful
    degrade), returning skipped entries rather than failing the build.
    """

    def __init__(
        self,
        name: str = "asset",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="asset",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="asset_generation",
                description=(
                    "Generate logo/hero/sprite/icon assets via a pluggable "
                    "provider (Replicate/SDXL) behind SKYN3T_ASSET_GEN."
                ),
                parameters={"asset_specs": "list", "artifact_dir": "str"},
                required_config=["REPLICATE_API_TOKEN"],
            )
        )
        self.add_capability(
            AgentCapability(
                name="design_import",
                description="Import design tokens from image/Figma/Penpot sources.",
                parameters={"design_import": "dict"},
            )
        )

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(
        self, task: TaskRequest, stdin_data: Optional[str] = None
    ) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")
        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip()
        artifact_dir = self.resolve_artifact_dir(data.get("artifact_dir"))
        asset_specs: List[Dict[str, Any]] = list(data.get("asset_specs") or [])
        design_import: Optional[Dict[str, Any]] = data.get("design_import")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return TaskResult(
                task_id=task.task_id, success=False, error=f"artifact_dir error: {e}"
            )

        provider = _active_provider()
        if provider is None:
            await self.think(
                "asset generation disabled/unavailable (flag off, no key, or no "
                "provider hook) — emitting skipped entries; build is not blocked"
            )

        assets: List[Dict[str, Any]] = []
        for spec in asset_specs:
            assets.append(
                await self._produce_asset(spec, brief, artifact_dir, provider)
            )

        # Design-token import (additive; independent of asset generation).
        tokens: Optional[dict] = None
        if isinstance(design_import, dict) and design_import.get("source"):
            ref = design_import.get("ref") or design_import.get("path")
            if ref:
                tokens = await import_design_tokens(
                    source=str(design_import.get("source")), ref=ref
                )
                if tokens is not None:
                    await self.think(
                        f"imported design tokens from {design_import.get('source')}"
                    )

        scaffolds = self._asset_scaffolds(assets, tokens)

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "assets": assets,
                "tokens": tokens,
                "scaffolds": scaffolds,
                "summary": (
                    f"{sum(1 for a in assets if not a.get('skipped'))}/{len(assets)} "
                    f"asset(s) generated"
                    + ("; tokens imported" if tokens else "")
                ),
            },
        )

    # ------------------------------------------------------------------
    # Per-asset production
    # ------------------------------------------------------------------
    async def _produce_asset(
        self,
        spec: Dict[str, Any],
        brief: str,
        artifact_dir: Path,
        provider: Optional[str],
    ) -> Dict[str, Any]:
        kind = str(spec.get("kind") or "image").strip().lower()
        if kind not in _ASSET_KINDS:
            kind = "image"
        out_path = self._resolve_out_path(spec, kind, artifact_dir)
        entry: Dict[str, Any] = {
            "kind": kind,
            "path": str(out_path),
            "provider": provider,
            "skipped": True,
        }

        if provider is None:
            entry["skipped_reason"] = "asset_gen_disabled"
            return entry

        hook = _PROVIDER_HOOKS.get(provider)
        if hook is None:  # defensive — _active_provider already checked
            entry["skipped_reason"] = "no_provider_hook"
            return entry

        dw, dh = _DEFAULT_DIMS.get(kind, _DEFAULT_DIMS["image"])
        request = {
            "kind": kind,
            "prompt": str(spec.get("prompt") or brief or kind),
            "w": int(spec.get("w") or dw),
            "h": int(spec.get("h") or dh),
            "out_path": str(out_path),
        }
        payload = await _invoke_hook(hook, request)
        if not payload:
            entry["skipped_reason"] = "provider_returned_nothing"
            return entry

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(payload)
        except Exception:  # noqa: BLE001
            logger.warning("failed writing asset to %s", out_path, exc_info=True)
            entry["skipped_reason"] = "write_failed"
            return entry

        entry["skipped"] = False
        entry["bytes"] = len(payload)
        await self.think(f"wrote {kind} asset -> {out_path.name} ({len(payload)} bytes)")
        return entry

    @staticmethod
    def _resolve_out_path(
        spec: Dict[str, Any], kind: str, artifact_dir: Path
    ) -> Path:
        raw = spec.get("out_path")
        if raw:
            p = Path(str(raw))
            if not p.is_absolute():
                p = artifact_dir / p
            return p
        # Default filename under an assets/ subfolder so we never collide
        # with the designer's logo.svg / tokens.* at the artifact root.
        suffix = "svg" if kind in ("icon", "logo") else "png"
        return artifact_dir / "assets" / f"{kind}.{suffix}"

    @staticmethod
    def _asset_scaffolds(
        assets: List[Dict[str, Any]], tokens: Optional[dict]
    ) -> List[str]:
        """Paths the build can wire in. Only includes assets actually
        written (not skipped) so the runner never references a missing file."""
        scaffolds = [a["path"] for a in assets if not a.get("skipped") and a.get("path")]
        if tokens and tokens.get("palette"):
            scaffolds.append("design_tokens.imported")
        return scaffolds


def get_asset_agent(
    *, event_bus: EventBus | None = None, config: Optional[Dict[str, Any]] = None
) -> AssetAgent:
    """Convenience factory mirroring other agents' construction style."""
    return AssetAgent(event_bus=event_bus, config=config)
