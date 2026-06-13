from skyn3t.adapters import context_compressor as cc


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SKYN3T_COMPRESS_CONTEXT", raising=False)
    p, s = cc.compress("a\n\n\n\nb", "sys")
    assert p == "a\n\n\n\nb" and s == "sys"


def test_collapses_blank_runs_and_dedupes_long_lines():
    text = (
        "this line is definitely long enough\n\n\n\n"
        "this line is definitely long enough\n"
        "a distinct keeper line that is long\n"
    )
    out = cc.compress_text(text)
    assert "\n\n\n" not in out
    assert out.count("this line is definitely long enough") == 1
    assert "a distinct keeper line that is long" in out


def test_truncates_huge_payload():
    out = cc.compress_text("x" * 60_000, max_chars=1_000)
    assert len(out) < 2_000
    assert "compressed" in out


def test_compress_enabled_path(monkeypatch):
    monkeypatch.setenv("SKYN3T_COMPRESS_CONTEXT", "1")
    p, _s = cc.compress("hello   world\n\n\n\nx", None)
    assert "\n\n\n" not in p
