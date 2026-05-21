"""T095–T097/T100 — required docs and final review artifacts exist."""

from __future__ import annotations

from pathlib import Path


def test_readme_links_core_spec_kit_artifacts() -> None:
    text = Path("README.md").read_text()
    assert ".specify/memory/constitution.md" in text
    assert "specs/001-alertbot-core/spec.md" in text
    assert "specs/001-alertbot-core/plan.md" in text
    assert "specs/001-alertbot-core/quickstart.md" in text


def test_runbook_covers_audit_meta_channel_and_secret_rotation() -> None:
    text = Path("docs/runbook.md").read_text()
    for needle in ["audit_log", "meta-channel", "silence abuse", "rotate"]:
        assert needle in text


def test_rollback_doc_contains_helm_rollback_and_db_steps() -> None:
    text = Path("docs/rollback.md").read_text()
    assert "helm rollback" in text
    assert "database" in text.lower()
    assert "silence" in text.lower()


def test_final_acceptance_review_exists_with_constitution_and_sc_trace() -> None:
    text = Path("docs/final-acceptance-review.md").read_text()
    assert "SC-001" in text
    assert "SC-010" in text
    assert "Constitution" in text
    assert "I. Webhook-First" in text
