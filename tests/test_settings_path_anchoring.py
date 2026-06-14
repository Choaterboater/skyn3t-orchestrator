"""Path settings must anchor relative defaults to the REPO ROOT, not the process
CWD — otherwise a server/tick launched from .../Skyn3t/data resolves "./data" to
data/data/* and splits runtime state (the skills-dir collapse bug, 9b57152).
Explicit absolute paths (tests' tmp dirs) must still pass through unchanged so
test isolation (63d03a6) is preserved."""

from pathlib import Path

from skyn3t.config.settings import _REPO_ROOT, Settings

_REL_DEFAULT_ENV = (
    "DATA_DIR",
    "VECTOR_DB_PATH",
    "SKYN3T_MODEL_ROUTING_PATH",
    "SKYN3T_SNAPSHOT_DIR",
)


def test_relative_data_defaults_anchor_to_repo_root(monkeypatch):
    # Drop the conftest absolute overrides so the "./data" defaults are exercised.
    for key in _REL_DEFAULT_ENV:
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.data_dir == (_REPO_ROOT / "data").resolve()
    assert Path(s.vector_db_path) == (_REPO_ROOT / "data/vector_db").resolve()
    assert s.model_routing_path == (_REPO_ROOT / "data/model_routing.json").resolve()
    assert s.snapshot_dir == (_REPO_ROOT / "data/checkpoints").resolve()
    # None of them collapse into a doubled data/data segment.
    for p in (s.data_dir, Path(s.vector_db_path), s.model_routing_path, s.snapshot_dir):
        assert "data/data" not in p.as_posix()


def test_absolute_path_env_passes_through(monkeypatch, tmp_path):
    # Explicit absolute env values (what conftest/tests set) are honored verbatim.
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "vdb"))
    monkeypatch.setenv("SKYN3T_MODEL_ROUTING_PATH", str(tmp_path / "mr.json"))
    monkeypatch.setenv("SKYN3T_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    s = Settings()
    assert Path(s.vector_db_path) == (tmp_path / "vdb").resolve()
    assert s.model_routing_path == (tmp_path / "mr.json").resolve()
    assert s.snapshot_dir == (tmp_path / "snaps").resolve()
