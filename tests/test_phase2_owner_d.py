"""Phase 2 — Owner D: canonical stack detection + build_patterns backfill.

Covers:
  * stack_detector.detect_stack_from_scaffold — canonical flattening of
    detect() incl. vite_react -> react_vite normalization and the
    unknown/crash fallback.
  * scripts/backfill_build_patterns.backfill — merges the vite_react
    bucket into react_vite, ADDING (not max-clamping) success/failure/
    skipped/tags/by_backend, reports the unknown bucket, and persists
    atomically only when --write is given.

These fail before Owner D's changes (no detect_stack_from_scaffold, no
_CANONICAL_STACK_NAMES, no backfill script) and pass after. They only
touch tmp_path — never data/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.backfill_build_patterns as bf
from skyn3t.agents.stack_detector import (
    _CANONICAL_STACK_NAMES,
    detect_stack_from_scaffold,
)
from skyn3t.intelligence.build_patterns import BuildPatternScoreboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_vite_pkg(root: Path) -> None:
    pkg = {
        "name": "demo",
        "version": "1.0.0",
        "dependencies": {"react": "^18", "react-dom": "^18", "vite": "^5"},
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")


# ---------------------------------------------------------------------------
# detect_stack_from_scaffold
# ---------------------------------------------------------------------------

class TestDetectStackFromScaffold:
    def test_vite_scaffold_returns_canonical_react_vite(self, tmp_path: Path) -> None:
        _write_vite_pkg(tmp_path)
        assert detect_stack_from_scaffold(tmp_path) == "react_vite"

    def test_nested_scaffold_dir(self, tmp_path: Path) -> None:
        # detect() peeks under <root>/scaffold first.
        _write_vite_pkg(tmp_path / "scaffold")
        assert detect_stack_from_scaffold(tmp_path) == "react_vite"

    def test_empty_dir_returns_unknown(self, tmp_path: Path) -> None:
        assert detect_stack_from_scaffold(tmp_path) == "unknown"

    def test_missing_dir_returns_unknown(self, tmp_path: Path) -> None:
        assert detect_stack_from_scaffold(tmp_path / "nope") == "unknown"

    def test_canonical_map_aliases_vite_react(self) -> None:
        # The normalization map is what unifies the two live buckets.
        assert _CANONICAL_STACK_NAMES.get("vite_react") == "react_vite"


# ---------------------------------------------------------------------------
# backfill merge math
# ---------------------------------------------------------------------------

class TestBackfillMerge:
    def _seed_split_store(self, path: Path) -> None:
        """Build a store with a vite_react/react_vite split sharing one shape
        plus a unique alias shape, a tags/by_backend payload, and an
        unknown bucket — mirroring the live data shape."""
        shape = ["index.html", "package.json", "src/App.tsx"]

        # Canonical react_vite already holds this shape with 1 success and
        # an existing failure tag.
        sb = BuildPatternScoreboard(store_path=path, flush_every=1)
        sb.record("react_vite", shape, "yes")
        sb.record_tag("react_vite", shape, "missing_mount")
        sb.record_backend("react_vite", shape, "kimi_cli", "yes")

        # Legacy vite_react bucket: same shape (+1 success, +1 failure,
        # +1 tag, +backend failure) AND a unique shape only in the alias.
        sb.record("vite_react", shape, "yes")
        sb.record("vite_react", shape, "no")
        sb.record_tag("vite_react", shape, "missing_mount")
        sb.record_backend("vite_react", shape, "kimi_cli", "no")
        sb.record("vite_react", ["other.txt"], "no")

        # An unknown bucket the backfill must report but not touch.
        sb.record("unknown", ["mystery.py"], "no")
        sb.flush()

    def test_merge_adds_counts_not_max(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        self._seed_split_store(path)
        raw = json.loads(path.read_text())

        new_raw, report = bf.backfill(raw)

        # vite_react bucket is gone, react_vite remains.
        assert "vite_react" not in new_raw
        assert "react_vite" in new_raw

        # Reload through the scoreboard to aggregate by stack cleanly.
        out_path = tmp_path / "out.json"
        out_path.write_text(json.dumps(new_raw))
        sb2 = BuildPatternScoreboard(store_path=out_path)
        stats = sb2.all_stats_for("react_vite")

        # Find the shared shape (3 files) vs the alias-only shape.
        shared = [s for s in stats if len(s.shape) == 3]
        assert len(shared) == 1
        shared = shared[0]

        # ADD semantics: 1 (canonical) + 1 (alias) success = 2; failure 0+1=1.
        assert shared.success == 2, "successes must be ADDED, not maxed"
        assert shared.failure == 1
        # Tag accumulates: 1 + 1 = 2.
        assert shared.tags.get("missing_mount") == 2
        # by_backend accumulates per slot.
        assert shared.by_backend["kimi_cli"]["success"] == 1
        assert shared.by_backend["kimi_cli"]["failure"] == 1

        # The alias-only shape was carried over as a new shape.
        alias_only = [s for s in stats if s.shape == ["other.txt"]]
        assert len(alias_only) == 1
        assert alias_only[0].failure == 1

        # Merge report describes the fold.
        assert report["merges"], "expected a vite_react->react_vite merge entry"
        merge = report["merges"][0]
        assert merge["from"] == "vite_react"
        assert merge["to"] == "react_vite"
        assert merge["shapes_merged_into_existing"] == 1
        assert merge["shapes_added_new"] == 1

    def test_unknown_bucket_reported_and_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        self._seed_split_store(path)
        raw = json.loads(path.read_text())

        new_raw, report = bf.backfill(raw)

        # Unknown is preserved verbatim.
        assert new_raw.get("unknown") == raw.get("unknown")
        assert report["unknown"] is not None
        assert report["unknown"]["failure"] == 1
        assert "manual review" in report["unknown"]["note"]

    def test_total_evidence_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        self._seed_split_store(path)
        raw = json.loads(path.read_text())

        def _grand_total(d: dict) -> tuple[int, int]:
            succ = sum(int(r.get("success", 0)) for b in d.values() for r in b.values())
            fail = sum(int(r.get("failure", 0)) for b in d.values() for r in b.values())
            return succ, fail

        before = _grand_total(raw)
        new_raw, _ = bf.backfill(raw)
        after = _grand_total(new_raw)
        assert before == after, "merge must conserve total success/failure evidence"


# ---------------------------------------------------------------------------
# CLI / persistence behaviour
# ---------------------------------------------------------------------------

class TestBackfillCLI:
    def _seed(self, path: Path) -> None:
        sb = BuildPatternScoreboard(store_path=path, flush_every=1)
        sb.record("react_vite", ["index.html", "package.json"], "yes")
        sb.record("vite_react", ["index.html", "package.json"], "yes")
        sb.flush()

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        self._seed(path)
        original = path.read_text()

        rc = bf.main(["--path", str(path)])  # no --write
        assert rc == 0
        assert path.read_text() == original, "dry-run must not modify the file"

    def test_write_persists_merge_atomically(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        self._seed(path)

        rc = bf.main(["--path", str(path), "--write"])
        assert rc == 0

        healed = json.loads(path.read_text())
        assert "vite_react" not in healed
        assert "react_vite" in healed
        # No leftover .tmp file from the atomic replace.
        assert not (tmp_path / "p.json.tmp").exists()

        # Merged shape carries both successes.
        sb = BuildPatternScoreboard(store_path=path)
        stats = sb.all_stats_for("react_vite")
        assert len(stats) == 1
        assert stats[0].success == 2

    def test_missing_path_errors(self, tmp_path: Path) -> None:
        rc = bf.main(["--path", str(tmp_path / "absent.json")])
        assert rc == 2

    def test_path_is_required(self) -> None:
        # argparse exits with code 2 when a required arg is missing.
        with pytest.raises(SystemExit) as exc:
            bf.main([])
        assert exc.value.code == 2
