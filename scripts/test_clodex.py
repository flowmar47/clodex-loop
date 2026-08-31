#!/usr/bin/env python3
"""Regression tests for clodex-loop without live Claude calls."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import build_review_packet  # noqa: E402
import claude_review  # noqa: E402


class StateValidationTests(unittest.TestCase):
    def valid_state(self, repo: Path) -> dict[str, object]:
        return {
            "repo": str(repo.resolve()),
            "repo_read": False,
            "rounds": 1,
            "model_argument": None,
            "max_budget_usd": None,
            "started_at": "2026-08-31T12:00:00+00:00",
            "session_id": "9ca1b05c-19a7-4cf5-843d-6bf0bfcb78a8",
        }

    def test_load_state_accepts_runner_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state.json"
            path.write_text(json.dumps(self.valid_state(root)), encoding="utf-8")
            self.assertEqual(claude_review.load_state(path)["rounds"], 1)

    def test_load_state_rejects_malformed_resume_fields(self) -> None:
        malformed = {
            "repo": "relative/repo",
            "repo_read": 1,
            "rounds": True,
            "model_argument": "",
            "max_budget_usd": float("inf"),
            "started_at": "2026-08-31T12:00:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for field, value in malformed.items():
                with self.subTest(field=field):
                    state = self.valid_state(root)
                    state[field] = value
                    path = root / f"{field}.json"
                    path.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, field):
                        claude_review.load_state(path)

    def test_malformed_resume_uses_one_line_failure_without_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = root / "packet.md"
            packet.write_text("review me", encoding="utf-8")
            malformed = (
                ("max_budget_usd", 10**999),
                ("model_argument", "model\0--dangerous"),
            )
            for field, value in malformed:
                with self.subTest(field=field):
                    state = self.valid_state(root)
                    state[field] = value
                    state_path = root / f"{field}.state.json"
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    with (
                        mock.patch.object(
                            claude_review, "find_claude", return_value="/fake/claude"
                        ),
                        mock.patch.object(claude_review, "run") as spawned,
                        mock.patch.object(
                            sys, "stderr", new_callable=io.StringIO
                        ) as stderr,
                    ):
                        rc = claude_review.main(
                            [
                                "resume",
                                "--repo",
                                str(root),
                                "--packet",
                                str(packet),
                                "--output",
                                str(root / f"{field}.out.md"),
                                "--state",
                                str(state_path),
                            ]
                        )
                    self.assertEqual(rc, 1)
                    spawned.assert_not_called()
                    self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                    self.assertNotIn("Traceback", stderr.getvalue())
                    self.assertIn(field, stderr.getvalue())


class ReviewContractTests(unittest.TestCase):
    def test_verdict_must_be_the_exact_final_line(self) -> None:
        self.assertEqual(
            claude_review.verdict_from_result("finding\nVERDICT: APPROVED\n```\n"),
            "VERDICT: APPROVED",
        )
        self.assertIsNone(
            claude_review.verdict_from_result("VERDICT: APPROVED\nmore text")
        )
        self.assertIsNone(claude_review.verdict_from_result("approved"))

    def test_packet_only_requests_zero_tools(self) -> None:
        command = claude_review.build_command(
            executable="claude",
            session_id="session",
            resume=False,
            repo_read=False,
            model=None,
            max_budget_usd=None,
        )
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", command)

    def test_repo_read_requests_only_read_glob_and_grep(self) -> None:
        command = claude_review.build_command(
            executable="claude",
            session_id="session",
            resume=True,
            repo_read=True,
            model=None,
            max_budget_usd=None,
        )
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(
            command[command.index("--allowedTools") + 1], "Read,Glob,Grep"
        )
        self.assertEqual(command[command.index("--resume") + 1], "session")

    def test_structured_error_cannot_approve(self) -> None:
        session_id = uuid.UUID("9ca1b05c-19a7-4cf5-843d-6bf0bfcb78a8")

        def fake_run(argv, **_kwargs):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "2.1.0\n", "")
            payload = {
                "type": "result",
                "session_id": str(session_id),
                "is_error": True,
                "result": "partial critique\nVERDICT: APPROVED",
                "terminal_reason": "transport failed",
            }
            return subprocess.CompletedProcess(argv, 1, json.dumps(payload), "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = root / "packet.md"
            packet.write_text("review me", encoding="utf-8")
            output = root / "out.md"
            state = root / "state.json"
            with (
                mock.patch.object(claude_review, "find_claude", return_value="/fake/claude"),
                mock.patch.object(claude_review, "run", side_effect=fake_run),
                mock.patch.object(claude_review.uuid, "uuid4", return_value=session_id),
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
                mock.patch.object(sys, "stderr", new_callable=io.StringIO),
            ):
                rc = claude_review.main(
                    [
                        "start",
                        "--repo",
                        str(root),
                        "--packet",
                        str(packet),
                        "--output",
                        str(output),
                        "--state",
                        str(state),
                    ]
                )
            summary = json.loads(stdout.getvalue())
            self.assertEqual(rc, 4)
            self.assertIsNone(summary["verdict"])
            self.assertEqual(summary["reported_verdict"], "VERDICT: APPROVED")
            self.assertIn("PARTIAL OUTPUT", output.read_text(encoding="utf-8"))


class PacketSafetyTests(unittest.TestCase):
    def test_redacts_tokens_and_secret_assignments(self) -> None:
        source = (
            "token=\"ghp_abcdefghijklmnopqrstuvwxyz123456\"\n"
            "password='correct horse battery staple'\n"
        )
        redacted = build_review_packet.redact(source)
        self.assertEqual(redacted.count, 2)
        self.assertNotIn("ghp_", redacted.text)
        self.assertNotIn("correct horse", redacted.text)

    def test_repo_path_must_stay_contained_and_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            inside = root / "inside.txt"
            inside.write_text("ok", encoding="utf-8")
            outside = Path(tmp) / "outside.txt"
            outside.write_text("no", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(inside)
            self.assertEqual(
                build_review_packet.resolve_repo_path(
                    root.resolve(), "inside.txt", label="included file"
                ),
                inside.resolve(),
            )
            with self.assertRaisesRegex(RuntimeError, "inside the repository"):
                build_review_packet.resolve_repo_path(
                    root.resolve(), "../outside.txt", label="included file"
                )
            with self.assertRaisesRegex(RuntimeError, "refusing symlink"):
                build_review_packet.resolve_repo_path(
                    root.resolve(), "link.txt", label="included file"
                )


if __name__ == "__main__":
    unittest.main()
