"""Tests for EnvVarScanner — verifies all five idioms + classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.agents.env_scanner import EnvVarRef, ScanResult, scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Smoke / edge cases
# ---------------------------------------------------------------------------

class TestScanSmoke:
    def test_empty_dir_returns_empty_result(self, tmp_path: Path) -> None:
        result = scan(tmp_path)
        assert isinstance(result, ScanResult)
        assert result.vars == {}
        assert result.scanned_files == 0

    def test_nonexistent_dir_returns_empty_result(self, tmp_path: Path) -> None:
        result = scan(tmp_path / "does-not-exist")
        assert result.vars == {}

    def test_skips_node_modules_and_dist(self, tmp_path: Path) -> None:
        _write(tmp_path, "node_modules/dep/index.js", "process.env.SHOULD_SKIP")
        _write(tmp_path, "dist/bundle.js", "process.env.ALSO_SKIP")
        _write(tmp_path, ".git/hooks/pre-commit", "process.env.GIT_SKIP")
        _write(tmp_path, "src/app.js", "process.env.REAL_VAR")
        result = scan(tmp_path)
        assert "REAL_VAR" in result.vars
        assert "SHOULD_SKIP" not in result.vars
        assert "ALSO_SKIP" not in result.vars
        assert "GIT_SKIP" not in result.vars

    def test_binary_file_is_skipped_not_crashed(self, tmp_path: Path) -> None:
        # Write valid UTF-16 bytes — UTF-8 read should fail and skip.
        bad = tmp_path / "src" / "bad.js"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"\xff\xfe\x00\x00garbage")
        # And one good file alongside it.
        _write(tmp_path, "src/good.js", "process.env.GOOD")
        result = scan(tmp_path)
        assert "GOOD" in result.vars
        assert result.skipped_files >= 1


# ---------------------------------------------------------------------------
# Node idiom (process.env)
# ---------------------------------------------------------------------------

class TestNodeIdiom:
    def test_attribute_access(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/api.js", "const k = process.env.API_KEY;")
        result = scan(tmp_path)
        assert "API_KEY" in result.vars
        assert result.vars["API_KEY"].idiom == "node"

    def test_bracket_access_double_quote(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/api.js", 'const k = process.env["API_KEY"];')
        result = scan(tmp_path)
        assert "API_KEY" in result.vars

    def test_bracket_access_single_quote(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/api.js", "const k = process.env['DB_HOST'];")
        result = scan(tmp_path)
        assert "DB_HOST" in result.vars

    def test_dedupes_across_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/a.js", "process.env.SHARED")
        _write(tmp_path, "src/b.js", "process.env.SHARED")
        result = scan(tmp_path)
        assert list(result.vars.keys()) == ["SHARED"]
        assert sorted(result.vars["SHARED"].used_in) == ["src/a.js", "src/b.js"]


# ---------------------------------------------------------------------------
# Vite idiom (import.meta.env)
# ---------------------------------------------------------------------------

class TestViteIdiom:
    def test_attribute_access(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/main.jsx", "const u = import.meta.env.VITE_API_URL;")
        result = scan(tmp_path)
        assert "VITE_API_URL" in result.vars
        assert result.vars["VITE_API_URL"].idiom == "vite"

    def test_bracket_access(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/main.tsx", 'const u = import.meta.env["MODE"];')
        result = scan(tmp_path)
        assert "MODE" in result.vars


# ---------------------------------------------------------------------------
# Python idioms (os.getenv / os.environ / pydantic)
# ---------------------------------------------------------------------------

class TestPythonGetenv:
    def test_no_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", "import os\nkey = os.getenv('SECRET_KEY')")
        result = scan(tmp_path)
        assert "SECRET_KEY" in result.vars
        assert result.vars["SECRET_KEY"].default is None
        assert result.vars["SECRET_KEY"].idiom == "python_getenv"

    def test_with_string_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", "import os\nhost = os.getenv('DB_HOST', 'localhost')")
        result = scan(tmp_path)
        assert result.vars["DB_HOST"].default == "localhost"

    def test_with_int_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", "import os\nport = os.getenv('PORT', 8000)")
        result = scan(tmp_path)
        assert result.vars["PORT"].default == "8000"

    def test_with_bool_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", "import os\ndbg = os.getenv('DEBUG', False)")
        result = scan(tmp_path)
        assert result.vars["DEBUG"].default == "false"


class TestPythonEnviron:
    def test_subscript(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", "import os\nv = os.environ['JWT_SECRET']")
        result = scan(tmp_path)
        assert "JWT_SECRET" in result.vars
        assert result.vars["JWT_SECRET"].idiom == "python_environ"

    def test_get_method(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", "import os\nv = os.environ.get('OPTIONAL_FLAG', 'no')")
        result = scan(tmp_path)
        assert result.vars["OPTIONAL_FLAG"].default == "no"


class TestPydanticSettings:
    def test_basic_settings_class(self, tmp_path: Path) -> None:
        _write(tmp_path, "config.py", """\
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgres://localhost/app"
    api_key: str
    port: int = 8000
