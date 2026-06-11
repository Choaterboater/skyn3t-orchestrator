"""Phase 2 — Owner E: SkillLibrary accumulation.

Proves the two Owner-E changes in
``skyn3t.intelligence.skill_library``:

1. ITEM 3 UPSERT — ``_write_skill`` merges success/failure counts
   ADDITIVELY (existing + incoming delta) instead of clamping with
   ``max()``. Recovery evidence must accumulate across upserts.

2. ITEM 3 CURATE SCHEDULE — ``curate_if_due(interval_seconds, **kw)``
   runs ``curate()`` only when the interval has elapsed, persisting a
   last-curate timestamp in a sidecar JSON, and returns the curate result
   or ``None`` when skipped.

All tests use ``SkillLibrary(root=tmp_path)`` — never touch data/skills/.
"""

from __future__ import annotations

import json
import time

from skyn3t.intelligence.skill_library import Skill, SkillLibrary

# ─── ITEM 3 UPSERT: additive count merge ────────────────────────────────


def test_upsert_accumulates_success_counts_additively(tmp_path):
    """Two upserts of the same slug ADD their counts.

    With the old ``max()`` clamp this asserted 3 (max(3, 2)); the additive
    fix makes it 5 (3 + 2). This is the failing-before/passing-after edge.
    """
    lib = SkillLibrary(root=tmp_path)
    lib.upsert(Skill(name="fastapi-health", success_count=3, failure_count=1))
    lib.upsert(Skill(name="fastapi-health", success_count=2, failure_count=4))

    reloaded = {s.slug: s for s in lib.all()}["fastapi-health"]
    assert reloaded.success_count == 5
    assert reloaded.failure_count == 5


def test_upsert_accumulates_failures_recovery_not_clamped(tmp_path):
    """Later failure evidence is not discarded once an equal count exists."""
    lib = SkillLibrary(root=tmp_path)
    lib.upsert(Skill(name="flaky-skill", success_count=0, failure_count=2))
    # Old max() would keep failure_count at 2; additive keeps growing.
    lib.upsert(Skill(name="flaky-skill", success_count=0, failure_count=2))
    lib.upsert(Skill(name="flaky-skill", success_count=0, failure_count=2))

    reloaded = {s.slug: s for s in lib.all()}["flaky-skill"]
    assert reloaded.failure_count == 6


def test_upsert_preserves_created_at_and_unions_tags(tmp_path):
    """Additive change must not regress the existing created_at / tag-merge
    behavior."""
    lib = SkillLibrary(root=tmp_path)
    first = Skill(name="merge-meta", tags=["a"], success_count=1, created_at=111.0)
    lib.upsert(first)
    lib.upsert(Skill(name="merge-meta", tags=["b"], success_count=1, created_at=999.0))

    reloaded = {s.slug: s for s in lib.all()}["merge-meta"]
    assert reloaded.created_at == 111.0
    assert reloaded.tags == ["a", "b"]
    assert reloaded.success_count == 2


def test_fresh_upsert_no_existing_keeps_seed_counts(tmp_path):
    """First write of a slug just persists the seed counts unchanged."""
    lib = SkillLibrary(root=tmp_path)
    lib.upsert(Skill(name="brand-new", success_count=7, failure_count=2))

    reloaded = {s.slug: s for s in lib.all()}["brand-new"]
    assert reloaded.success_count == 7
    assert reloaded.failure_count == 2


# ─── ITEM 3 CURATE SCHEDULE: curate_if_due cadence ──────────────────────


def test_curate_if_due_runs_first_time_and_persists_ts(tmp_path):
    lib = SkillLibrary(root=tmp_path)
    # A hurtful skill with enough samples so curate would archive it.
    lib.upsert(Skill(name="bad-skill", success_count=0, failure_count=5))

    result = lib.curate_if_due(interval_seconds=86400)
    assert result is not None
    assert "archived" in result and "kept" in result
    assert "bad-skill" in result["archived"]

    # Sidecar timestamp persisted next to root, not inside it.
    state_path = tmp_path.parent / f"{tmp_path.name}.curate.json"
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["last_curate_ts"] > 0


def test_curate_if_due_skips_when_not_yet_due(tmp_path):
    lib = SkillLibrary(root=tmp_path)
    first = lib.curate_if_due(interval_seconds=86400)
    assert first is not None
    # Immediate second call within the interval is skipped -> None.
    second = lib.curate_if_due(interval_seconds=86400)
    assert second is None


def test_curate_if_due_runs_again_after_interval_elapses(tmp_path):
    lib = SkillLibrary(root=tmp_path)
    assert lib.curate_if_due(interval_seconds=86400) is not None
    # Backdate the persisted timestamp well beyond the interval.
    state_path = lib._curate_state_path
    state_path.write_text(
        json.dumps({"last_curate_ts": time.time() - 100000}), encoding="utf-8"
    )
    assert lib.curate_if_due(interval_seconds=86400) is not None


def test_curate_if_due_passes_kwargs_through(tmp_path):
    """protect_tags forwarded to curate() spares a pinned-equivalent skill."""
    lib = SkillLibrary(root=tmp_path)
    lib.upsert(Skill(name="hurtful-but-protected", tags=["keepme"],
                     success_count=0, failure_count=9))

    result = lib.curate_if_due(interval_seconds=0, protect_tags=["keepme"])
    assert result is not None
    assert "hurtful-but-protected" in result["kept"]
    assert "hurtful-but-protected" not in result["archived"]


def test_curate_state_sidecar_not_scanned_as_skill(tmp_path):
    """The curate sidecar lives outside root so it never appears in all()."""
    lib = SkillLibrary(root=tmp_path)
    lib.upsert(Skill(name="real-skill", success_count=1))
    lib.curate_if_due(interval_seconds=86400)

    names = {s.name for s in lib.all()}
    assert names == {"real-skill"}
