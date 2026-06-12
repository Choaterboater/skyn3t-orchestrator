"""Unified delivery gateway for SkyN3t (Phase 5B).

Before this module, outbound delivery was ad hoc:

* ``studio/notify_dispatcher.py`` — Discord-only approval/clarification
  notifications.
* ``MessagingRouter.reply()`` — per-platform reply path, but only ever
  driven by the inbound message → agent → reply flow.

Nothing consumed the scheduler's ``EventType.SYSTEM_ALERT`` /
``kind=scheduled_job_triggered`` events, so scheduled reports and
briefings had nowhere to go. This module adds the single delivery
surface every scheduled job / briefing / report routes through.

Design goals (all from the GATEWAY contract):

* ``DeliveryGateway.deliver()`` is the one entry point. It resolves the
  destination channel by name through the process-wide
  ``MessagingRouter`` (``get_default_router()``) and forwards to
  ``MessagingChannel.send`` via ``router.reply()``.
* Unconfigured / unavailable channel => ``DeliveryResult(skipped=True)``
  — *never* an error, *never* a raise. The gateway is best-effort: a
  missing Telegram token must not crash a scheduled briefing that also
  goes to Slack.
* ``channels()`` reflects only *usable* channels — it reads
  ``MessagingRouter.available_platforms()`` (the additive gate the
  CHANNELS owners expose) so the status endpoint shows real availability.
* A thin **event bridge** (``make_scheduled_delivery_bridge`` /
  ``register_scheduled_delivery_bridge``) is the consumer for the
  scheduler's emitted events: given a ``scheduled_job_triggered`` event
  whose ``payload['delivery'] == {channel, to}`` plus the job result
  text, it calls ``deliver()``. This is wired with
  ``EventBus.subscribe(bridge, EventType.SYSTEM_ALERT)``.

Nothing here imports an SDK or does network I/O at import time. Email /
Discord "bridge" delivery is lazy-imported inside the relevant helper so
this module imports cleanly even when those deps are absent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.integrations.messaging import (
    MessagingRouter,
    get_default_router,
)

logger = logging.getLogger("skyn3t.integrations.gateway")


# Bridge channel ids that are NOT MessagingRouter platforms — they have
# their own delivery surface (notify_dispatcher / EmailAgent SMTP).
_EMAIL_BRIDGE = "email"
_DISCORD_BRIDGE = "discord"


@dataclass
class DeliveryResult:
    """Outcome of a single ``deliver()`` call.

    ``ok`` True  => delivered (best-effort: the underlying channel was
                    asked to send and did not raise).
    ``skipped``  => the channel is unconfigured / unavailable; this is a
                    benign no-op, not an error. ``ok`` is False when
                    skipped (nothing was delivered) but ``error`` stays
                    None so callers can distinguish "off" from "broke".
    """

    channel: str
    ok: bool
    error: Optional[str] = None
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "error": self.error,
            "skipped": self.skipped,
        }


class DeliveryGateway:
    """Single outbound-delivery surface.

    Routes a (channel, to, text) tuple through the messaging router. A
    couple of channel names are "bridges" to non-router delivery paths:

      * ``'email'``   — SMTP via the EmailAgent helper (uses ``subject``).
      * ``'discord'`` — the existing notify_dispatcher webhook path.

    Everything else is treated as a MessagingRouter platform name
    (telegram, slack, whatsapp, matrix, signal, …).
    """

    def __init__(self, router: Optional[MessagingRouter] = None):
        # Default to the process-wide router so the gateway and the
        # inbound message flow share the same registered channels.
        self._router = router if router is not None else get_default_router()

    # ── introspection ────────────────────────────────────────────────

    def channels(self) -> List[str]:
        """Configured + available channel names.

        Prefers ``MessagingRouter.available_platforms()`` (the additive
        availability gate the CHANNELS owners expose); falls back to
        ``platforms()`` if that method isn't present yet (defensive —
        these owners ship concurrently). Bridge channels (email/discord)
        are appended only when their config is actually present so the
        status endpoint reflects reality.
        """
        names: List[str] = []
        # available_platforms() is the new, availability-aware accessor.
        avail = getattr(self._router, "available_platforms", None)
        try:
            if callable(avail):
                names = list(avail())
            else:
                names = list(self._router.platforms())
        except Exception:
            logger.debug("gateway.channels(): router introspection failed", exc_info=True)
            names = []
        # Surface bridge channels only when usable.
        if _email_available() and _EMAIL_BRIDGE not in names:
            names.append(_EMAIL_BRIDGE)
        if _discord_available() and _DISCORD_BRIDGE not in names:
            names.append(_DISCORD_BRIDGE)
        return names

    # ── core delivery ────────────────────────────────────────────────

    async def deliver(
        self,
        *,
        channel: str,
        to: str,
        text: str,
        thread: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> DeliveryResult:
        """Deliver ``text`` to ``to`` on ``channel``.

        * Resolves the channel through the router and forwards via
          ``router.reply()``.
        * ``email``/``discord`` are bridged to their own surfaces;
          ``subject`` is used only by the email bridge and ignored
          elsewhere (per contract).
        * Unknown / unconfigured / unavailable channel =>
          ``DeliveryResult(skipped=True)``. Never raises.
        """
        chan = (channel or "").strip().lower()
        if not chan:
            return DeliveryResult(channel="", ok=False, skipped=True)
        if not text:
            # Nothing to send — treat as a benign skip rather than error.
            return DeliveryResult(channel=chan, ok=False, skipped=True)

        # Bridge channels first.
        if chan == _EMAIL_BRIDGE:
            return await self._deliver_email(to=to, text=text, subject=subject)
        if chan == _DISCORD_BRIDGE:
            return await self._deliver_discord(to=to, text=text)

        # MessagingRouter platform path.
        ch = None
        try:
            ch = self._router.get(chan)
        except Exception:
            logger.debug("gateway.deliver(): router.get(%s) failed", chan, exc_info=True)
            ch = None
        if ch is None:
            # No such channel registered => skipped, not an error.
            return DeliveryResult(channel=chan, ok=False, skipped=True)
        # Honor the additive is_available() gate when the adapter exposes
        # it: an unconfigured-but-registered channel is a skip, not a
        # failed delivery.
        if not _channel_is_available(ch):
            return DeliveryResult(channel=chan, ok=False, skipped=True)
        try:
            await self._router.reply(chan, to, text, thread=thread)
            return DeliveryResult(channel=chan, ok=True)
        except Exception as exc:  # never propagate — gateway is best-effort
            logger.warning("gateway.deliver(%s) failed: %s", chan, exc)
            return DeliveryResult(channel=chan, ok=False, error=str(exc))

    async def broadcast(
        self,
        *,
        channels: List[str],
        to_map: Dict[str, str],
        text: str,
    ) -> List[DeliveryResult]:
        """Deliver the same ``text`` to several channels at once.

        ``to_map`` maps a channel name to its destination id. A channel
        with no entry in ``to_map`` is skipped (no destination known).
        Each delivery is independent — one failure never aborts the rest.
        """
        results: List[DeliveryResult] = []
        for chan in channels or []:
            to = (to_map or {}).get(chan)
            if not to:
                results.append(DeliveryResult(channel=chan, ok=False, skipped=True))
                continue
            results.append(await self.deliver(channel=chan, to=to, text=text))
        return results

    # ── bridge helpers (lazy, non-raising) ───────────────────────────

    async def _deliver_email(
        self, *, to: str, text: str, subject: Optional[str]
    ) -> DeliveryResult:
        """Bridge to SMTP via the EmailAgent helper.

        Lazy-imports EmailAgent so a missing email config / SDK never
        breaks gateway import. Unconfigured SMTP => skipped.
        """
        if not _email_available():
            return DeliveryResult(channel=_EMAIL_BRIDGE, ok=False, skipped=True)
        if not to:
            return DeliveryResult(channel=_EMAIL_BRIDGE, ok=False, skipped=True)
        try:
            from skyn3t.integrations.email_agent import EmailAgent  # lazy

            agent = EmailAgent()
            await agent._send_email(  # type: ignore[attr-defined]
                to=to,
                subject=subject or "SkyN3t notification",
                body=text,
            )
            return DeliveryResult(channel=_EMAIL_BRIDGE, ok=True)
        except TypeError:
            # _send_email signature differs in this build — degrade to a
            # skip rather than crash the whole delivery batch.
            logger.debug("email bridge: _send_email signature mismatch", exc_info=True)
            return DeliveryResult(channel=_EMAIL_BRIDGE, ok=False, skipped=True)
        except Exception as exc:
            logger.warning("gateway email bridge failed: %s", exc)
            return DeliveryResult(channel=_EMAIL_BRIDGE, ok=False, error=str(exc))

    async def _deliver_discord(self, *, to: str, text: str) -> DeliveryResult:
        """Bridge to Discord via the notify_dispatcher webhook path.

        ``to`` is treated as a Discord webhook URL. Lazy-imports httpx
        so the gateway imports without it. Unconfigured => skipped.
        """
        if not to:
            return DeliveryResult(channel=_DISCORD_BRIDGE, ok=False, skipped=True)
        try:
            import httpx  # type: ignore  # lazy
        except Exception:
            return DeliveryResult(channel=_DISCORD_BRIDGE, ok=False, skipped=True)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(to, json={"content": text[:1900]})
                if resp.status_code >= 400:
                    return DeliveryResult(
                        channel=_DISCORD_BRIDGE,
                        ok=False,
                        error=f"discord webhook {resp.status_code}",
                    )
            return DeliveryResult(channel=_DISCORD_BRIDGE, ok=True)
        except Exception as exc:
            logger.warning("gateway discord bridge failed: %s", exc)
            return DeliveryResult(channel=_DISCORD_BRIDGE, ok=False, error=str(exc))


# ── availability probes (pure, non-raising) ──────────────────────────


def _channel_is_available(ch: Any) -> bool:
    """True when a registered channel reports itself available.

    The additive ``is_available()`` gate (CHANNELS contract) is optional
    on older adapters; absence => assume available (registration alone
    used to imply usability). Never raises.
    """
    probe = getattr(ch, "is_available", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:
        return False


def _email_available() -> bool:
    """True when SMTP delivery is configured. Pure; never raises."""
    import os

    return bool(
        os.getenv("EMAIL_SMTP_HOST")
        and os.getenv("EMAIL_ADDRESS")
        and os.getenv("EMAIL_PASSWORD")
    )


def _discord_available() -> bool:
    """True when a Discord webhook is configured. Pure; never raises."""
    import os

    return bool(os.getenv("DISCORD_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL"))


# ── scheduled-job → delivery event bridge ────────────────────────────


def extract_delivery(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Pull ``{channel, to}`` from a scheduled-job event payload.

    The scheduler emits ``SYSTEM_ALERT`` events with
    ``payload = {kind, job_id, name, task_type, payload: {...}}`` and the
    NL-schedule hook may attach ``delivery: {channel, to}`` to the
    *inner* job payload (``ScheduledJob.payload['delivery']``). Some
    callers may also attach it at the top level. Check both. Returns None
    when no usable delivery target is present.
    """
    if not isinstance(payload, dict):
        return None
    candidates = []
    inner = payload.get("payload")
    if isinstance(inner, dict):
        candidates.append(inner.get("delivery"))
    candidates.append(payload.get("delivery"))
    for delivery in candidates:
        if isinstance(delivery, dict):
            channel = str(delivery.get("channel") or "").strip()
            to = str(delivery.get("to") or "").strip()
            if channel and to:
                return {"channel": channel, "to": to}
    return None


