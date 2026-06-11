from __future__ import annotations

from skyn3t.intelligence.domain_corpus import (
    build_github_search_queries,
    make_golden_project_record,
    parse_github_repo,
    redact_sensitive_text,
    source_from_uri,
)


def test_redact_sensitive_networking_text() -> None:
    result = redact_sensitive_text(
        "ARUBA_API_TOKEN=abc123\n"
        "switch=10.10.20.30\n"
        "mac=aa:bb:cc:dd:ee:ff\n"
        "Authorization: Bearer secret-token\n"
    )

    assert "abc123" not in result.text
    assert "10.10.20.30" not in result.text
    assert "aa:bb:cc:dd:ee:ff" not in result.text
    assert "secret-token" not in result.text
    assert {finding.kind for finding in result.findings} >= {
        "assignment_secret",
        "ipv4_address",
        "mac_address",
        "authorization_header",
    }


def test_source_from_github_uri_is_read_only_candidate_clone() -> None:
    source = source_from_uri("https://github.com/example/aruba-toolkit.git", ref="main")

    assert source.source_type == "github"
    assert source.read_only_original is True
    assert source.clone_strategy == "local_clone_or_fork"
    assert parse_github_repo(source.uri) == ("example", "aruba-toolkit")


def test_make_golden_project_record_infers_tags_and_redacts_notes() -> None:
    record = make_golden_project_record(
        title="Aruba field troubleshooting CLI",
        source_uri="/tmp/aruba-cli",
        stack="Python FastAPI",
        commands=["pytest -q", "python -m app --dry-run"],
        file_shape=["README.md", "src/main.py", "src/main.py"],
        reusable_patterns=[
            "Use dry-run mode before applying config.",
            "password=supersecret",
        ],
        quality_notes="Validates AOS-CX inventory and hides 192.168.1.10.",
    )

    assert record.source.source_type == "local"
    assert record.source.read_only_original is True
    assert "aruba" in record.vendor_tags
    assert "field_troubleshooting" in record.domain_tags
    assert "inventory_config" in record.domain_tags
    assert record.file_shape == ["README.md", "src/main.py"]
    assert "supersecret" not in "\n".join(record.reusable_patterns)
    assert "192.168.1.10" not in record.quality_notes


def test_build_github_search_queries_for_networking_domains() -> None:
    queries = build_github_search_queries(
        vendors=["aruba", "juniper"],
        domains=["field_troubleshooting", "automation_scripts"],
        extra_terms=["python"],
    )

    assert any("aos-cx aruba" in query for query in queries)
    assert any("junos juniper" in query for query in queries)
    assert any("troubleshooting diagnostics" in query for query in queries)
    assert any("network automation" in query for query in queries)
    assert all("python" in query for query in queries)
