"""Vision (multimodal) support for the OpenRouter adapter + design_vision.

Two surfaces:
  1. OpenRouterBackend.complete() must build an OpenAI-style multimodal
     ``content`` parts list (text + image_url data URL) when LLMRequest.images
     is set, and stay a BARE STRING (byte-identical to the old behavior) when
     images is empty.
  2. design_vision.score_screenshot() must try the OpenRouter vision path
     FIRST, return the coerced rubric dict, cache it by image hash, and
     degrade to (None) on any backend failure — never burning a real call in
     pytest (the backend is fully mocked).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from skyn3t.adapters import openrouter
from skyn3t.adapters.llm_client import LLMRequest
from skyn3t.agents import design_vision

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-pixels" * 8


class _CapturingResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, content: str = "ok"):
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "choices": [{"message": {"content": self._content}}],
        }


class _CapturingClient:
    """Records the JSON payload of the last POST without any network."""

    def __init__(self, content: str = "ok"):
        self.last_payload = None
        self._content = content

    async def post(self, _path, json=None, **_kwargs):  # noqa: A002
        self.last_payload = json
        return _CapturingResp(self._content)

    async def aclose(self) -> None:
        return None


# ── adapter: text-only stays a bare string (no regression) ───────────────


def test_complete_text_only_content_is_bare_string():
    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")
    client = _CapturingClient()
    backend._client = client

    out = asyncio.run(backend.complete(LLMRequest(prompt="hello", max_tokens=4)))
    assert out == "ok"
    msgs = client.last_payload["messages"]
    user = [m for m in msgs if m["role"] == "user"][0]
    # Byte-identical to the legacy path: plain string, not a parts list.
    assert user["content"] == "hello"
    assert isinstance(user["content"], str)


# ── adapter: images → multimodal parts list with base64 data URL ─────────


def test_complete_with_images_builds_multimodal_parts(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)

    backend = openrouter.OpenRouterBackend(api_key="sk-or-test")
    client = _CapturingClient()
    backend._client = client

    out = asyncio.run(
        backend.complete(
            LLMRequest(prompt="describe this", model="openai/gpt-4o-mini",
                       images=[str(img)], max_tokens=50)
        )
    )
    assert out == "ok"
    user = [m for m in client.last_payload["messages"] if m["role"] == "user"][0]
    parts = user["content"]
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "describe this"}
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # The embedded base64 round-trips back to the original bytes.
    decoded = base64.b64decode(url.split(",", 1)[1])
    assert decoded == _PNG_BYTES
    # The model override is honored.
    assert client.last_payload["model"] == "openai/gpt-4o-mini"


def test_image_to_data_url_guesses_jpeg(tmp_path):
    img = tmp_path / "shot.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    url = openrouter._image_to_data_url(str(img))
    assert url.startswith("data:image/jpeg;base64,")


def test_build_user_content_multiple_images(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(_PNG_BYTES)
    b.write_bytes(_PNG_BYTES)
    parts = openrouter._build_user_content(
        LLMRequest(prompt="two", images=[str(a), str(b)])
    )
    assert isinstance(parts, list)
    assert len(parts) == 3  # text + 2 images
    assert sum(1 for p in parts if p.get("type") == "image_url") == 2


# ── design_vision: OpenRouter vision path (fully mocked) ──────────────────


@pytest.fixture
def _live_key(monkeypatch):
    """Make _try_openrouter_score believe a key is present without a real one.
    conftest sets OPENROUTER_API_KEY='' (empty) for all tests."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # No vision CLI installed → only the OpenRouter path can produce a score.
    monkeypatch.setattr(design_vision.shutil, "which", lambda _n: None)


class _FakeBackend:
    """Stand-in for OpenRouterBackend: returns a canned JSON string, records
    the LLMRequest it was handed, never touches the network."""

    instances: list = []

    def __init__(self, *_a, **_k):
        self.requests: list = []
        _FakeBackend.instances.append(self)

    async def complete(self, req):
        self.requests.append(req)
        return (
            '```json\n{"score": 84, "verdict": "pass", '
            '"reasons": ["deliberate type scale", "real layout rhythm"], '
            '"generic_ai_tells": []}\n```'
        )

    async def aclose(self):
        return None


def _install_fake_backend(monkeypatch, backend_cls=_FakeBackend):
    _FakeBackend.instances = []
    monkeypatch.setattr(
        "skyn3t.adapters.openrouter.OpenRouterBackend", backend_cls
    )


