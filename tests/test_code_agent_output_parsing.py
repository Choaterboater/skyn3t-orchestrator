from skyn3t.agents.code_agent import _extract_marked_files


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
