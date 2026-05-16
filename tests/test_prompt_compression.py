from skyn3t.prompt_compression import compress_prompt_context


def test_short_body_returned_unchanged():
    assert compress_prompt_context("hello", max_chars=100) == "hello"


def test_long_body_truncated_with_marker():
    body = "x" * 500
    out = compress_prompt_context(body, max_chars=100)
    assert len(out) <= 100
    assert out.endswith("…[truncated]")


def test_blank_line_runs_collapsed_before_truncation():
    body = "a\n\n\n\n\nb" + ("x" * 200)
    out = compress_prompt_context(body, max_chars=50)
    assert "\n\n\n" not in out
    assert out.endswith("…[truncated]")


def test_empty_and_non_positive_max_are_safe():
    assert compress_prompt_context("", max_chars=10) == ""
    assert compress_prompt_context("abc", max_chars=0) == "abc"
