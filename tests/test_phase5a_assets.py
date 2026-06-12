"""Phase 5A — AssetAgent + design-import + feature-tier scaffolds (M6).

Covers AssetAgentAPI: flag-off inert, no-key graceful skip (no network),
fake provider writes asset bytes to a tmp dir, import_design_tokens with a
fake design_vision.extract, the design_vision non-photo seam, the
service_brand_kit generated-asset block, and the stack_templates
auth/payments/email feature tiers.

No network, no real LLM, no orchestrator. Everything is monkeypatched or
flag-gated off by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.agents import asset_agent
from skyn3t.agents.asset_agent import (
    AssetAgent,
    asset_gen_enabled,
    import_design_tokens,
    register_provider_hook,
)
from skyn3t.core.agent import TaskRequest

# ── Fixtures / helpers ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_hooks():
    """Each test starts with an empty provider registry."""
    asset_agent._PROVIDER_HOOKS.clear()
    yield
    asset_agent._PROVIDER_HOOKS.clear()


def _specs():
    return [
        {"kind": "logo", "prompt": "a logo", "out_path": "assets/logo.png"},
        {"kind": "image", "prompt": "a hero", "out_path": "assets/hero.png"},
    ]


# ── Flag gating ─────────────────────────────────────────────────────────


def test_asset_gen_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SKYN3T_ASSET_GEN", raising=False)
    assert asset_gen_enabled() is False


def test_asset_gen_flag_truthy(monkeypatch):
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("SKYN3T_ASSET_GEN", val)
        assert asset_gen_enabled() is True
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "0")
    assert asset_gen_enabled() is False


@pytest.mark.asyncio
async def test_flag_off_is_inert(monkeypatch, tmp_path: Path):
    """Flag off → every asset skipped, no provider consulted, no files."""
    monkeypatch.delenv("SKYN3T_ASSET_GEN", raising=False)
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")  # key present but flag off

    called = {"n": 0}

    def hook(_req):
        called["n"] += 1
        return b"PNGDATA"

    register_provider_hook("replicate", hook)

    agent = AssetAgent()
    await agent.initialize()
    task = TaskRequest(
        input_data={"brief": "b", "artifact_dir": str(tmp_path), "asset_specs": _specs()}
    )
    result = await agent.execute(task)

    assert result.success is True
    assets = result.output["assets"]
    assert len(assets) == 2
    assert all(a["skipped"] for a in assets)
    assert all(a.get("skipped_reason") == "asset_gen_disabled" for a in assets)
    assert called["n"] == 0  # provider never invoked
    # No bytes written to disk.
    assert not (tmp_path / "assets" / "logo.png").exists()


# ── No-key graceful skip (no network) ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_key_skips_without_network(monkeypatch, tmp_path: Path):
    """Flag on but no REPLICATE_API_TOKEN → skip, never touch network."""
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)

    called = {"n": 0}

    def hook(_req):
        called["n"] += 1
        return b"PNGDATA"

    register_provider_hook("replicate", hook)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={"brief": "b", "artifact_dir": str(tmp_path), "asset_specs": _specs()}
    )
    result = await agent.execute(task)

    assert result.success is True
    assets = result.output["assets"]
    assert len(assets) == 2
    assert all(a["skipped"] for a in assets)
    assert called["n"] == 0
    assert not (tmp_path / "assets" / "logo.png").exists()


@pytest.mark.asyncio
async def test_flag_on_key_present_but_no_hook_skips(monkeypatch, tmp_path: Path):
    """Flag + key but no registered provider hook → graceful skip."""
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")
    # registry empty (autouse fixture)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={"brief": "b", "artifact_dir": str(tmp_path), "asset_specs": _specs()}
    )
    result = await agent.execute(task)

    assert result.success is True
    assert all(a["skipped"] for a in result.output["assets"])


# ── Happy path: fake provider writes bytes ──────────────────────────────


@pytest.mark.asyncio
async def test_fake_provider_writes_assets(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")

    seen_requests = []

    def hook(req):
        seen_requests.append(req)
        return ("FAKEBYTES-" + req["kind"]).encode("utf-8")

    register_provider_hook("replicate", hook)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={"brief": "b", "artifact_dir": str(tmp_path), "asset_specs": _specs()}
    )
    result = await agent.execute(task)

    assert result.success is True
    assets = result.output["assets"]
    assert len(assets) == 2
    assert all(not a["skipped"] for a in assets)
    assert all(a["provider"] == "replicate" for a in assets)

    logo = tmp_path / "assets" / "logo.png"
    hero = tmp_path / "assets" / "hero.png"
    assert logo.read_bytes() == b"FAKEBYTES-logo"
    assert hero.read_bytes() == b"FAKEBYTES-image"

    # scaffolds reference only written assets.
    assert str(logo) in result.output["scaffolds"]
    assert str(hero) in result.output["scaffolds"]
    # default dims threaded through when spec omits w/h.
    assert seen_requests[0]["w"] > 0 and seen_requests[0]["h"] > 0


@pytest.mark.asyncio
async def test_async_provider_hook_supported(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")

    async def hook(req):
        return b"ASYNC-" + req["kind"].encode()

    register_provider_hook("replicate", hook)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={
            "brief": "b",
            "artifact_dir": str(tmp_path),
            "asset_specs": [{"kind": "icon", "out_path": "assets/i.svg"}],
        }
    )
    result = await agent.execute(task)
    assert result.success is True
    assert (tmp_path / "assets" / "i.svg").read_bytes() == b"ASYNC-icon"


@pytest.mark.asyncio
async def test_provider_returning_none_skips_gracefully(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")

    def hook(_req):
        return None  # provider couldn't produce

    register_provider_hook("replicate", hook)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={
            "brief": "b",
            "artifact_dir": str(tmp_path),
            "asset_specs": [{"kind": "logo", "out_path": "assets/logo.png"}],
        }
    )
    result = await agent.execute(task)
    assert result.success is True
    asset = result.output["assets"][0]
    assert asset["skipped"] is True
    assert asset["skipped_reason"] == "provider_returned_nothing"


@pytest.mark.asyncio
async def test_provider_raising_does_not_block(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "tok")

    def hook(_req):
        raise RuntimeError("boom")

    register_provider_hook("replicate", hook)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={
            "brief": "b",
            "artifact_dir": str(tmp_path),
            "asset_specs": [{"kind": "logo", "out_path": "assets/logo.png"}],
        }
    )
    result = await agent.execute(task)
    assert result.success is True  # never blocks
    assert result.output["assets"][0]["skipped"] is True


# ── import_design_tokens ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_design_tokens_image_uses_extract(monkeypatch, tmp_path: Path):
    from skyn3t.agents.design_vision import DesignReference, PaletteEntry

    fake_ref = DesignReference(
        image_path="x.png",
        image_sha="abc",
        palette=[PaletteEntry(name="ink", hex="#0B1220", role="bg")],
        typography_vibe="geometric sans",
        mood=["techno-precision"],
        forbidden_words=["cozy"],
    )

    async def fake_extract(path):
        assert isinstance(path, Path)
        return fake_ref

    monkeypatch.setattr("skyn3t.agents.design_vision.extract", fake_extract)

    tokens = await import_design_tokens(source="image", ref=tmp_path / "ref.png")
    assert tokens is not None
    assert tokens["source"] == "image"
    assert tokens["palette"][0]["hex"] == "#0B1220"
    assert tokens["typography_vibe"] == "geometric sans"
    assert "cozy" in tokens["forbidden_words"]


@pytest.mark.asyncio
async def test_import_design_tokens_extract_none(monkeypatch, tmp_path: Path):
    async def fake_extract(_path):
        return None

    monkeypatch.setattr("skyn3t.agents.design_vision.extract", fake_extract)
    tokens = await import_design_tokens(source="image", ref=tmp_path / "ref.png")
    assert tokens is None


@pytest.mark.asyncio
async def test_import_design_tokens_figma_no_key_skips(monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.delenv("FIGMA_API_TOKEN", raising=False)
    tokens = await import_design_tokens(source="figma", ref="file-key")
    assert tokens is None


@pytest.mark.asyncio
async def test_import_design_tokens_figma_with_hook(monkeypatch):
    monkeypatch.setenv("FIGMA_TOKEN", "tok")

    def figma_hook(req):
        assert req["source"] == "figma"
        return {"source": "figma", "palette": [{"name": "p", "hex": "#fff", "role": "bg"}]}

    register_provider_hook("figma_tokens", figma_hook)
    tokens = await import_design_tokens(source="figma", ref="file-key")
    assert tokens is not None
    assert tokens["source"] == "figma"


@pytest.mark.asyncio
async def test_import_design_tokens_unknown_source():
    assert await import_design_tokens(source="bogus", ref="x") is None


@pytest.mark.asyncio
async def test_execute_with_design_import(monkeypatch, tmp_path: Path):
    """execute() folds imported tokens into output when design_import set."""
    monkeypatch.delenv("SKYN3T_ASSET_GEN", raising=False)  # gen off; import independent

    async def fake_import(*, source, ref):  # noqa: ARG001
        return {"source": source, "palette": [{"name": "a", "hex": "#abc", "role": "accent"}]}

    monkeypatch.setattr("skyn3t.agents.asset_agent.import_design_tokens", fake_import)

    agent = AssetAgent()
    task = TaskRequest(
        input_data={
            "brief": "b",
            "artifact_dir": str(tmp_path),
            "asset_specs": [],
            "design_import": {"source": "figma", "ref": "key"},
        }
    )
    result = await agent.execute(task)
    assert result.success is True
    assert result.output["tokens"]["source"] == "figma"
    assert "design_tokens.imported" in result.output["scaffolds"]


# ── design_vision non-photo seam ────────────────────────────────────────


def test_design_reference_from_tokens():
    from skyn3t.agents.design_vision import design_reference_from_tokens

    ref = design_reference_from_tokens(
        {
            "palette": [
                {"name": "bg", "hex": "#000", "role": "bg"},
                {"name": "weird", "hex": "#123456", "role": "made-up-role"},
            ],
            "typography_vibe": "mono",
            "mood": ["sharp"],
        },
        source_label="figma",
    )
    assert ref is not None
    assert ref.image_path == "<figma>"
    assert ref.palette[0].role == "bg"
    # unknown role normalized to accent.
    assert ref.palette[1].role == "accent"


def test_design_reference_from_tokens_empty():
    from skyn3t.agents.design_vision import design_reference_from_tokens

    assert design_reference_from_tokens({"palette": []}) is None
    assert design_reference_from_tokens({}) is None
    assert design_reference_from_tokens("notadict") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_extract_design_source_image_delegates(monkeypatch, tmp_path: Path):
    from skyn3t.agents import design_vision

    called = {"n": 0}

    async def fake_extract(path):
        called["n"] += 1
        return None

    monkeypatch.setattr(design_vision, "extract", fake_extract)
    await design_vision.extract_design_source(source="image", ref=tmp_path / "x.png")
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_extract_design_source_figma_coerces_tokens():
    from skyn3t.agents import design_vision

    ref = await design_vision.extract_design_source(
        source="figma",
        ref="key",
        tokens={"palette": [{"name": "p", "hex": "#fff", "role": "accent"}]},
    )
    assert ref is not None
    assert ref.palette[0].hex == "#fff"


@pytest.mark.asyncio
async def test_extract_design_source_figma_no_tokens_none():
    from skyn3t.agents import design_vision

    assert await design_vision.extract_design_source(source="figma", ref="key") is None


# ── service_brand_kit generated-asset block ─────────────────────────────


def test_generated_assets_markdown_only_written():
    from skyn3t.agents.service_brand_kit import generated_assets_markdown

    md = generated_assets_markdown(
        [
            {"kind": "logo", "path": "assets/logo.png", "provider": "replicate", "skipped": False},
            {"kind": "hero", "path": "assets/hero.png", "provider": "replicate", "skipped": True},
        ]
    )
    assert "assets/logo.png" in md
    assert "assets/hero.png" not in md  # skipped one excluded


def test_generated_assets_markdown_empty():
    from skyn3t.agents.service_brand_kit import generated_assets_markdown

    assert generated_assets_markdown(None) == ""
    assert generated_assets_markdown([]) == ""
    assert generated_assets_markdown([{"kind": "x", "path": "p", "skipped": True}]) == ""


# ── stack_templates feature tiers (auth/payments/email) ─────────────────


def test_detect_feature_tiers():
    from skyn3t.agents.stack_templates import _detect_feature_tiers

    assert _detect_feature_tiers("") == []
    assert _detect_feature_tiers("a static landing page") == []
    assert "auth" in _detect_feature_tiers("users can log in and sign up")
    assert "payments" in _detect_feature_tiers("checkout with stripe")
    assert "email" in _detect_feature_tiers("send a welcome email via smtp")
    tiers = _detect_feature_tiers("login, stripe checkout, and email verification")
    assert tiers == ["auth", "payments", "email"]


def test_feature_tier_files_pluggable():
    from skyn3t.agents.stack_templates import _feature_tier_files

    assert _feature_tier_files([]) == []
    assert _feature_tier_files(["bogus"]) == []
    auth = _feature_tier_files(["auth"])
    names = [p for p, _ in auth]
    assert any("auth" in n for n in names)


def test_plan_for_stack_emits_auth_tier_with_backend():
    from skyn3t.agents.stack_templates import plan_for_stack

    brief = (
        "A react dashboard that talks to a real backend api with api keys; "
        "users can sign up and log in, checkout with stripe, and get a "
        "welcome email via smtp."
    )
    plan = plan_for_stack("react_vite", brief)
    assert plan is not None
    paths = [p for p, _ in plan]
    assert any("server/auth/" in p for p in paths)
    assert any("server/payments/" in p for p in paths)
    assert any("server/email/" in p for p in paths)


def test_plan_for_stack_no_feature_tier_when_unsignaled():
    from skyn3t.agents.stack_templates import plan_for_stack

    # A plain static site brief with no backend / auth / payment signals.
    plan = plan_for_stack("static_site", "a simple personal landing page")
    assert plan is not None
    paths = [p for p, _ in plan]
    assert not any("server/auth/" in p for p in paths)
    assert not any("server/payments/" in p for p in paths)


# ── designer flag-off behavior ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_designer_asset_hook_inert_when_flag_off(monkeypatch):
    """DesignerAgent._maybe_generate_assets returns ([], None) with flag off."""
    monkeypatch.delenv("SKYN3T_ASSET_GEN", raising=False)
    from skyn3t.agents.designer import DesignerAgent

    agent = DesignerAgent()
    assets, tokens = await agent._maybe_generate_assets(
        brief="b", mood="minimal", artifact_dir=Path("/tmp/x"), data={}
    )
    assert assets == []
    assert tokens is None
