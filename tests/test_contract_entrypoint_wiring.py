"""Entrypoint wiring contract checks."""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.contract_engine import check_contract


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_contract_flags_orphan_components_not_mounted_in_app(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "import React from 'react';\nexport default function App() {\n"
        "  return <div className='shell'>Hello</div>;\n}\n",
    )
    _write(
        scaffold / "src" / "components" / "HabitList.jsx",
        "import React from 'react';\nexport default function HabitList() {\n"
        "  return <section className='habit-list'>List</section>;\n}\n",
    )
    _write(
        scaffold / "src" / "components" / "HabitCard.jsx",
        "import React from 'react';\nexport default function HabitCard() {\n"
        "  return <article className='habit-card'>Card</article>;\n}\n",
    )

    report = check_contract(scaffold, "Build a habit tracker UI", artifact)

    orphan = [f for f in report.findings if f.category == "orphan_components"]
    assert orphan
    assert orphan[0].severity == "blocker"
    assert report.ok is False


def test_contract_accepts_app_that_mounts_components(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "import React from 'react';\nimport HabitList from './components/HabitList.jsx';\n"
        "export default function App() {\n  return <HabitList />;\n}\n",
    )
    _write(
        scaffold / "src" / "components" / "HabitList.jsx",
        "import React from 'react';\nexport default function HabitList() {\n"
        "  return <section className='habit-list'>List</section>;\n}\n",
    )

    report = check_contract(scaffold, "Build a habit tracker UI", artifact)

    assert not [f for f in report.findings if f.category == "orphan_components"]
