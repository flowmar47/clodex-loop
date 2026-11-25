#!/usr/bin/env python3
"""Build a bounded, redacted packet for an independent external review."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_MAX_BYTES = 9_000_000
DEFAULT_UNIFIED_CONTEXT = 80
MIN_UNIFIED_CONTEXT = 3
MAX_UNIFIED_CONTEXT = 200
DEFAULT_PRIMARY = "current host agent"
DEFAULT_REVIEWER = "Claude Code CLI default (unpinned)"
MAX_ROLE_CHARS = 200
MAX_UNTRACKED_FILE_BYTES = 256_000
SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS = {
    "plan": SKILL_DIR / "references" / "plan-review-prompt.md",
    "code": SKILL_DIR / "references" / "code-review-prompt.md",
}
SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "secrets.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "kubeconfig",
}
SENSITIVE_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".mobileprovision",
    ".tfvars",
    ".jks",
    ".keystore",
}


def _replace_quoted_assignment(match: re.Match[str]) -> str:
    return f'{match.group(1)}{match.group(2)}"<REDACTED_SECRET>"'


def _replace_env_assignment(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(3)}<REDACTED_SECRET>"


SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<REDACTED_PRIVATE_KEY>",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED_AWS_ACCESS_KEY>"),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        "<REDACTED_GITHUB_TOKEN>",
    ),
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        "<REDACTED_GITHUB_TOKEN>",
    ),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"), "<REDACTED_API_KEY>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "Bearer <REDACTED_TOKEN>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "<REDACTED_SLACK_TOKEN>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<REDACTED_GOOGLE_API_KEY>"),
    (
        re.compile(
            r"\b((?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp)://)"
            r"[^:\s/]+:[^@\s]+@"
        ),
        r"\1<REDACTED_CREDENTIALS>@",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
            r"password|secret)\b(\s*[:=]\s*)"
            r"([\"'])(?!<|\$|\{)[^\"'\r\n]{8,}\3"
        ),
        _replace_quoted_assignment,
    ),
    (
        re.compile(
            r"(?im)^([+-]?[ \t]*(?:export[ \t]+)?)"
            r"((?:[A-Za-z0-9_]*_)?(?:api[_-]?(?:key|secret|token)|access[_-]?token|"
            r"auth[_-]?token|refresh[_-]?token|bearer[_-]?token|bot[_-]?token|"
            r"client[_-]?secret|secret[_-]?(?:access[_-]?)?key|private[_-]?key|"
            r"signing[_-]?key|encryption[_-]?key|webhook[_-]?secret|"
            r"password|passwd|secret|token))"
            r"([ \t]*=[ \t]*)"
            r"(?!<|\$|\{|\"|')(?![a-z_]+\.[a-z_]+(?:[,\s]|$))"
            r"[^\s\"'#(),;\r\n]{8,}(?=$|\s|#)"
        ),
        _replace_env_assignment,
    ),
)


@dataclass(frozen=True)
class RedactedText:
    text: str
    count: int


def run_git(repo: Path, argv: Sequence[str], timeout: int = 60) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *argv),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"git {' '.join(argv)} failed: {detail}")
    return result.stdout


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in SENSITIVE_NAMES
        or name.startswith(".env.")
        or path.suffix.lower() in SENSITIVE_SUFFIXES
        or any(part.lower() in {".ssh", ".gnupg", "secrets"} for part in path.parts)
    )


def normalize_role(value: str, *, label: str) -> str:
    if "\0" in value:
        raise RuntimeError(f"{label} contains a null byte")
    role = " ".join(value.split())
    if not role or len(role) > MAX_ROLE_CHARS:
        raise RuntimeError(f"{label} must be 1-{MAX_ROLE_CHARS} visible characters")
    return role


def redact(text: str) -> RedactedText:
    count = 0
    current = text
    for pattern, replacement in SECRET_PATTERNS:
        current, matches = pattern.subn(replacement, current)
        count += matches
    return RedactedText(current, count)


def resolve_repo_path(repo: Path, value: str, *, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo / candidate
    if candidate.is_symlink():
        raise RuntimeError(f"refusing symlink for {label}: {candidate}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise RuntimeError(f"{label} must stay inside the repository: {resolved}") from exc
    if not resolved.is_file():
        raise RuntimeError(f"{label} is not a regular file: {resolved}")
    if is_sensitive_path(resolved.relative_to(repo)):
        raise RuntimeError(f"refusing sensitive-looking {label}: {resolved}")
    return resolved


def resolve_regular_input(value: str, *, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_symlink():
        raise RuntimeError(f"refusing symlink for {label}: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError(f"{label} is not a regular file: {resolved}")
    return resolved


def read_text(path: Path) -> RedactedText:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"refusing non-regular input: {path}")
    return redact(path.read_text(encoding="utf-8", errors="replace"))


def section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body.rstrip()}\n"


def include_repo_files(repo: Path, values: Sequence[str]) -> tuple[str, int]:
    chunks: list[str] = []
    redactions = 0
    for value in values:
        path = resolve_repo_path(repo, value, label="included file")
        content = read_text(path)
        redactions += content.count
        relative = path.relative_to(repo)
        chunks.append(f"### `{relative}`\n\n```text\n{content.text.rstrip()}\n```\n")
    return "\n".join(chunks), redactions


def exclude_pathspecs(excludes: Sequence[str]) -> tuple[str, ...]:
    if not excludes:
        return ()
    return (".", *(f":(exclude){pattern}" for pattern in excludes))


def collect_untracked(repo: Path, excludes: Sequence[str] = ()) -> tuple[str, int, list[str]]:
    all_output = run_git(repo, ("ls-files", "--others", "--exclude-standard", "-z"))
    all_files = [raw for raw in all_output.split("\0") if raw]
    if excludes:
        included_output = run_git(
            repo,
            ("ls-files", "--others", "--exclude-standard", "-z", "--", *exclude_pathspecs(excludes)),
        )
        included_set = {raw for raw in included_output.split("\0") if raw}
    else:
        included_set = set(all_files)

    chunks: list[str] = []
    redactions = 0
    omitted: list[str] = []
    for raw in all_files:
        relative = Path(raw)
        path = repo / relative
        if is_sensitive_path(relative):
            omitted.append(f"{relative} (sensitive-looking path)")
            continue
        if raw not in included_set:
            omitted.append(f"{relative} (excluded by --exclude)")
            continue
        if path.is_symlink() or not path.is_file():
            omitted.append(f"{relative} (symlink or non-regular file)")
            continue
        size = path.stat().st_size
        if size > MAX_UNTRACKED_FILE_BYTES:
            omitted.append(f"{relative} ({size} bytes; over per-file limit)")
            continue
        raw_bytes = path.read_bytes()
        if b"\0" in raw_bytes[:8192]:
            omitted.append(f"{relative} (binary)")
            continue
        content = redact(raw_bytes.decode("utf-8", errors="replace"))
        redactions += content.count
        chunks.append(f"### `{relative}` (untracked)\n\n```text\n{content.text.rstrip()}\n```\n")
    return "\n".join(chunks), redactions, omitted


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"refusing to overwrite packet: {path}")
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


def build_packet(args: argparse.Namespace) -> tuple[str, int, list[str]]:
    repo = Path(args.repo).resolve(strict=True)
    if not repo.is_dir():
        raise RuntimeError(f"repository path is not a directory: {repo}")
    plan_path = resolve_repo_path(repo, args.plan, label="plan")
    prompt = read_text(PROMPTS[args.mode])
    plan = read_text(plan_path)
    redactions = prompt.count + plan.count
    omissions: list[str] = []

    parts = [
        prompt.text.rstrip(),
        section(
            "Review metadata",
            "\n".join(
                (
                    f"- Mode: `{args.mode}`",
                    f"- Round: `{args.round}`",
                    f"- Repository: `{repo}`",
                    f"- Plan: `{plan_path.relative_to(repo)}`",
                    f"- Primary operator: `{args.primary}`",
                    f"- Independent reviewer: `{args.reviewer}` (advisory only; does not implement)",
                )
            ),
        ),
        section("Locked plan", plan.text),
    ]

    included, included_redactions = include_repo_files(repo, args.include)
    redactions += included_redactions
    if included:
        parts.append(section("Selected repository evidence", included))

    if args.notes:
        notes_path = resolve_regular_input(args.notes, label="notes")
        if is_sensitive_path(notes_path):
            raise RuntimeError(f"refusing sensitive-looking notes path: {notes_path}")
        notes = read_text(notes_path)
        redactions += notes.count
        parts.append(section("Primary operator dispositions and notes", notes.text))

    if args.mode == "code":
        if not args.base or args.base.startswith("-"):
            raise RuntimeError("code mode requires a safe --base commit/ref")
        resolved_base = run_git(repo, ("rev-parse", "--verify", f"{args.base}^{{commit}}"))
        base_commit = resolved_base.strip()
        changed_names = run_git(repo, ("diff", "--name-only", "-z", base_commit, "--"))
        all_changed = [name for name in changed_names.split("\0") if name]
        sensitive_changes = [
            name
            for name in all_changed
            if is_sensitive_path(Path(name))
        ]
        if sensitive_changes:
            raise RuntimeError(
                "refusing code packet with sensitive-looking changed paths: "
                + ", ".join(sensitive_changes)
            )
        pathspecs = exclude_pathspecs(args.exclude)
        if pathspecs:
            included_names = run_git(
                repo, ("diff", "--name-only", "-z", base_commit, "--", *pathspecs)
            )
            included_set = {name for name in included_names.split("\0") if name}
            for name in all_changed:
                if name not in included_set:
                    omissions.append(f"{name} (excluded by --exclude)")
        status = run_git(repo, ("status", "--short"))
        stat = run_git(repo, ("diff", "--stat", base_commit, "--", *pathspecs), timeout=120)
        diff = run_git(
            repo,
            (
                "diff",
                "--no-ext-diff",
                "--find-renames",
                "--find-copies",
                f"--unified={args.unified}",
                base_commit,
                "--",
                *pathspecs,
            ),
            timeout=180,
        )
        clean_diff = redact(diff)
        redactions += clean_diff.count
        untracked, untracked_redactions, untracked_omitted = collect_untracked(
            repo, excludes=args.exclude
        )
        redactions += untracked_redactions
        omissions.extend(untracked_omitted)
        parts.extend(
            (
                section("Implementation baseline", f"`{base_commit}`"),
                section("Git status", f"```text\n{status.rstrip()}\n```"),
                section("Diff stat", f"```text\n{stat.rstrip()}\n```"),
                section("Implementation diff", f"```diff\n{clean_diff.text.rstrip()}\n```"),
            )
        )
        if untracked:
            parts.append(section("Untracked implementation files", untracked))
        if args.proof_file:
            proof_path = resolve_regular_input(args.proof_file, label="proof file")
            if is_sensitive_path(proof_path):
                raise RuntimeError(f"refusing sensitive-looking proof path: {proof_path}")
            proof = read_text(proof_path)
            redactions += proof.count
            parts.append(section("Primary proof output", f"```text\n{proof.text.rstrip()}\n```"))

    if omissions:
        parts.append(section("Omitted from packet", "\n".join(f"- {item}" for item in omissions)))
    parts.append(
        section(
            "Packet boundary",
            f"Automatic secret redactions applied: `{redactions}`. "
            "Omitted artifacts are not evidence of correctness; flag any omission that blocks review.",
        )
    )
    return "\n".join(parts).rstrip() + "\n", redactions, omissions


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a bounded and redacted plan/code review packet for an independent reviewer."
    )
    parser.add_argument("mode", choices=("plan", "code"))
    parser.add_argument("--repo", default=".", help="target repository root")
    parser.add_argument("--plan", default="PLAN.md", help="plan path inside repo")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--output", required=True, help="new packet path")
    parser.add_argument(
        "--primary",
        default=DEFAULT_PRIMARY,
        help="resolved primary-operator identity from the current host",
    )
    parser.add_argument(
        "--reviewer",
        default=DEFAULT_REVIEWER,
        help="resolved independent-reviewer identity; default is the unpinned CLI model",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="task-relevant text file inside repo; repeat as needed",
    )
    parser.add_argument("--notes", help="operator disposition/notes file")
    parser.add_argument("--base", help="base commit/ref; required for code mode")
    parser.add_argument("--proof-file", help="captured proof-test output")
    parser.add_argument(
        "--unified",
        type=int,
        default=None,
        help=f"diff context lines for code mode ({MIN_UNIFIED_CONTEXT}..{MAX_UNIFIED_CONTEXT}; default {DEFAULT_UNIFIED_CONTEXT})",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="glob pathspec to exclude from code-mode diff and untracked files; repeat as needed",
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    if args.round < 1:
        parser.error("--round must be positive")
    if args.max_bytes < 100_000 or args.max_bytes > DEFAULT_MAX_BYTES:
        parser.error(f"--max-bytes must be between 100000 and {DEFAULT_MAX_BYTES}")
    if args.mode == "plan" and (
        args.base or args.proof_file or args.exclude or args.unified is not None
    ):
        parser.error("--base, --proof-file, --unified, and --exclude apply only to code mode")
    if args.unified is None:
        args.unified = DEFAULT_UNIFIED_CONTEXT
    elif args.unified < MIN_UNIFIED_CONTEXT or args.unified > MAX_UNIFIED_CONTEXT:
        parser.error(f"--unified must be between {MIN_UNIFIED_CONTEXT} and {MAX_UNIFIED_CONTEXT}")
    for pattern in args.exclude:
        if not pattern.strip() or "\0" in pattern or pattern.startswith(":"):
            parser.error(f"invalid --exclude glob: {pattern!r}")
    try:
        args.primary = normalize_role(args.primary, label="--primary")
        args.reviewer = normalize_role(args.reviewer, label="--reviewer")
    except RuntimeError as exc:
        parser.error(str(exc))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        packet, redactions, omissions = build_packet(args)
        size = len(packet.encode("utf-8"))
        if size > args.max_bytes:
            raise RuntimeError(
                f"packet is {size} bytes, above {args.max_bytes}; split the review by component"
            )
        output_candidate = Path(args.output)
        if output_candidate.is_symlink():
            raise RuntimeError(f"refusing symlink for output: {output_candidate}")
        output = output_candidate.resolve(strict=False)
        atomic_write(output, packet)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Packet build failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "bytes": size,
                "omitted_count": len(omissions),
                "output": str(output),
                "redactions": redactions,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
