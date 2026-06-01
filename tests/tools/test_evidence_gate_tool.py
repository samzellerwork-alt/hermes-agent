import json
import os
import subprocess
import time
from pathlib import Path

from tools.evidence_gate_tool import MANUAL, evidence_gate


def _write_manifest(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_evidence_gate_writes_completed_when_checks_pass(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("done\n", encoding="utf-8")
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [
                {"type": "file_exists", "path": "artifact.txt"},
                {"type": "file_non_empty", "path": "artifact.txt"},
            ],
        },
    )

    result = json.loads(evidence_gate(str(manifest)))

    assert result["status"] == "COMPLETED"
    assert result["success"] is True
    assert (tmp_path / "RESULT.md").read_text(encoding="utf-8").startswith(
        "# Result\n\nStatus: COMPLETED"
    )


def test_evidence_gate_refuses_command_checks_without_executing(tmp_path):
    marker = tmp_path / "marker"
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [
                {
                    "type": "command",
                    "name": "malicious command",
                    "command": f"touch {marker}",
                },
            ],
        },
    )

    result = json.loads(evidence_gate(str(manifest)))

    assert result["status"] == "FAILED"
    assert result["success"] is False
    assert result["checks"][0]["type"] == "command"
    assert "unsupported" in result["checks"][0]["message"]
    assert not marker.exists()


def test_evidence_gate_file_fresh_requires_non_empty_recent_file(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("done\n", encoding="utf-8")
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [
                {
                    "type": "file_fresh",
                    "path": "artifact.txt",
                    "max_age_seconds": 60,
                },
            ],
        },
    )

    result = json.loads(evidence_gate(str(manifest)))

    assert result["status"] == "COMPLETED"
    assert result["checks"][0]["size"] > 0
    assert result["checks"][0]["age_seconds"] <= 60


def test_evidence_gate_file_fresh_fails_for_stale_file(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("done\n", encoding="utf-8")
    stale = time.time() - 120
    os.utime(artifact, (stale, stale))
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [
                {
                    "type": "file_fresh",
                    "path": "artifact.txt",
                    "max_age_seconds": 60,
                },
            ],
        },
    )

    result = json.loads(evidence_gate(str(manifest)))

    assert result["status"] == "FAILED"
    assert result["checks"][0]["passed"] is False
    assert result["checks"][0]["age_seconds"] > 60


def test_evidence_gate_refuses_completed_and_logs_when_evidence_fails(tmp_path):
    log_path = tmp_path / "evidence-gate.log"
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [{"type": "file_exists", "path": "missing.txt"}],
        },
    )

    result = json.loads(evidence_gate(str(manifest), log_path=str(log_path)))

    assert result["status"] == "FAILED"
    assert result["success"] is False
    assert "Status: FAILED" in (tmp_path / "RESULT.md").read_text(encoding="utf-8")
    log_record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert log_record["event"] == "evidence_gate_refused"
    assert log_record["reason"] == "completion_refused"
    assert log_record["requested_status"] == "COMPLETED"


def test_evidence_gate_manual_check_forces_manual_verification(tmp_path):
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [{"type": "manual", "name": "human signoff"}],
        },
    )

    result = json.loads(evidence_gate(str(manifest)))

    assert result["status"] == MANUAL
    assert result["manual_count"] == 1
    assert "Status: MANUAL_VERIFICATION_REQUIRED" in (tmp_path / "RESULT.md").read_text(encoding="utf-8")


def test_evidence_gate_git_clean_respects_allowed_prefixes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "allowed.log").write_text("ok\n", encoding="utf-8")
    (repo / "blocked.txt").write_text("dirty\n", encoding="utf-8")
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        {
            "requested_status": "COMPLETED",
            "result_path": "RESULT.md",
            "checks": [
                {
                    "type": "git_clean",
                    "repo_path": str(repo),
                    "allowed_prefixes": ["allowed.log"],
                }
            ],
        },
    )

    result = json.loads(evidence_gate(str(manifest), log_path=str(tmp_path / "gate.log")))

    assert result["status"] == "FAILED"
    assert result["checks"][0]["dirty"] == ["?? blocked.txt"]


def test_evidence_gate_registered_in_core_toolset():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
    from tools.registry import registry

    entry = registry.get_entry("evidence_gate")
    assert entry is not None
    assert entry.toolset == "evidence_gate"
    assert "evidence_gate" in _HERMES_CORE_TOOLS
    assert TOOLSETS["evidence_gate"]["tools"] == ["evidence_gate"]
