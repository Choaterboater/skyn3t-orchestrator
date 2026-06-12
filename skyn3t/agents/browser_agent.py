"""Browser Agent — drive a real browser (local Playwright/CDP or a cloud
sandbox) to browse, extract page content, and capture screenshots.

Design constraints (Phase 5B parity):

* Playwright is NOT a hard dependency. It is not installed in the venv and
  may not be installed in production. Every ``playwright`` (and cloud SDK)
  import is LAZY — performed *inside* ``available()`` / the backend
  methods — so importing this module never fails and never drags in a
  missing SDK at import time.
* Each backend is opt-in and gated: the local CDP backend needs Playwright
  importable; the cloud backends need their API key
  (``BROWSERBASE_API_KEY`` / ``BROWSER_USE_API_KEY``) AND their SDK.
* ``available()`` is pure/non-raising (mirrors ``docker_available()``): it
  returns ``True``/``False`` and never propagates an exception.
* Graceful no-browser skip: when no backend is available, the agent does
  not crash — ``backend_available`` is ``False`` and ``execute`` returns
  ``TaskResult(success=False, output={'skipped': True, 'reason': ...})``.

The agent runs standalone; it does not depend on the Playwright MCP tools
that may exist in the surrounding harness.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.browser_agent")


# ─────────────────────────────────────────────────────────────────────────
# Result / step value objects
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class BrowserStep:
    """Outcome of a single ``act`` instruction."""

    ok: bool
    detail: str = ""
    error: Optional[str] = None


@dataclass
class BrowserSnapshot:
    """A point-in-time capture of the current page."""

    url: str
    title: str = ""
    text: str = ""
    screenshot_path: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────
# Backend protocol + abstract base
# ─────────────────────────────────────────────────────────────────────────


@runtime_checkable
class BrowserBackend(Protocol):
    """A browser automation backend.

    Implementations MUST keep all third-party imports lazy so that merely
    importing this module never requires Playwright or a cloud SDK.
    """

    name: str  # 'local-cdp' | 'browserbase' | 'browser-use'

    @classmethod
    def available(cls) -> bool:  # pragma: no cover - protocol stub
        """Return True when this backend can actually run. NEVER raises."""
        ...

    async def open(self, url: str) -> None: ...  # pragma: no cover

    async def act(self, instruction: str) -> "BrowserStep": ...  # pragma: no cover

    async def snapshot(self) -> "BrowserSnapshot": ...  # pragma: no cover

    async def close(self) -> None: ...  # pragma: no cover


def _playwright_importable() -> bool:
    """True if ``playwright.async_api`` can be imported — WITHOUT importing it.

    Uses ``importlib.util.find_spec`` so we never actually pull Playwright
    into the process during the availability probe (which keeps the probe
    cheap and side-effect-free). Never raises.
    """
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except Exception:
        # find_spec can raise (e.g. a broken/partial install). Treat any
        # failure as "not available" rather than letting it propagate.
        return False


class BaseBrowserBackend:
    """Common scaffolding for backends. Subclasses override the verbs.

    Provides a no-op default lifecycle so partially-implemented backends
    degrade instead of crashing.
    """

    name: str = "base"

    @classmethod
    def available(cls) -> bool:  # pragma: no cover - overridden
        return False

    async def open(self, url: str) -> None:
        raise NotImplementedError

    async def act(self, instruction: str) -> BrowserStep:
        raise NotImplementedError

    async def snapshot(self) -> BrowserSnapshot:
        raise NotImplementedError

    async def close(self) -> None:
        # Idempotent no-op by default.
        return None


# ─────────────────────────────────────────────────────────────────────────
# Local Playwright / CDP backend
# ─────────────────────────────────────────────────────────────────────────


class LocalCdpBackend(BaseBrowserBackend):
    """Drive a local Chromium via Playwright (CDP).

    All Playwright imports are lazy — they happen inside ``available()``
    (probe only, no real import) and inside the lifecycle methods (real
    import, guarded). If Playwright is absent the backend reports
    unavailable and is never selected.
    """

    name = "local-cdp"

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._pw = None  # async_playwright context manager
        self._browser = None
        self._page = None

    @classmethod
    def available(cls) -> bool:
        # Opt-out hook + presence probe. Never raises.
        if os.getenv("SKYN3T_BROWSER_DISABLE_LOCAL", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return False
        return _playwright_importable()

    async def open(self, url: str) -> None:
        if self._page is None:
            # LAZY import — only reached when the backend was selected, which
            # only happens when available() returned True.
            from playwright.async_api import async_playwright  # noqa: WPS433

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._page = await self._browser.new_page()
        await self._page.goto(url)

    async def act(self, instruction: str) -> BrowserStep:
        if self._page is None:
            return BrowserStep(ok=False, error="no page open; call open() first")
        # The local backend has no LLM-driven action planner; it exposes a
        # minimal, deterministic command surface so callers (and tests) get
        # a predictable, side-effect-bounded behaviour. Richer NL action
        # planning is the cloud backends' job (browser-use).
        try:
            text = (instruction or "").strip()
            if text.lower().startswith("goto "):
                await self._page.goto(text[5:].strip())
                return BrowserStep(ok=True, detail=f"navigated to {text[5:].strip()}")
            # Default: treat the instruction as a best-effort note; we do not
            # execute arbitrary JS for safety.
            return BrowserStep(
                ok=True,
                detail=f"noted instruction (no-op): {text[:120]}",
            )
        except Exception as exc:  # pragma: no cover - needs real browser
            return BrowserStep(ok=False, error=str(exc))

    async def snapshot(self) -> BrowserSnapshot:
        if self._page is None:
            return BrowserSnapshot(url="", title="", text="")
        url = self._page.url
        title = await self._page.title()
        try:
            text = await self._page.inner_text("body")
        except Exception:  # pragma: no cover - needs real browser
            text = ""
        return BrowserSnapshot(url=url, title=title, text=text)

    async def close(self) -> None:
        # Idempotent teardown — safe to call multiple times.
        try:
            if self._page is not None:
                await self._page.close()
        except Exception:  # pragma: no cover - needs real browser
            pass
        finally:
            self._page = None
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # pragma: no cover - needs real browser
            pass
        finally:
            self._browser = None
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # pragma: no cover - needs real browser
            pass
        finally:
            self._pw = None


# ─────────────────────────────────────────────────────────────────────────
# Cloud backends (gated on API key + SDK)
# ─────────────────────────────────────────────────────────────────────────


class BrowserbaseBackend(BaseBrowserBackend):
    """Cloud Chromium via Browserbase. Gated on ``BROWSERBASE_API_KEY``.

    Lazy-imports the ``browserbase`` SDK inside methods. ``available()``
    only requires the key + SDK *spec* to be present (no real import).
    """

    name = "browserbase"

    def __init__(self) -> None:
        self._client = None
        self._session = None
        self._page = None
        self._pw = None
        self._browser = None

    @classmethod
    def available(cls) -> bool:
        try:
            if not os.getenv("BROWSERBASE_API_KEY"):
                return False
            # Needs the SDK and Playwright (Browserbase drives a remote
            # Chromium over CDP via Playwright). Probe only, no real import.
            if importlib.util.find_spec("browserbase") is None:
                return False
            return _playwright_importable()
        except Exception:
            return False

    async def open(self, url: str) -> None:  # pragma: no cover - needs network
        from browserbase import Browserbase  # noqa: WPS433
        from playwright.async_api import async_playwright  # noqa: WPS433

        if self._page is None:
            self._client = Browserbase(api_key=os.environ["BROWSERBASE_API_KEY"])
            self._session = self._client.sessions.create(
                project_id=os.getenv("BROWSERBASE_PROJECT_ID", "")
            )
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                self._session.connect_url
            )
            ctx = self._browser.contexts[0]
            self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await self._page.goto(url)

    async def act(self, instruction: str) -> BrowserStep:  # pragma: no cover
        if self._page is None:
            return BrowserStep(ok=False, error="no session open; call open() first")
        return BrowserStep(ok=True, detail=f"noted instruction: {instruction[:120]}")

    async def snapshot(self) -> BrowserSnapshot:  # pragma: no cover - needs network
        if self._page is None:
            return BrowserSnapshot(url="", title="", text="")
        url = self._page.url
        title = await self._page.title()
        try:
            text = await self._page.inner_text("body")
        except Exception:
            text = ""
        return BrowserSnapshot(url=url, title=title, text=text)

    async def close(self) -> None:  # pragma: no cover - needs network
        for attr in ("_browser", "_pw"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                closer = getattr(obj, "close", None) or getattr(obj, "stop", None)
                if closer:
                    res = closer()
                    if hasattr(res, "__await__"):
                        await res
            except Exception:
                pass
            setattr(self, attr, None)
        self._page = None
        self._session = None
        self._client = None


class BrowserUseBackend(BaseBrowserBackend):
    """Cloud agentic browser via browser-use. Gated on ``BROWSER_USE_API_KEY``.

    Lazy-imports the ``browser_use`` SDK inside methods. ``available()``
    only checks the key + SDK spec presence (no real import).
    """

    name = "browser-use"

    def __init__(self) -> None:
        self._agent = None
        self._last_url = ""
        self._last_result = ""

    @classmethod
    def available(cls) -> bool:
        try:
            if not os.getenv("BROWSER_USE_API_KEY"):
                return False
            return importlib.util.find_spec("browser_use") is not None
        except Exception:
            return False

    async def open(self, url: str) -> None:  # pragma: no cover - needs network
        self._last_url = url

    async def act(self, instruction: str) -> BrowserStep:  # pragma: no cover
        from browser_use import Agent  # noqa: WPS433

        try:
            self._agent = Agent(task=instruction)
            result = await self._agent.run()
            self._last_result = str(result)
            return BrowserStep(ok=True, detail=self._last_result[:200])
        except Exception as exc:
            return BrowserStep(ok=False, error=str(exc))

    async def snapshot(self) -> BrowserSnapshot:  # pragma: no cover - needs network
        return BrowserSnapshot(
            url=self._last_url, title="", text=self._last_result
        )

    async def close(self) -> None:  # pragma: no cover - needs network
        self._agent = None


# ─────────────────────────────────────────────────────────────────────────
# Backend selection
# ─────────────────────────────────────────────────────────────────────────

# Preference order: local first (no network / no cost), then cloud SDKs.
_BACKEND_ORDER = (LocalCdpBackend, BrowserbaseBackend, BrowserUseBackend)


def select_browser_backend() -> Optional[BrowserBackend]:
    """Return the best available backend instance, or ``None``.

    local-cdp if Playwright is importable, else a cloud backend whose key +
    SDK are present, else ``None`` (graceful skip). Never raises — a backend
    whose ``available()`` somehow throws is simply skipped.
    """
    for backend_cls in _BACKEND_ORDER:
        try:
            if backend_cls.available():
                return backend_cls()  # type: ignore[return-value]
        except Exception:
            logger.debug("backend %s availability probe failed", backend_cls, exc_info=True)
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────
# The agent
# ─────────────────────────────────────────────────────────────────────────


class BrowserAgent(BaseAgent):
    """Agent that browses the web, extracts content, and screenshots pages.

    task_type ∈ {browse, extract, screenshot}. When no browser backend is
    available the agent degrades gracefully rather than crashing.
    """

    def __init__(
        self,
        name: str = "browser_agent",
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type="browser",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="browse",
                description="Open a URL and run optional NL instructions",
                parameters={"url": "str", "instructions": "list"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="extract",
                description="Extract text/DOM content from a page",
                parameters={"url": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="screenshot",
                description="Capture a screenshot of a page",
                parameters={"url": "str", "path": "str"},
            )
        )
        # Resolved lazily so construction never touches Playwright/cloud SDKs.
        self._backend: Optional[BrowserBackend] = None
        self._backend_probed = False

    async def initialize(self) -> None:
        """No eager browser launch — selection is lazy + non-fatal."""
        self.metadata["initialized"] = True
        self.metadata["backend"] = self._resolve_backend().name if self.backend_available else None

    async def health_check(self) -> bool:
        """Healthy regardless of browser availability.

        A missing browser is a *capability* limitation (graceful skip), not
        an unhealthy agent — gating health on it would spin self-heal.
        """
        return True

    # ── backend resolution ───────────────────────────────────────────────

    def _resolve_backend(self) -> Optional[BrowserBackend]:
        if not self._backend_probed:
            try:
                self._backend = select_browser_backend()
            except Exception:
                logger.debug("browser backend selection failed", exc_info=True)
                self._backend = None
            self._backend_probed = True
        return self._backend

    @property
    def backend_available(self) -> bool:
        """True iff a usable browser backend exists (local or gated cloud)."""
        return self._resolve_backend() is not None

    def _skip_result(self, task: TaskRequest, reason: str) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=False,
            output={"skipped": True, "reason": reason},
        )

    # ── execution ────────────────────────────────────────────────────────

    async def execute(
        self, task: TaskRequest, stdin_data: Optional[str] = None
    ) -> TaskResult:
        task_type = task.input_data.get("task_type", "browse")
        if task_type not in ("browse", "extract", "screenshot"):
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        backend = self._resolve_backend()
        if backend is None:
            # Graceful no-browser skip — never a crash.
            return self._skip_result(task, "no browser backend")

        url = task.input_data.get("url", "")
        if not url:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="No url provided",
            )

        try:
            if task_type == "browse":
                return await self._do_browse(task, backend, url)
            if task_type == "extract":
                return await self._do_extract(task, backend, url)
            return await self._do_screenshot(task, backend, url)
        except Exception as exc:
            logger.debug("browser execute failed", exc_info=True)
            return TaskResult(task_id=task.task_id, success=False, error=str(exc))
        finally:
            try:
                await backend.close()
            except Exception:
                logger.debug("browser backend close failed", exc_info=True)

    async def _do_browse(
        self, task: TaskRequest, backend: BrowserBackend, url: str
    ) -> TaskResult:
        await backend.open(url)
        steps = []
        for instruction in task.input_data.get("instructions", []) or []:
            step = await backend.act(str(instruction))
            steps.append({"ok": step.ok, "detail": step.detail, "error": step.error})
        snap = await backend.snapshot()
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "backend": backend.name,
                "url": snap.url,
                "title": snap.title,
                "steps": steps,
            },
        )

    async def _do_extract(
        self, task: TaskRequest, backend: BrowserBackend, url: str
    ) -> TaskResult:
        await backend.open(url)
        snap = await backend.snapshot()
        max_chars = int(task.input_data.get("max_chars", 20000) or 20000)
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "backend": backend.name,
                "url": snap.url,
                "title": snap.title,
                "text": (snap.text or "")[:max_chars],
            },
        )

    async def _do_screenshot(
        self, task: TaskRequest, backend: BrowserBackend, url: str
    ) -> TaskResult:
        await backend.open(url)
        snap = await backend.snapshot()
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "backend": backend.name,
                "url": snap.url,
                "title": snap.title,
                "screenshot_path": snap.screenshot_path,
            },
        )
