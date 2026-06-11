"""Regression tests for the Python code chunker in DocumentProcessor.

Bug (pre-fix) in DocumentProcessor._chunk_python_code:
  - At a def/class boundary the accumulated chunk was only flushed when it
    exceeded chunk_size (default 1000 words), so nearly every real-world
    function/class chunk was silently dropped -> GitHub .py learning lost.
  - When the first non-blank line was a def/class, the `else` branch tried to
    append an unbound `chunk_text`, raising UnboundLocalError and crashing
    ingestion.

These tests fail before the fix and pass after it.
"""

from skyn3t.rag.document_processor import DocumentProcessor


def _make_processor():
    proc = DocumentProcessor()
    # Keep the default-style large chunk_size so the "small chunk" path is
    # exercised exactly as it is in production (default chunk_size=1000).
    proc.chunk_size = 1000
    proc.chunk_overlap = 200
    return proc


def test_leading_def_does_not_crash_and_is_chunked():
    """A file whose first non-blank line is a def must not raise and must
    produce at least one chunk (regression for UnboundLocalError)."""
    proc = _make_processor()
    code = "def foo():\n    return 1\n"
    chunks = proc._chunk_python_code(code)
    assert len(chunks) >= 1
    assert any("def foo" in c for c in chunks)


def test_leading_class_does_not_crash():
    proc = _make_processor()
    code = "class Foo:\n    pass\n"
    chunks = proc._chunk_python_code(code)
    assert len(chunks) >= 1
    assert any("class Foo" in c for c in chunks)


def test_small_functions_are_not_dropped():
    """Multiple small functions (each well under chunk_size) must each survive
    rather than being silently dropped at the def boundary."""
    proc = _make_processor()
    code = (
        "import os\n"
        "\n"
        "def alpha():\n"
        "    return 'a'\n"
        "\n"
        "def beta():\n"
        "    return 'b'\n"
        "\n"
        "async def gamma():\n"
        "    return 'g'\n"
    )
    chunks = proc._chunk_python_code(code)
    joined = "\n".join(chunks)
    assert any("def alpha" in c for c in chunks)
    assert any("def beta" in c for c in chunks)
    assert any("async def gamma" in c for c in chunks)
    # Leading module content before the first def must be preserved too.
    assert "import os" in joined


def test_process_code_python_roundtrip_for_small_file():
    """End-to-end: process_code on a small python file yields chunks with the
    expected metadata, instead of an empty list or a crash."""
    proc = _make_processor()
    code = "def only():\n    return 42\n"
    docs = proc.process_code(code, language="python")
    assert len(docs) >= 1
    assert docs[0]["metadata"]["language"] == "python"
    assert docs[0]["metadata"]["total_chunks"] == len(docs)
    assert "def only" in docs[0]["content"]
