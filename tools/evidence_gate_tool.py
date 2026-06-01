"""Evidence-gated result writer.

This tool prevents agents from writing a completed RESULT.md unless a manifest
contains checks that can be evaluated successfully.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.registry import registry


COMPLETED = "COMPLETED"
FAILED = "FAILED"
MANUAL = "MANUAL_VERIFICATION_REQUIRED"


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _resolve_path(value: str | None, base_dir: Path) -> Path:
    path = Path(value or "")
    if not path.is_absolute():
        path = base_dir / path
    return path


def _default_log_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "logs" / "evidence-gate.log"
    except Exception:
        return Path.home() / ".hermes" / "evidence-gate.log"


def log_evidence_gate_event(event: str, payload: dict[str, Any], log_path: str | None = None) -> Path:
    path = Path(log_path) if log_path else _default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=_json_default) + "\n")
    return path


def _check_file_exists(check: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    path = _resolve_path(str(check.get("path", "")), base_dir)
    passed = path.exists()
    return {
        "type": "file_exists",
        "name": check.get("name") or str(check.get("path", "")),
        "passed": passed,
        "path": str(path),
        "message": "path exists" if passed else "path does not exist",
    }


def _file_state(path: Path) -> dict[str, Any]:
    exists = path.exists()
    is_file = path.is_file() if exists else False
    size = path.stat().st_size if is_file else None
    mtime = path.stat().st_mtime if is_file else None
    return {
        "path": str(path),
        "exists": exists,
        "is_file": is_file,
        "size": size,
        "mtime": mtime,
    }


def _check_file_non_empty(check: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    path = _resolve_path(str(check.get("path", "")), base_dir)
    state = _file_state(path)
    passed = bool(state["is_file"] and (state["size"] or 0) > 0)
    return {
        "type": "file_non_empty",
        "name": check.get("name") or str(check.get("path", "")),
        "passed": passed,
        **state,
        "message": (
            "file exists and is non-empty"
            if passed
            else "file is missing, not a file, or empty"
        ),
    }


def _check_file_fresh(check: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    path = _resolve_path(str(check.get("path", "")), base_dir)
    state = _file_state(path)
    try:
        max_age_seconds = float(check.get("max_age_seconds"))
    except (TypeError, ValueError):
        max_age_seconds = -1
    now = datetime.now(timezone.utc).timestamp()
    age_seconds = (now - state["mtime"]) if state["mtime"] is not None else None
    passed = bool(
        state["is_file"]
        and (state["size"] or 0) > 0
        and max_age_seconds >= 0
        and age_seconds is not None
        and age_seconds <= max_age_seconds
    )
    return {
        "type": "file_fresh",
        "name": check.get("name") or str(check.get("path", "")),
        "passed": passed,
        **state,
        "max_age_seconds": max_age_seconds,
        "age_seconds": age_seconds,
        "message": (
            "file exists, is non-empty, and is fresh"
            if passed
            else "file is missing, empty, or stale"
        ),
    }


def _check_command_unsupported(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "command",
        "name": check.get("name") or "command",
        "passed": False,
        "message": "unsupported check type: command checks are not executed",
    }


def _porcelain_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else ""
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip().strip('"')


def _check_git_clean(check: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    repo = _resolve_path(str(check.get("repo_path") or check.get("path") or "."), base_dir)
    allowed_prefixes = [str(prefix) for prefix in check.get("allowed_prefixes", [])]
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "type": "git_clean",
            "name": check.get("name") or str(repo),
            "passed": False,
            "repo_path": str(repo),
            "exit_code": proc.returncode,
            "stderr": proc.stderr[-4000:],
            "message": "git status failed",
        }
    dirty = []
    allowed = []
    for line in proc.stdout.splitlines():
        path = _porcelain_path(line)
        if any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed_prefixes):
            allowed.append(line)
        else:
            dirty.append(line)
    passed = not dirty
    return {
        "type": "git_clean",
        "name": check.get("name") or str(repo),
        "passed": passed,
        "repo_path": str(repo),
        "allowed_prefixes": allowed_prefixes,
        "dirty": dirty,
        "allowed_dirty": allowed,
        "message": "git tree clean" if passed else "git tree has unallowed changes",
    }


def _check_manual(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "manual",
        "name": check.get("name") or "manual",
        "passed": None,
        "message": check.get("message") or "manual verification required",
    }


def _evaluate_check(check: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    check_type = str(check.get("type", "")).strip()
    if check_type == "file_exists":
        return _check_file_exists(check, base_dir)
    if check_type == "file_non_empty":
        return _check_file_non_empty(check, base_dir)
    if check_type == "file_fresh":
        return _check_file_fresh(check, base_dir)
    if check_type == "command":
        return _check_command_unsupported(check)
    if check_type == "git_clean":
        return _check_git_clean(check, base_dir)
    if check_type == "manual":
        return _check_manual(check)
    return {
        "type": check_type or "unknown",
        "name": check.get("name") or check_type or "unknown",
        "passed": False,
        "message": f"unsupported check type: {check_type!r}",
    }


def _render_result_markdown(
    final_status: str,
    requested_status: str,
    manifest_path: Path,
    check_results: list[dict[str, Any]],
    body: str,
) -> str:
    lines = [
        "# Result",
        "",
        f"Status: {final_status}",
        f"Requested status: {requested_status}",
        f"Evidence manifest: {manifest_path}",
        "",
    ]
    if body:
        lines.extend([body.rstrip(), ""])
    lines.extend(["## Evidence Checks", ""])
    for result in check_results:
        marker = "PASS" if result.get("passed") is True else "MANUAL" if result.get("passed") is None else "FAIL"
        lines.append(f"- {marker} {result.get('type')}: {result.get('name')} - {result.get('message')}")
    lines.append("")
    return "\n".join(lines)


def evidence_gate(
    manifest_path: str,
    requested_status: str | None = None,
    result_path: str | None = None,
    result_body: str | None = None,
    log_path: str | None = None,
    task_id: str | None = None,
) -> str:
    """Evaluate a JSON evidence manifest and write RESULT.md with gated status."""
    manifest_file = Path(manifest_path).expanduser()
    if not manifest_file.is_absolute():
        manifest_file = Path.cwd() / manifest_file
    manifest_file = manifest_file.resolve()
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        result = {
            "success": False,
            "status": FAILED,
            "requested_status": requested_status,
            "manifest_path": str(manifest_file),
            "error": f"could not load evidence manifest: {exc}",
        }
        if (requested_status or "").upper() == COMPLETED:
            log_path_used = log_evidence_gate_event(
                "evidence_gate_refused",
                {**result, "reason": "manifest_load_failed", "task_id": task_id},
                log_path,
            )
            result["log_path"] = str(log_path_used)
        return json.dumps(result)

    if not isinstance(manifest, dict):
        return json.dumps({
            "success": False,
            "status": FAILED,
            "requested_status": requested_status,
            "manifest_path": str(manifest_file),
            "error": "evidence manifest must be a JSON object",
        })

    base_dir = _resolve_path(str(manifest.get("base_dir", ".")), manifest_file.parent)
    checks = manifest.get("checks", [])
    if not isinstance(checks, list):
        checks = []

    requested = str(requested_status or manifest.get("requested_status") or COMPLETED)
    output_path_value = result_path or manifest.get("result_path") or "RESULT.md"
    output_path = _resolve_path(str(output_path_value), base_dir)
    body = result_body if result_body is not None else str(manifest.get("result_body", ""))

    check_results = [
        _evaluate_check(check, base_dir)
        for check in checks
        if isinstance(check, dict)
    ]
    if not check_results:
        check_results.append({
            "type": "manifest",
            "name": "evidence checks",
            "passed": False,
            "message": "at least one evidence check is required",
        })
    failures = [result for result in check_results if result.get("passed") is False]
    manual = [result for result in check_results if result.get("passed") is None]
    if failures:
        final_status = FAILED
    elif manual:
        final_status = MANUAL
    else:
        final_status = requested

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_result_markdown(final_status, requested, manifest_file, check_results, body),
        encoding="utf-8",
    )

    response = {
        "success": final_status == requested and not failures and not manual,
        "status": final_status,
        "requested_status": requested,
        "manifest_path": str(manifest_file),
        "result_path": str(output_path),
        "checks": check_results,
        "failure_count": len(failures),
        "manual_count": len(manual),
    }
    if requested.upper() == COMPLETED and final_status.upper() != COMPLETED:
        log_path_used = log_evidence_gate_event(
            "evidence_gate_refused",
            {
                **response,
                "reason": "completion_refused",
                "task_id": task_id,
            },
            log_path,
        )
        response["log_path"] = str(log_path_used)
    return json.dumps(response, default=_json_default)


registry.register(
    name="evidence_gate",
    toolset="evidence_gate",
    schema={
        "name": "evidence_gate",
        "description": (
            "Evaluate an evidence manifest and write RESULT.md only with a "
            "status supported by non-executing file, git-clean, or manual checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "manifest_path": {
                    "type": "string",
                    "description": "Path to the evidence manifest JSON.",
                },
                "requested_status": {
                    "type": "string",
                    "description": "Requested final status, for example COMPLETED.",
                },
                "result_path": {
                    "type": "string",
                    "description": "Optional RESULT.md output path overriding the manifest.",
                },
                "result_body": {
                    "type": "string",
                    "description": "Optional markdown body to include in RESULT.md.",
                },
                "log_path": {
                    "type": "string",
                    "description": "Optional evidence-gate refusal log path.",
                },
            },
            "required": ["manifest_path"],
        },
    },
    handler=lambda args, **kw: evidence_gate(
        manifest_path=args.get("manifest_path", ""),
        requested_status=args.get("requested_status"),
        result_path=args.get("result_path"),
        result_body=args.get("result_body"),
        log_path=args.get("log_path"),
        task_id=kw.get("task_id"),
    ),
    description="Evidence-gated RESULT.md writer",
    emoji="",
)