def test_score_screenshot_uses_openrouter_first(tmp_path, monkeypatch, _live_key):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    _install_fake_backend(monkeypatch)

    result = asyncio.run(
        design_vision.score_screenshot(img, brief="a fitness tracker", mood="bold")
    )
    assert result is not None
    assert result["score"] == 84
    assert result["verdict"] == "pass"
    assert result["reasons"] == ["deliberate type scale", "real layout rhythm"]
    assert result["generic_ai_tells"] == []

    # The backend got the image attached inline + the configured vision model.
    be = _FakeBackend.instances[0]
    req = be.requests[0]
    assert req.images == [str(img)]
    assert req.model == design_vision.DEFAULT_VISION_MODEL
    assert req.temperature == 0.0


def test_score_screenshot_respects_vision_model_env(tmp_path, monkeypatch, _live_key):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("SKYN3T_VISION_MODEL", "openai/gpt-4.1-mini")
    _install_fake_backend(monkeypatch)

    asyncio.run(design_vision.score_screenshot(img))
    req = _FakeBackend.instances[0].requests[0]
    assert req.model == "openai/gpt-4.1-mini"


def test_score_screenshot_caches_by_image_hash(tmp_path, monkeypatch, _live_key):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    # Cache under tmp so we don't pollute the repo data dir.
    monkeypatch.setattr(
        design_vision, "_score_cache_dir", lambda: tmp_path / "score_cache"
    )
    _install_fake_backend(monkeypatch)

    first = asyncio.run(design_vision.score_screenshot(img, brief="x"))
    assert first is not None
    assert len(_FakeBackend.instances) == 1  # one live call

    # Second identical call: served from cache, NO new backend constructed.
    second = asyncio.run(design_vision.score_screenshot(img, brief="x"))
    assert second == first
    assert len(_FakeBackend.instances) == 1  # still just the one


def test_score_screenshot_backend_failure_degrades_to_none(tmp_path, monkeypatch, _live_key):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)

    class _BoomBackend(_FakeBackend):
        async def complete(self, req):
            self.requests.append(req)
            raise RuntimeError("403 key exhausted")

    _install_fake_backend(monkeypatch, _BoomBackend)
    # No CLI either → score is unavailable → None (never breaks the build).
    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is None


def test_score_screenshot_non_json_response_degrades_to_none(tmp_path, monkeypatch, _live_key):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)

    class _GarbageBackend(_FakeBackend):
        async def complete(self, req):
            self.requests.append(req)
            return "I cannot score this image, sorry."

    _install_fake_backend(monkeypatch, _GarbageBackend)
    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is None


def test_score_screenshot_no_key_skips_openrouter(tmp_path, monkeypatch):
    """With no key, the OpenRouter path is skipped entirely (no backend
    constructed) and we fall through to the CLI ladder."""
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr(design_vision.shutil, "which", lambda _n: None)

    constructed = []
    monkeypatch.setattr(
        "skyn3t.adapters.openrouter.OpenRouterBackend",
        lambda *a, **k: constructed.append(1),
    )
    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is None
    assert constructed == []  # OpenRouter never attempted without a key


def test_score_screenshot_openrouter_then_cli_fallback(tmp_path, monkeypatch):
    """When OpenRouter yields nothing usable but a CLI is present, the CLI
    fallback still runs and produces a score."""
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    class _NullBackend(_FakeBackend):
        async def complete(self, req):
            self.requests.append(req)
            return "garbage not json"

    _install_fake_backend(monkeypatch, _NullBackend)
    monkeypatch.setattr(design_vision.shutil, "which", lambda _n: "/usr/bin/claude")

    async def fake_run_cli(args, stdin_data=None):  # noqa: ARG001
        return '{"score": 71, "verdict": "pass", "reasons": ["ok"], "generic_ai_tells": []}'

    monkeypatch.setattr(design_vision, "_run_cli", fake_run_cli)

    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is not None
    assert result["score"] == 71


# ── build_verifier consumer: real score flows through _maybe_score ───────


def test_build_verifier_maybe_score_gets_real_score(tmp_path, monkeypatch):
    """_maybe_score_screenshot returns the real (score, reasons) now that
    score_screenshot has a working backend path."""
    from skyn3t.agents.build_verifier import BuildVerifierAgent

    async def fake_score(image_path, *, brief="", mood=""):  # noqa: ARG001
        return {
            "score": 42,
            "verdict": "fail",
            "reasons": ["safe but generic", "weak hierarchy"],
            "generic_ai_tells": ["indigo gradient"],
        }

    monkeypatch.setattr(design_vision, "score_screenshot", fake_score)

    agent = BuildVerifierAgent.__new__(BuildVerifierAgent)
    score, reasons = asyncio.run(
        agent._maybe_score_screenshot(Path("/x/shot.png"), brief="b", mood="m")
    )
    assert score == 42
    assert "safe but generic" in reasons
    assert any("generic-AI tells" in r for r in reasons)
