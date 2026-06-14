from types import SimpleNamespace

from skyn3t.core.orchestrator import Orchestrator


def test_wire_rag_agents_sets_shared_rag_for_github_agents():
    rag = object()
    orchestrator = Orchestrator()
    orchestrator._rag = rag
    github_explorer = SimpleNamespace(name="custom-gh", agent_type="github_explorer")
    github_ingestor = SimpleNamespace(name="github_ingestor", agent_type="github_ingestor")

    orchestrator.agents = {
        "custom-gh": github_explorer,
        "github_ingestor": github_ingestor,
    }

    orchestrator._wire_rag_agents()

    assert github_explorer.rag is rag
    assert github_ingestor.rag is rag


def test_wire_rag_agents_preserves_existing_aliases_and_rag():
    shared_rag = object()
    existing_rag = object()
    orchestrator = Orchestrator()
    orchestrator._rag = shared_rag
    explorer = SimpleNamespace(name="explorer", agent_type="explorer")
    project_memory = SimpleNamespace(
        name="project_memory",
        agent_type="project_memory",
        rag=existing_rag,
    )
    unrelated = SimpleNamespace(name="writer", agent_type="writer")

    orchestrator.agents = {
        "explorer": explorer,
        "project_memory": project_memory,
        "writer": unrelated,
    }

    orchestrator._wire_rag_agents()

    assert explorer.rag is shared_rag
    assert project_memory.rag is existing_rag
    assert not hasattr(unrelated, "rag")