""")
        result = scan(tmp_path)
        # Pydantic field names get uppercased into env var names.
        assert "DATABASE_URL" in result.vars
        assert "API_KEY" in result.vars
        assert "PORT" in result.vars
        assert result.vars["DATABASE_URL"].default == "postgres://localhost/app"
        assert result.vars["PORT"].default == "8000"
        assert result.vars["API_KEY"].default is None
        for v in ("DATABASE_URL", "API_KEY", "PORT"):
            assert result.vars[v].idiom == "pydantic"


# ---------------------------------------------------------------------------
# Type inference + secret classification
# ---------------------------------------------------------------------------

class TestTypeInference:
    @pytest.mark.parametrize("name,expected_type,expected_secret", [
        ("API_KEY",        "secret", True),
        ("JWT_SECRET",     "secret", True),
        ("DB_PASSWORD",    "secret", True),
        ("WEATHER_TOKEN",  "secret", True),
        ("DATABASE_URL",   "url",    False),
        ("API_ENDPOINT",   "url",    False),
        ("REDIS_HOST",     "url",    False),
        ("PORT",           "int",    False),
        ("MAX_RETRIES",    "int",    False),
        ("REQUEST_TIMEOUT","int",    False),
        ("DEBUG",          "bool",   False),
        ("ENABLE_CACHE",   "bool",   False),
        ("FROM_EMAIL",     "email",  False),
        ("USER_NAME",      "string", False),  # not a special token
    ])
    def test_inferred(self, tmp_path: Path, name: str, expected_type: str, expected_secret: bool) -> None:
        _write(tmp_path, "src/app.js", f"const v = process.env.{name};")
        result = scan(tmp_path)
        ref = result.vars[name]
        assert ref.type_hint == expected_type
        assert ref.is_secret is expected_secret


# ---------------------------------------------------------------------------
# Defaults precedence (first non-empty wins)
# ---------------------------------------------------------------------------

class TestDefaultsPrecedence:
    def test_first_default_wins_over_later_none(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "import os\nx = os.getenv('SHARED', 'first')")
        _write(tmp_path, "b.py", "import os\ny = os.getenv('SHARED')")
        result = scan(tmp_path)
        assert result.vars["SHARED"].default == "first"

    def test_default_survives_across_subsequent_pure_uses(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.js", "process.env.PORT")
        _write(tmp_path, "b.py", "import os; os.getenv('PORT', 8000)")
        result = scan(tmp_path)
        assert result.vars["PORT"].default == "8000"


# ---------------------------------------------------------------------------
# required() / optional() partitioning
# ---------------------------------------------------------------------------

class TestPartitioning:
    def test_required_vs_optional(self, tmp_path: Path) -> None:
        _write(tmp_path, "main.py", """\
import os
required = os.getenv('JWT_SECRET')
optional = os.getenv('CACHE_TTL', '60')
""")
        result = scan(tmp_path)
        required_names = [v.name for v in result.required()]
        optional_names = [v.name for v in result.optional()]
        assert "JWT_SECRET" in required_names
        assert "CACHE_TTL" in optional_names
        assert "JWT_SECRET" not in optional_names


# ---------------------------------------------------------------------------
# Mixed-language scaffold (the realistic fullstack case)
# ---------------------------------------------------------------------------

class TestMixedFullstack:
    def test_react_vite_plus_fastapi(self, tmp_path: Path) -> None:
        # React/Vite frontend
        _write(tmp_path, "frontend/src/main.jsx", """\
const apiBase = import.meta.env.VITE_API_BASE_URL;
const sentry = import.meta.env.VITE_SENTRY_DSN;
""")
        # FastAPI backend
        _write(tmp_path, "backend/main.py", """\
import os
DATABASE_URL = os.environ['DATABASE_URL']
JWT_SECRET = os.getenv('JWT_SECRET')
PORT = int(os.getenv('PORT', 8000))
""")
        # Pydantic settings on top of that
        _write(tmp_path, "backend/config.py", """\
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    cors_origin: str = "*"
""")
        result = scan(tmp_path)
        # All four front+back vars present.
        assert {"VITE_API_BASE_URL", "VITE_SENTRY_DSN",
                "DATABASE_URL", "JWT_SECRET", "PORT", "CORS_ORIGIN"} <= set(result.vars.keys())
        # And classifications make sense for downstream UI rendering.
        assert result.vars["JWT_SECRET"].is_secret is True
        assert result.vars["VITE_API_BASE_URL"].type_hint == "url"
        assert result.vars["PORT"].type_hint == "int"
        assert result.vars["CORS_ORIGIN"].default == "*"


# ---------------------------------------------------------------------------
# Tolerates malformed input
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_python_syntax_error_falls_back_to_regex(self, tmp_path: Path) -> None:
        # Broken Python — but still has a recognizable getenv pattern.
        _write(tmp_path, "broken.py", "def foo(:\n    x = os.getenv('FALLBACK_KEY')")
        result = scan(tmp_path)
        # Regex catches it even when AST fails.
        assert "FALLBACK_KEY" in result.vars

    def test_lowercase_var_names_ignored(self, tmp_path: Path) -> None:
        # Convention: env vars are uppercase. Lowercase looks like a regular
        # field access (e.g. `process.env.someLocalVar` would be a typo).
        _write(tmp_path, "src/app.js", "process.env.someLocal")
        result = scan(tmp_path)
        assert "someLocal" not in result.vars

    def test_nested_subdirs_walked(self, tmp_path: Path) -> None:
        _write(tmp_path, "a/b/c/d/deep.py", "import os; os.getenv('DEEP_VAR')")
        result = scan(tmp_path)
        assert "DEEP_VAR" in result.vars