def _result_text(payload: Dict[str, Any]) -> str:
    """Best-effort: derive the human-facing text for a triggered job.

    Prefers an explicit ``result``/``text``/``output`` on the event (set
    by whatever ran the job), else falls back to the job name so a bare
    "briefing fired" still reaches the user.
    """
    inner = payload.get("payload") if isinstance(payload, dict) else None
    for source in (payload, inner):
        if not isinstance(source, dict):
            continue
        for key in ("result", "text", "output", "message", "body"):
            val = source.get(key)
            if isinstance(val, str) and val.strip():
                return val
    name = payload.get("name") if isinstance(payload, dict) else None
    if isinstance(name, str) and name.strip():
        return f"Scheduled job '{name}' ran."
    return "Scheduled job ran."


def make_scheduled_delivery_bridge(
    gateway: Optional[DeliveryGateway] = None,
    *,
    kinds: Optional[List[str]] = None,
) -> Callable[[Event], None]:
    """Build a SYSTEM_ALERT subscriber that delivers triggered jobs.

    Returns a *synchronous* callback (EventBus dispatches callbacks
    synchronously) suitable for
    ``event_bus.subscribe(bridge, EventType.SYSTEM_ALERT)``.

    On each matching event it extracts ``payload['delivery']`` and the
    job result text and calls ``gateway.deliver()``. The async deliver is
    scheduled on the running loop when there is one (the scheduler runs
    inside an event loop), else driven to completion with
    ``asyncio.run`` so synchronous callers/tests still deliver. The
    callback never raises — a delivery problem must not poison the bus.
    """
    gw = gateway if gateway is not None else get_gateway()
    accepted = set(kinds or ["scheduled_job_triggered", "reminder_triggered"])

    def _bridge(event: Event) -> None:
        try:
            if event.event_type != EventType.SYSTEM_ALERT:
                return
            payload = event.payload or {}
            if payload.get("kind") not in accepted:
                return
            delivery = extract_delivery(payload)
            if delivery is None:
                return  # no delivery target configured for this job
            text = _result_text(payload)
            subject = None
            name = payload.get("name")
            if isinstance(name, str) and name.strip():
                subject = name
            coro = gw.deliver(
                channel=delivery["channel"],
                to=delivery["to"],
                text=text,
                subject=subject,
            )
            _run_coro(coro)
        except Exception:
            logger.debug("scheduled delivery bridge failed", exc_info=True)

    return _bridge


