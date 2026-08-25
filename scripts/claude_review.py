#!/usr/bin/env python3
"""Run Claude Code as a bounded adversarial reviewer from a Codex workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


MAX_PACKET_BYTES = 9_000_000
REQUIRED_FLAGS = (
    "--allowedTools",
    "--disable-slash-commands",
    "--name",
    "--no-chrome",
    "--output-format",
    "--permission-mode",
    "--resume",
    "--safe-mode",
    "--session-id",
    "--strict-mcp-config",
    "--tools",
)
VERDICTS = {"VERDICT: APPROVED", "VERDICT: REVISE"}


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def find_claude() -> str:
    executable = shutil.which("claude")
    if executable is None:
        raise RuntimeError("Claude Code CLI is unavailable")
    return executable


def select_result_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in reversed(payload):
            if isinstance(item, dict) and (
                item.get("type") == "result" or "result" in item
            ):
                return item
    return None


def parse_json_document(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise RuntimeError("Claude returned no JSON output")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    payloads: list[Any] = []
    for line in stripped.splitlines():
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if payloads:
        return payloads
    raise RuntimeError("Claude output did not contain a JSON object")


def assistant_text(payload: Any) -> str | None:
    items = payload if isinstance(payload, list) else [payload]
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "assistant":
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n\n".join(chunks) if chunks else None


def session_from_document(payload: Any, fallback: str) -> str:
    items = payload if isinstance(payload, list) else [payload]
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        value = item.get("session_id")
        if isinstance(value, str) and value:
            return value
    return fallback


def claude_error_detail(
    payload: dict[str, Any] | None,
    stderr: str,
    returncode: int,
) -> str:
    details: list[str] = []
    if payload is not None:
        for key in ("terminal_reason", "errors"):
            value = payload.get(key)
            if value:
                details.append(str(value))
    if stderr.strip():
        details.append(stderr.strip())
    if not details:
        details.append(f"exit {returncode}" if returncode else "missing result object")
    normalized = " | ".join(" ".join(detail.split()) for detail in details)
    return normalized[:500]


def resolve_path(value: str, *, label: str, strict: bool) -> Path:
    candidate = Path(value)
    if candidate.is_symlink():
        raise RuntimeError(f"refusing symlink for {label}: {candidate}")
    return candidate.resolve(strict=strict)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"refusing to overwrite output: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: dict[str, Any], *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise RuntimeError(f"refusing to overwrite state: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def doctor() -> int:
    try:
        executable = find_claude()
        version_result = run((executable, "--version"), timeout=20)
        help_result = run((executable, "--help"), timeout=20)
        auth_result = run((executable, "auth", "status", "--json"), timeout=20)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1

    help_text = help_result.stdout + help_result.stderr
    missing_flags = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
    try:
        auth = json.loads(auth_result.stdout) if auth_result.stdout.strip() else {}
    except json.JSONDecodeError:
        auth = {}
    logged_in = bool(auth.get("loggedIn"))
    ok = (
        version_result.returncode == 0
        and help_result.returncode == 0
        and auth_result.returncode == 0
        and logged_in
        and not missing_flags
    )
    print(
        json.dumps(
            {
                "auth_method": auth.get("authMethod"),
                "claude": Path(executable).name,
                "logged_in": logged_in,
                "missing_flags": missing_flags,
                "ok": ok,
                "subscription_type": auth.get("subscriptionType"),
                "version": version_result.stdout.strip(),
            },
            sort_keys=True,
        )
    )
    return 0 if ok else 1


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid review state {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid review state object: {path}")
    try:
        uuid.UUID(str(payload["session_id"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"review state has no valid session_id: {path}") from exc
    return payload


def make_state(
    *,
    executable: str,
    packet_bytes: bytes,
    max_budget_usd: float | None,
    model: str | None,
    repo: Path,
    repo_read: bool,
    round_number: int,
    session_id: str,
    started_at: str | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "claude_version": run((executable, "--version"), timeout=20).stdout.strip(),
        "last_packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "last_review_at": now,
        "max_budget_usd": max_budget_usd,
        "model_argument": model,
        "repo": str(repo),
        "repo_read": repo_read,
        "rounds": round_number,
        "session_id": session_id,
        "started_at": started_at or now,
    }


def model_from_payload(payload: dict[str, Any], fallback: str | None) -> str:
    model_usage = payload.get("modelUsage")
    observed_models = list(model_usage) if isinstance(model_usage, dict) else []
    reported = payload.get("model") or (observed_models[0] if observed_models else None)
    return str(reported or fallback or "CLI default")


def build_command(
    *,
    executable: str,
    session_id: str,
    resume: bool,
    repo_read: bool,
    model: str | None,
    max_budget_usd: float | None,
) -> list[str]:
    command = [
        executable,
        "-p",
        "--safe-mode",
        "--no-chrome",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "json",
    ]
    if repo_read:
        command.extend(("--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep"))
    else:
        command.extend(("--tools", ""))
    if resume:
        command.extend(("--resume", session_id))
    else:
        command.extend(("--session-id", session_id, "--name", f"clodex-{session_id[:8]}"))
    if model:
        command.extend(("--model", model))
    if max_budget_usd is not None:
        command.extend(("--max-budget-usd", f"{max_budget_usd:.2f}"))
    command.append(
        "Perform the adversarial review described in the piped packet. "
        "Return only the review and its required final verdict."
    )
    return command


def verdict_from_result(result: str) -> str | None:
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    while lines and lines[-1] in {"```", "```text", "```markdown"}:
        lines.pop()
    if not lines:
        return None
    return lines[-1] if lines[-1] in VERDICTS else None


def review(args: argparse.Namespace, *, resume: bool) -> int:
    try:
        executable = find_claude()
        repo = Path(args.repo).resolve(strict=True)
        if not repo.is_dir():
            raise RuntimeError(f"repository path is not a directory: {repo}")
        packet_path = resolve_path(args.packet, label="packet", strict=True)
        if not packet_path.is_file():
            raise RuntimeError(f"packet is not a regular file: {packet_path}")
        packet_bytes = packet_path.read_bytes()
        if len(packet_bytes) > MAX_PACKET_BYTES:
            raise RuntimeError(
                f"packet is {len(packet_bytes)} bytes; limit is {MAX_PACKET_BYTES}"
            )
        packet = packet_bytes.decode("utf-8", errors="strict")
        output_path = resolve_path(args.output, label="output", strict=False)
        state_path = resolve_path(args.state, label="state", strict=False)
        if len({packet_path, output_path, state_path}) != 3:
            raise RuntimeError("packet, output, and state paths must be distinct")
        if output_path.exists():
            raise RuntimeError(f"refusing to overwrite output: {output_path}")

        if resume:
            if not state_path.is_file():
                raise RuntimeError(f"resume state does not exist: {state_path}")
            state = load_state(state_path)
            session_id = str(state["session_id"])
            if Path(str(state.get("repo", ""))).resolve(strict=False) != repo:
                raise RuntimeError("review state belongs to a different repository")
            repo_read = bool(state.get("repo_read", False))
            model = state.get("model_argument")
            max_budget_usd = state.get("max_budget_usd")
            round_number = int(state.get("rounds", 0)) + 1
        else:
            if state_path.exists():
                raise RuntimeError(f"refusing to overwrite state: {state_path}")
            session_id = str(uuid.uuid4())
            repo_read = bool(args.repo_read)
            model = args.model
            max_budget_usd = args.max_budget_usd
            round_number = 1

        command = build_command(
            executable=executable,
            session_id=session_id,
            resume=resume,
            repo_read=repo_read,
            model=model,
            max_budget_usd=max_budget_usd,
        )
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "command": command[:-1] + ["<fixed review instruction>"],
                        "packet_bytes": len(packet_bytes),
                        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
                        "repo": str(repo),
                        "repo_read": repo_read,
                        "resume": resume,
                        "session_id": session_id,
                    },
                    sort_keys=True,
                )
            )
            return 0

        completed = run(
            command,
            cwd=repo,
            input_text=packet,
            timeout=args.timeout_seconds,
        )
        try:
            document = parse_json_document(completed.stdout)
        except RuntimeError as exc:
            if completed.returncode != 0:
                detail = completed.stderr.strip() or str(exc)
                raise RuntimeError(
                    f"Claude exited {completed.returncode}: {detail}"
                ) from exc
            raise
        payload = select_result_payload(document)
        returned_session = session_from_document(document, session_id)
        if returned_session != session_id:
            raise RuntimeError(
                f"Claude returned unexpected session {returned_session}; expected {session_id}"
            )

        started_at = state.get("started_at") if resume else None
        if completed.returncode != 0 or payload is None or payload.get("is_error"):
            partial = assistant_text(document)
            payload_result = payload.get("result") if payload is not None else None
            if partial is None and isinstance(payload_result, str) and payload_result.strip():
                partial = payload_result.strip()
            detail = claude_error_detail(payload, completed.stderr, completed.returncode)
            if partial is None:
                raise RuntimeError(f"Claude exited {completed.returncode}: {detail}")
            marker = (
                f"[clodex-loop] PARTIAL OUTPUT - Claude error: {detail}; "
                "not an approval."
            )
            atomic_write_text(output_path, marker + "\n\n" + partial.rstrip() + "\n")
            next_state = make_state(
                executable=executable,
                packet_bytes=packet_bytes,
                max_budget_usd=max_budget_usd,
                model=model,
                repo=repo,
                repo_read=repo_read,
                round_number=round_number,
                session_id=session_id,
                started_at=started_at,
            )
            atomic_write_json(state_path, next_state, replace=resume)
            reported_verdict = verdict_from_result(partial)
            result_payload = payload or {}
            print(
                json.dumps(
                    {
                        "claude_error": detail,
                        "cost_usd": result_payload.get("total_cost_usd"),
                        "model": model_from_payload(result_payload, model),
                        "output": str(output_path),
                        "reported_verdict": reported_verdict,
                        "repo_read": repo_read,
                        "round": round_number,
                        "session_id": session_id,
                        "verdict": None,
                    },
                    sort_keys=True,
                )
            )
            print(
                "Claude returned an error; partial reviewer output and exact session state "
                "were preserved. Do not treat its verdict as approval or retry blindly.",
                file=sys.stderr,
            )
            return 4

        result = payload.get("result")
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("Claude JSON has no non-empty result")

        atomic_write_text(output_path, result.rstrip() + "\n")
        next_state = make_state(
            executable=executable,
            packet_bytes=packet_bytes,
            max_budget_usd=max_budget_usd,
            model=model,
            repo=repo,
            repo_read=repo_read,
            round_number=round_number,
            session_id=session_id,
            started_at=started_at,
        )
        atomic_write_json(state_path, next_state, replace=resume)

        verdict = verdict_from_result(result)
        summary = {
            "cost_usd": payload.get("total_cost_usd"),
            "model": model_from_payload(payload, model),
            "output": str(output_path),
            "repo_read": repo_read,
            "round": round_number,
            "session_id": session_id,
            "verdict": verdict,
        }
        print(json.dumps(summary, sort_keys=True))
        if verdict is None:
            print(
                "Claude output was preserved but lacked an exact final verdict; do not retry blindly.",
                file=sys.stderr,
            )
            return 3
        return 0
    except subprocess.TimeoutExpired:
        print(
            f"Claude review timed out after {args.timeout_seconds} seconds; do not retry blindly.",
            file=sys.stderr,
        )
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"Claude review failed: {exc}", file=sys.stderr)
        return 1


def add_common_review_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="trusted target repository")
    parser.add_argument("--packet", required=True, help="bounded redacted review packet")
    parser.add_argument("--output", required=True, help="new reviewer-output path")
    parser.add_argument("--state", required=True, help="review-session state path")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Invoke Claude Code as a safe, bounded adversarial reviewer."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="verify CLI, auth, and safety flags")

    start = subparsers.add_parser("start", help="start a fresh review session")
    add_common_review_args(start)
    start.add_argument("--model", help="optional explicit Claude model")
    start.add_argument("--max-budget-usd", type=float)
    start.add_argument(
        "--repo-read",
        action="store_true",
        help="allow only Claude Read/Glob/Grep tools; default is no tools",
    )

    resume = subparsers.add_parser("resume", help="resume the exact review session")
    add_common_review_args(resume)
    args = parser.parse_args(argv)
    if args.command in {"start", "resume"}:
        if args.timeout_seconds < 30 or args.timeout_seconds > 3600:
            parser.error("--timeout-seconds must be between 30 and 3600")
    if args.command == "start" and args.max_budget_usd is not None:
        if args.max_budget_usd <= 0 or args.max_budget_usd > 100:
            parser.error("--max-budget-usd must be greater than 0 and at most 100")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "doctor":
        return doctor()
    return review(args, resume=args.command == "resume")


if __name__ == "__main__":
    raise SystemExit(main())
