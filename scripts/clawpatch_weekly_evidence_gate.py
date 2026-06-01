#!/usr/bin/env python3
"""Evidence gate for Clawpatch weekly reports."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.evidence_gate_tool import evidence_gate, log_evidence_gate_event


DEFAULT_REPORT = REPO_ROOT / "reports" / "clawpatch" / "weekly" / "REPORT.md"
DEFAULT_RESULT = REPO_ROOT / "reports" / "clawpatch" / "weekly" / "RESULT.md"
DEFAULT_MANIFEST = REPO_ROOT / "reports" / "clawpatch" / "weekly" / "evidence_manifest.json"


def _resolve_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _generate_command_env() -> dict[str, str]:
    env = os.environ.copy()
    profile_home = _resolve_hermes_home() / "home"
    profile_bin = str(profile_home / ".local" / "bin")
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    path_parts = [part for part in path_parts if part != profile_bin]
    env["HOME"] = str(profile_home)
    env["PATH"] = os.pathsep.join([profile_bin, *path_parts])
    return env


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Clawpatch weekly evidence gate.")
    parser.add_argument("--verify-only", action="store_true", help="Only verify existing artifacts.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="Expected Clawpatch weekly report path.")
    parser.add_argument("--result", default=str(DEFAULT_RESULT), help="RESULT.md output path.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Evidence manifest path to write.")
    parser.add_argument("--log-path", default=None, help="Optional evidence-gate log path.")
    parser.add_argument("--max-age-hours", type=float, default=168.0, help="Maximum report artifact age.")
    parser.add_argument(
        "--generate-command",
        default=None,
        help="Optional shell command to create/update Clawpatch artifacts before verification.",
    )
    return parser.parse_args()


def _write_manifest(manifest_path: Path, report: Path, result: Path, max_age_hours: float) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "requested_status": "COMPLETED",
        "result_path": str(result),
        "result_body": "Clawpatch weekly report artifacts verified through the evidence gate.",
        "checks": [
            {
                "type": "file_exists",
                "name": "Clawpatch weekly report exists",
                "path": str(report),
            },
            {
                "type": "file_fresh",
                "name": "Clawpatch weekly report is fresh and non-empty",
                "path": str(report),
                "max_age_seconds": max_age_hours * 3600,
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = _parse_args()
    report = Path(args.report).expanduser().resolve()
    result = Path(args.result).expanduser().resolve()
    manifest = Path(args.manifest).expanduser().resolve()

    if not args.verify_only and args.generate_command:
        generate_env = _generate_command_env()
        try:
            preflight = subprocess.run(
                ["codex", "--version"],
                env=generate_env,
                text=True,
                capture_output=True,
                check=False,
            )
            preflight_ok = preflight.returncode == 0
        except OSError:
            preflight_ok = False
        if not preflight_ok:
            payload = {
                "status": "FAILED",
                "requested_status": "COMPLETED",
                "reason": "codex_profile_env_unhealthy",
                "report": str(report),
            }
            log_evidence_gate_event("completion_refused", payload, args.log_path)
            print(json.dumps(payload, sort_keys=True))
            return 1

        proc = subprocess.run(
            args.generate_command,
            shell=True,
            text=True,
            check=False,
            env=generate_env,
        )
        if proc.returncode != 0:
            payload = {
                "status": "FAILED",
                "requested_status": "COMPLETED",
                "reason": "generate_command_failed",
                "exit_code": proc.returncode,
                "report": str(report),
            }
            log_evidence_gate_event("completion_refused", payload, args.log_path)
            print(json.dumps(payload, sort_keys=True))
            return proc.returncode or 1

    _write_manifest(manifest, report, result, args.max_age_hours)
    gate_result = json.loads(
        evidence_gate(
            manifest_path=str(manifest),
            requested_status="COMPLETED",
            result_path=str(result),
            log_path=args.log_path,
        )
    )
    if gate_result.get("status") != "COMPLETED":
        log_evidence_gate_event(
            "completion_refused",
            {
                "status": gate_result.get("status"),
                "requested_status": "COMPLETED",
                "reason": "fresh_report_artifacts_missing",
                "report": str(report),
                "result_path": str(result),
            },
            args.log_path or gate_result.get("log_path"),
        )
        print(json.dumps(gate_result, sort_keys=True))
        return 1

    print(json.dumps(gate_result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
