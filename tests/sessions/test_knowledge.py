import json

import pytest

from bm25_search.sessions.config import Config
from bm25_search.sessions.db import Database
from bm25_search.sessions.models import SessionError
from bm25_search.sessions.service import Service
from test_runtime import setup_project, hit


def record(service, evidence, **extra):
    return service.call("knowledge_record", dict(title="[synthetic] generation check", claim="[synthetic] compare generation before commit",
                                                evidence=[dict(session_id=evidence["session_id"], anchor=evidence["anchor"])], generated_by="codex", **extra))


def test_optional_knowledge_migration_pinned_evidence_and_forget(tmp_path):
    root, _ = setup_project(tmp_path, ("codex",))
    service = Service(root, "codex", knowledge=True)
    service.sync()
    evidence = hit(service)
    saved = record(service, evidence)
    read = service.call("knowledge_read", dict(knowledge_id=saved["knowledge_id"]))
    assert read["evidence_state"] == "current"
    assert read["knowledge"]["status"] == "proposed"
    assert read["knowledge"]["verification"] == "unverified"
    assert read["evidence"][0]["snapshot"]
    assert service.call("knowledge_search", dict(query="generation"))["results"]
    assert list(root.glob(".bm25_sessions.db.before-v2-*.bak"))
    with Database(root) as db:
        db.forget(evidence["session_id"], apply=True)
        assert db.connection.execute("SELECT count(*) FROM knowledge_evidence").fetchone()[0] == 0
    assert not service.call("knowledge_search", dict(query="generation"))["results"]
    assert service.call("knowledge_read", dict(knowledge_id=saved["knowledge_id"]))["evidence_state"] == "missing_evidence"


def test_tested_requires_actual_execution_and_acceptance_is_separate(tmp_path):
    root, _ = setup_project(tmp_path, ("codex", "agy"))
    service = Service(root, "codex", knowledge=True)
    service.sync()
    text_evidence = hit(service, agent="codex")
    with pytest.raises(SessionError, match="execution_evidence_required"):
        record(service, text_evidence, verification="tested")
    execution = hit(service, query="exit_code", agent="agy")
    saved = record(service, execution, verification="tested")
    assert saved["record_status"] == "proposed"
    assert saved["verification"] == "tested"
    accepted = record(service, execution, knowledge_id=saved["knowledge_id"], verification="tested", status="accepted")
    assert accepted["record_status"] == "accepted"


def test_changed_source_redaction_and_superseding_preserve_history(tmp_path):
    root, inputs = setup_project(tmp_path, ("codex",))
    service = Service(root, "codex", knowledge=True)
    service.sync()
    evidence = hit(service)
    saved = record(service, evidence)
    config = Config(root)
    config.data["redaction"]["rules"] = [dict(pattern="generation", replacement="MASKED")]
    config.save()
    service = Service(root, "codex", knowledge=True)
    service.sync()
    old = service.call("knowledge_read", dict(knowledge_id=saved["knowledge_id"]))
    assert old["evidence_state"] == "stale_evidence"
    assert "generation" not in json.dumps(old)
    assert not service.call("knowledge_search", dict(query="MASKED"))["results"]
    fresh = hit(service, query="MASKED")
    newer = record(service, fresh, supersedes_id=saved["knowledge_id"])
    assert service.call("knowledge_read", dict(knowledge_id=saved["knowledge_id"]))["knowledge"]["status"] == "superseded"
    assert newer["knowledge_id"] != saved["knowledge_id"]