def register_scheduled_delivery_bridge(
    event_bus: EventBus,
    gateway: Optional[DeliveryGateway] = None,
) -> Callable[[Event], None]:
    """Subscribe a scheduled-delivery bridge to ``event_bus`` and return
    the callback (so callers can ``unsubscribe`` it later)."""
    bridge = make_scheduled_delivery_bridge(gateway)
    event_bus.subscribe(bridge, EventType.SYSTEM_ALERT)
    return bridge


def _run_coro(coro: Any) -> None:
    """Drive an awaitable to completion from a sync context, safely.

    * If a loop is already running (the scheduler's loop), schedule the
      coroutine as a task and let it run without blocking the publisher.
    * Otherwise run it to completion with ``asyncio.run``.
    Never raises; a scheduling failure is logged and swallowed so the
    event bus keeps dispatching.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is not None and loop.is_running():
            loop.create_task(coro)
        else:
            asyncio.run(coro)
    except Exception:
        logger.debug("scheduled delivery coroutine scheduling failed", exc_info=True)
        # Best-effort: close the coroutine so we don't leak a warning.
        try:
            coro.close()
        except Exception:
            pass


# ── process-wide singleton ───────────────────────────────────────────

_default_gateway: Optional[DeliveryGateway] = None


def get_gateway() -> DeliveryGateway:
    """Return the process-wide delivery gateway (lazy)."""
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = DeliveryGateway()
    return _default_gateway
