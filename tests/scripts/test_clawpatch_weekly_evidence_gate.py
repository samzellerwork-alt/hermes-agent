import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "clawpatch_weekly_evidence_gate.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("clawpatch_weekly_evidence_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_clawpatch_verify_only_completes_with_fresh_report(tmp_path):
    report = tmp_path / "REPORT.md"
    report.write_text("# Weekly\n\nVerified.\n", encoding="utf-8")
    result_path = tmp_path / "RESULT.md"
    manifest = tmp_path / "manifest.json"
    log_path = tmp_path / "evidence-gate.log"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verify-only",
            "--report",
            str(report),
            "--result",
            str(result_path),
            "--manifest",
            str(manifest),
            "--log-path",
            str(log_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "COMPLETED"
    assert payload["success"] is True
    assert "Status: COMPLETED" in result_path.read_text(encoding="utf-8")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["checks"][1]["type"] == "file_fresh"
    assert "command" not in manifest_payload["checks"][1]


def test_clawpatch_verify_only_refuses_completion_without_fresh_report(tmp_path):
    report = tmp_path / "missing-report.md"
    result_path = tmp_path / "RESULT.md"
    manifest = tmp_path / "manifest.json"
    log_path = tmp_path / "evidence-gate.log"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verify-only",
            "--report",
            str(report),
            "--result",
            str(result_path),
            "--manifest",
            str(manifest),
            "--log-path",
            str(log_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "FAILED"
    assert payload["requested_status"] == "COMPLETED"
    assert "Status: FAILED" in result_path.read_text(encoding="utf-8")
    log_text = log_path.read_text(encoding="utf-8")
    assert "evidence_gate_refused" in log_text
    assert "completion_refused" in log_text


def test_clawpatch_verify_only_refuses_stale_report(tmp_path):
    report = tmp_path / "REPORT.md"
    report.write_text("# Weekly\n\nOld.\n", encoding="utf-8")
    stale = time.time() - 7200
    os.utime(report, (stale, stale))
    result_path = tmp_path / "RESULT.md"
    manifest = tmp_path / "manifest.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verify-only",
            "--report",
            str(report),
            "--result",
            str(result_path),
            "--manifest",
            str(manifest),
            "--max-age-hours",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "FAILED"
    assert payload["checks"][0]["passed"] is True
    assert payload["checks"][1]["type"] == "file_fresh"
    assert payload["checks"][1]["passed"] is False


def test_generate_command_uses_profile_local_codex_env(monkeypatch, tmp_path):
    module = _load_script_module()
    hermes_home = tmp_path / "hermes-profile"
    profile_home = hermes_home / "home"
    profile_bin = str(profile_home / ".local" / "bin")
    calls = []

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", profile_bin, "/bin"]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--generate-command",
            "clawpatch weekly",
            "--report",
            str(tmp_path / "REPORT.md"),
            "--result",
            str(tmp_path / "RESULT.md"),
            "--manifest",
            str(tmp_path / "manifest.json"),
        ],
    )
    monkeypatch.setattr(module, "_write_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "evidence_gate",
        lambda **kwargs: json.dumps({"status": "COMPLETED", "success": True}),
    )

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0
    assert calls[0][0] == ["codex", "--version"]
    assert calls[1][0] == "clawpatch weekly"
    generate_env = calls[1][1]["env"]
    assert generate_env["HOME"].endswith("/home")
    assert generate_env["HOME"] == str(profile_home)
    path_parts = generate_env["PATH"].split(os.pathsep)
    assert path_parts[0] == profile_bin
    assert path_parts.count(profile_bin) == 1


def test_generate_command_refuses_when_codex_preflight_fails(monkeypatch, capsys, tmp_path):
    module = _load_script_module()
    calls = []

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-profile"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--generate-command",
            "clawpatch weekly",
            "--report",
            str(tmp_path / "REPORT.md"),
            "--result",
            str(tmp_path / "RESULT.md"),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--log-path",
            str(tmp_path / "gate.log"),
        ],
    )
    monkeypatch.setattr(module, "_write_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "evidence_gate",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("evidence_gate should not run")),
    )

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd == ["codex", "--version"]:
            return subprocess.CompletedProcess(cmd, 127)
        raise AssertionError("generate command should not run")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FAILED"
    assert payload["reason"] == "codex_profile_env_unhealthy"
    assert [cmd for cmd, _kwargs in calls] == [["codex", "--version"]]
    assert "codex_profile_env_unhealthy" in (tmp_path / "gate.log").read_text(encoding="utf-8")
