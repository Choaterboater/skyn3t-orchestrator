"""Tests for ``skyn3t.intelligence.error_signatures``.

Signatures are the join key between the experience index and the
RAG-recall layer. Stability across runs is what makes the ranker
useful — these tests pin the canonical format so a passing-style
change later doesn't silently re-bucket every fix.
"""

from __future__ import annotations

import pytest

from skyn3t.intelligence.error_signatures import (
    signature_for_build_issue,
    signature_for_build_issues,
    signature_for_finding,
    signature_for_findings,
)


def test_single_finding_with_category_only():
    sig = signature_for_finding({"category": "palette_schism"})
    assert sig == "contract:palette_schism"


def test_single_finding_with_file_appends_basename():
    sig = signature_for_finding({"category": "placeholder_leak", "file": "src/App.jsx"})
    assert sig == "contract:placeholder_leak:App.jsx"


def test_single_finding_strips_path_components():
    sig = signature_for_finding(
        {"category": "missing_mount", "file": "scaffold/src/index.html"},
    )
    assert sig == "contract:missing_mount:index.html"


def test_single_finding_handles_windows_paths():
    sig = signature_for_finding(
        {"category": "x", "file": r"scaffold\src\App.jsx"},
    )
    assert sig == "contract:x:App.jsx"


def test_single_finding_lowercases_and_underscores_category():
    sig = signature_for_finding({"category": "Palette Schism"})
    assert sig == "contract:palette_schism"


def test_single_finding_falls_back_through_aliases():
    """Category may arrive under ``rule``, ``rule_id``, or ``kind``."""
    assert signature_for_finding({"rule": "Missing Mount"}) == "contract:missing_mount"
    assert signature_for_finding({"rule_id": "R042"}) == "contract:r042"
    assert signature_for_finding({"kind": "drift"}) == "contract:drift"


def test_source_appears_in_signature():
    sig = signature_for_finding({"category": "x"}, source="consistency")
    assert sig == "consistency:x"


def test_finding_without_category_returns_none():
    """Better to record nothing than to bucket every nameless failure
    under the same ``unknown`` signature."""
    assert signature_for_finding({"file": "App.jsx"}) is None
    assert signature_for_finding({}) is None
    assert signature_for_finding({"category": ""}) is None
    assert signature_for_finding(None) is None  # type: ignore[arg-type]


def test_findings_prefers_blockers():
    findings = [
        {"category": "warn_thing", "severity": "warning"},
        {"category": "blocker_thing", "severity": "blocker"},
        {"category": "another_warn", "severity": "warning"},
    ]
    assert signature_for_findings(findings) == "contract:blocker_thing"


def test_findings_falls_back_to_first_usable_when_no_blockers():
    findings = [
        {"category": "", "severity": "warning"},  # not usable
        {"category": "noticed", "severity": "warning"},
        {"category": "other", "severity": "warning"},
    ]
    assert signature_for_findings(findings) == "contract:noticed"


def test_findings_empty_list_returns_none():
    assert signature_for_findings([]) is None
    assert signature_for_findings(None) is None  # type: ignore[arg-type]


def test_findings_all_unnamed_returns_none():
    findings = [{"severity": "blocker"}, {"file": "x.js"}]
    assert signature_for_findings(findings) is None


# ---------------------------------------------------------------------
# Build-error signatures (parsed from build logs, not from findings)
# ---------------------------------------------------------------------


class _BuildIssue:
    """FileIssue-shaped duck for tests — no need to import the real
    dataclass since the helper duck-types via getattr."""

    def __init__(self, path, error_message):
        self.path = path
        self.error_message = error_message


def test_build_signature_missing_module():
    issue = _BuildIssue(path="src/App.jsx", error_message="Missing module: react-helmet")
    assert signature_for_build_issue(issue) == "build:missing_module:App.jsx"


def test_build_signature_syntax_error():
    issue = _BuildIssue(
        path="src/main.jsx",
        error_message="Line 12, col 3: Unexpected token '}'",
    )
    assert signature_for_build_issue(issue) == "build:syntax_error:main.jsx"


def test_build_signature_missing_export():
    issue = _BuildIssue(
        path="src/components/Foo.jsx",
        error_message="Missing export (line 4, col 8)",
    )
    assert signature_for_build_issue(issue) == "build:missing_export:Foo.jsx"


def test_build_signature_type_error():
    issue = _BuildIssue(
        path="src/App.tsx",
        error_message="Cannot find name 'window'",
    )
    assert signature_for_build_issue(issue) == "build:type_error:App.tsx"


def test_build_signature_unknown_message_returns_none():
    """Unrecognized error messages must NOT bucket under ``unknown``."""
    issue = _BuildIssue(path="src/App.jsx", error_message="weird thing happened")
    assert signature_for_build_issue(issue) is None


def test_build_signature_dict_input_supported():
    """Helper duck-types on getattr OR dict — both must work."""
    assert (
        signature_for_build_issue(
            {"path": "src/App.jsx", "error_message": "Missing module: x"},
        )
        == "build:missing_module:App.jsx"
    )


def test_build_signature_no_path_returns_category_only():
    issue = _BuildIssue(path="", error_message="SyntaxError on something")
    assert signature_for_build_issue(issue) == "build:syntax_error"


def test_build_signatures_picks_first_classifiable():
    """When the first issue is unclassifiable, fall through to the
    next — build logs usually have one root cause and several
    downstream notes."""
    unclassifiable = _BuildIssue(path="a.js", error_message="weird noise")
    classifiable = _BuildIssue(path="b.js", error_message="Missing module: foo")
    assert (
        signature_for_build_issues([unclassifiable, classifiable])
        == "build:missing_module:b.js"
    )


def test_build_signatures_returns_none_for_all_unclassifiable():
    issues = [
        _BuildIssue(path="a.js", error_message="weird"),
        _BuildIssue(path="b.js", error_message="more weird"),
    ]
    assert signature_for_build_issues(issues) is None


def test_build_signatures_empty_input_returns_none():
    assert signature_for_build_issues([]) is None
