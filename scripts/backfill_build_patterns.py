#!/usr/bin/env python3
"""Backfill / heal a build_patterns.json scoreboard.

The live scoreboard split the same logical stack across two keys —
``vite_react`` and ``react_vite`` — because different record sites named
the stack differently (success path used the coarse build-verifier kind,
failure paths used the brief-based detector which returned ``None`` →
``unknown``). Phase 2 unifies the record sites on
:func:`skyn3t.agents.stack_detector.detect_stack_from_scaffold`, but the
already-persisted JSON still carries the historical split. This script
heals an EXISTING file in place (atomically) by:

  1. Merging the legacy ``vite_react`` bucket into the canonical
     ``react_vite`` bucket, combining shapes by shape-hash. Counts
     (success / failure / skipped), per-failure ``tags`` and per-backend
     ``by_backend`` tallies are ADDED so total evidence is preserved
     (never ``max``-clamped). ``last_seen_at`` keeps the most recent.
  2. Reporting the ``unknown`` bucket. We CANNOT re-detect those rows
     without the original scaffold directories (only the shape file-list
     survives, not a manifest tree), so they are left untouched for
     manual review and merely summarised.

Safety: this NEVER touches ``data/`` implicitly. ``--path`` is required
and must point at the file to heal; pass ``--dry-run`` (default) to print
the plan without writing, and ``--write`` to actually persist.

Usage::

    # inspect only (no write):
    python3 scripts/backfill_build_patterns.py --path /tmp/copy_of_build_patterns.json

    # heal in place (atomic tmp+replace, mirrors BuildPatternScoreboard._flush_locked):
    python3 scripts/backfill_build_patterns.py --path /tmp/copy_of_build_patterns.json --write

Run it against a COPY first. It is intentionally NOT wired into any
scheduler or the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, Tuple

# The canonical alias map + stats schema live in the owning modules.
# Import them so this script stays in lock-step with the live merge rules
# instead of duplicating field lists.
try:
    from skyn3t.agents.stack_detector import _CANONICAL_STACK_NAMES
    from skyn3t.intelligence.build_patterns import BuildPatternStats
except ModuleNotFoundError:  # pragma: no cover - allow running from repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from skyn3t.agents.stack_detector import _CANONICAL_STACK_NAMES
    from skyn3t.intelligence.build_patterns import BuildPatternStats


# ---------------------------------------------------------------------------
# Stats merge helpers
# ---------------------------------------------------------------------------

def _merge_stats(into: BuildPatternStats, other: BuildPatternStats) -> None:
    """Fold ``other`` into ``into`` in place — ADD all evidence, don't clamp.

    Both must already share the same shape-hash (same normalized shape).
    The ``into.shape`` is kept; counts/tags/by_backend accumulate.
    """
    into.success += other.success
    into.failure += other.failure
    into.skipped += other.skipped

    for tag, count in other.tags.items():
        into.tags[tag] = into.tags.get(tag, 0) + int(count)

    for backend, slot in other.by_backend.items():
        dest = into.by_backend.setdefault(
            backend, {"success": 0, "failure": 0, "skipped": 0}
        )
        for key in ("success", "failure", "skipped"):
            dest[key] = int(dest.get(key, 0)) + int(slot.get(key, 0))

    # Keep the freshest sighting so recency-based queries stay meaningful.
    into.last_seen_at = max(into.last_seen_at, other.last_seen_at)


def _bucket_totals(bucket: Dict[str, dict]) -> Tuple[int, int, int, int]:
    """(shapes, success, failure, skipped) for a raw {hash: row} bucket."""
    success = sum(int(row.get("success", 0)) for row in bucket.values())
    failure = sum(int(row.get("failure", 0)) for row in bucket.values())
    skipped = sum(int(row.get("skipped", 0)) for row in bucket.values())
    return len(bucket), success, failure, skipped


# ---------------------------------------------------------------------------
# Core backfill
# ---------------------------------------------------------------------------

def backfill(raw: Dict[str, Dict[str, dict]]) -> Tuple[Dict[str, Dict[str, dict]], Dict]:
    """Pure transform: merge alias buckets into their canonical key.

    Takes the parsed JSON dict (``{stack: {shape_hash: row}}``) and
    returns ``(new_raw, report)`` where ``new_raw`` is the healed dict
    and ``report`` describes what changed. Does NOT do any I/O — callers
    decide whether to persist, which keeps this trivially testable.
    """
    report: Dict = {
        "merges": [],          # one entry per alias→canonical fold
        "unknown": None,       # summary of the unreviewable bucket
        "before": {},
        "after": {},
    }

    # Snapshot before-totals per stack for the summary.
    for stack, bucket in raw.items():
        if isinstance(bucket, dict):
            report["before"][stack] = _bucket_totals(bucket)

    # Rebuild the dict so we control ordering and avoid mutating while
    # iterating. Canonical buckets are materialised as BuildPatternStats
    # so the merge math reuses the live schema.
    typed: Dict[str, Dict[str, BuildPatternStats]] = {}
    for stack, bucket in raw.items():
        if not isinstance(bucket, dict):
            continue
        typed_bucket: Dict[str, BuildPatternStats] = {}
        for shape_hash, row in bucket.items():
            if isinstance(row, dict):
                typed_bucket[str(shape_hash)] = BuildPatternStats.from_dict(row)
        typed[str(stack)] = typed_bucket

    # Apply each alias → canonical fold.
    for alias, canonical in _CANONICAL_STACK_NAMES.items():
        if alias not in typed:
            continue
        alias_bucket = typed.pop(alias)
        canonical_bucket = typed.setdefault(canonical, {})

        merged_shapes = 0
        new_shapes = 0
        for shape_hash, stats in alias_bucket.items():
            existing = canonical_bucket.get(shape_hash)
            if existing is None:
                canonical_bucket[shape_hash] = stats
                new_shapes += 1
            else:
                _merge_stats(existing, stats)
                merged_shapes += 1

        report["merges"].append({
            "from": alias,
            "to": canonical,
            "shapes_from_alias": len(alias_bucket),
            "shapes_merged_into_existing": merged_shapes,
            "shapes_added_new": new_shapes,
        })

    # Report (but never touch) the unknown bucket — unrecoverable without
    # the original scaffolds.
    unknown_bucket = raw.get("unknown")
    if isinstance(unknown_bucket, dict) and unknown_bucket:
        shapes, success, failure, skipped = _bucket_totals(unknown_bucket)
        report["unknown"] = {
            "shapes": shapes,
            "success": success,
            "failure": failure,
            "skipped": skipped,
            "note": (
                "Cannot re-detect stack without the original scaffold trees "
                "(only the shape file-list survives, not a manifest). Left "
                "untouched for manual review."
            ),
        }

    # Serialize back to plain dicts.
    new_raw: Dict[str, Dict[str, dict]] = {
        stack: {h: s.to_dict() for h, s in bucket.items()}
        for stack, bucket in typed.items()
    }

    for stack, bucket in new_raw.items():
        report["after"][stack] = _bucket_totals(bucket)

    return new_raw, report


# ---------------------------------------------------------------------------
# Atomic persistence — mirrors BuildPatternScoreboard._flush_locked
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_report(report: Dict, *, wrote: bool) -> None:
    print("=" * 64)
    print("build_patterns backfill report")
    print("=" * 64)

    print("\nBEFORE (stack: shapes / success / failure / skipped):")
    for stack in sorted(report["before"]):
        shapes, succ, fail, skip = report["before"][stack]
        print(f"  {stack:<14} {shapes:>4} shapes  {succ:>5}✓  {fail:>5}✗  {skip:>5}∅")

    if report["merges"]:
        print("\nMERGES:")
        for m in report["merges"]:
            print(
                f"  {m['from']} -> {m['to']}: "
                f"{m['shapes_from_alias']} alias shape(s), "
                f"{m['shapes_merged_into_existing']} folded into existing, "
                f"{m['shapes_added_new']} added new"
            )
    else:
        print("\nMERGES: none (no alias buckets present)")

    if report["unknown"]:
        u = report["unknown"]
        print("\nUNKNOWN BUCKET (left for manual review):")
        print(
            f"  {u['shapes']} shapes  {u['success']}✓  {u['failure']}✗  "
            f"{u['skipped']}∅"
        )
        print(f"  {u['note']}")
    else:
        print("\nUNKNOWN BUCKET: empty")

    print("\nAFTER (stack: shapes / success / failure / skipped):")
    for stack in sorted(report["after"]):
        shapes, succ, fail, skip = report["after"][stack]
        print(f"  {stack:<14} {shapes:>4} shapes  {succ:>5}✓  {fail:>5}✗  {skip:>5}∅")

    print("\n" + ("WROTE changes to disk." if wrote else "DRY RUN — no changes written. Pass --write to persist."))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Heal a build_patterns.json: merge vite_react -> react_vite and "
            "report the unknown bucket. Requires an explicit --path; never "
            "touches data/ implicitly."
        ),
    )
    parser.add_argument(
        "--path",
        required=True,
        type=Path,
        help="Path to the build_patterns.json to heal (operate on a COPY).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist the healed file in place (atomic). Default is dry-run.",
    )
    args = parser.parse_args(argv)

    path: Path = args.path
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface parse errors plainly
        print(f"error: could not parse JSON at {path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(raw, dict):
        print(f"error: expected a JSON object at top level of {path}", file=sys.stderr)
        return 2

    new_raw, report = backfill(raw)

    if args.write:
        _atomic_write(path, new_raw)

    _print_report(report, wrote=bool(args.write))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
