from skyn3t.agents.code_agent import _extract_marked_files, _strip_cli_prelude, _syntax_ok


def test_extract_marked_files_parses_js_and_py_markers() -> None:
    raw = """
// === server/index.js ===
import express from "express";

# === src/main.py ===
print("ok")
""".strip()

    parsed = _extract_marked_files(raw)

    assert parsed["server/index.js"] == 'import express from "express";'
    assert parsed["src/main.py"] == 'print("ok")'


def test_strip_cli_prelude_removes_css_tool_trace() -> None:
    raw = """
I’m checking the surrounding components so the stylesheet rewrite matches the existing class names.

● Search (glob)
  │ "src/**/*"
  └ No matches found

:root {
  color-scheme: dark;
}
""".strip()

    stripped = _strip_cli_prelude(raw, "src/styles.css")

    assert stripped.startswith(":root {")
    assert "I’m checking" not in stripped


def test_syntax_ok_rejects_css_transcript_prefix() -> None:
    contaminated = """
I’m checking the surrounding components so the stylesheet rewrite matches the existing class names.

✗ Read styles.css
  │ src/styles.css

:root {
  color-scheme: dark;
}
""".strip()

    assert _syntax_ok(contaminated, "src/styles.css") is False
