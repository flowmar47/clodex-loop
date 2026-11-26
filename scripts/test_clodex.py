#!/usr/bin/env python3
"""Regression tests for clodex-loop without live Claude calls."""

from __future__ import annotations

import io
import json
import os
import re
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
            state = claude_review.load_state(path)
            self.assertEqual(state["rounds"], 1)
            self.assertIsNone(state.get("effort_argument"))

    def test_load_state_accepts_and_validates_effort_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self.valid_state(root)
            state["effort_argument"] = "high"
            path = root / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(claude_review.load_state(path)["effort_argument"], "high")

            for bad in ("", "  ", "high\0bad", 123):
                state["effort_argument"] = bad
                bad_path = root / "bad.json"
                bad_path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "effort_argument"):
                    claude_review.load_state(bad_path)

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
        self.assertNotIn("--model", command)

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

    def test_parse_effort_values_reads_live_help_and_handles_missing_enum(self) -> None:
        help_paren = (
            "Options:\n"
            "  --effort <level>                      Effort level for the current session\n"
            "                                        (low, medium, high, xhigh, max)\n"
            "  --environment <id>                    Cloud environment\n"
        )
        self.assertEqual(
            claude_review.parse_effort_values(help_paren),
            {"low", "medium", "high", "xhigh", "max"},
        )
        help_choices = '  --effort <level>  Set effort (choices: "alpha", "beta")\n'
        self.assertEqual(
            claude_review.parse_effort_values(help_choices),
            {"alpha", "beta"},
        )
        help_unenumerated = (
            "  --effort <level>  Effort level for the current session\n"
            "                    (default: \"medium\")\n"
        )
        self.assertIsNone(claude_review.parse_effort_values(help_unenumerated))
        self.assertIsNone(claude_review.parse_effort_values("  --model <m>  Model\n"))

    def test_doctor_reports_same_product_host_and_effort_values(self) -> None:
        required_help = "\n".join(
            [
                *claude_review.REQUIRED_FLAGS,
                "  --effort <level>  Effort level",
                "                    (low, medium, high)",
            ]
        )

        def fake_run(argv, **_kwargs):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "2.1.0\n", "")
            if "--help" in argv:
                return subprocess.CompletedProcess(argv, 0, required_help, "")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"loggedIn": True, "authMethod": "oauth"}), ""
            )

        with (
            mock.patch.object(claude_review, "find_claude", return_value="/fake/claude"),
            mock.patch.object(claude_review, "run", side_effect=fake_run),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
        ):
            rc = claude_review.main(["doctor"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(report["ok"])
        self.assertFalse(report["same_product_host"])
        self.assertEqual(report["effort_values"], ["high", "low", "medium"])
        self.assertNotIn("note", report)

        for env_key in ("CLAUDECODE", "CLAUDE_CODE"):
            with (
                self.subTest(env_key=env_key),
                mock.patch.object(claude_review, "find_claude", return_value="/fake/claude"),
                mock.patch.object(claude_review, "run", side_effect=fake_run),
                mock.patch.dict(os.environ, {env_key: "1"}, clear=True),
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
            ):
                rc = claude_review.main(["doctor"])
                report = json.loads(stdout.getvalue())
                self.assertEqual(rc, 1)
                self.assertFalse(report["ok"])
                self.assertTrue(report["same_product_host"])
                self.assertEqual(
                    report["note"],
                    "Host is Claude Code; use model-loop with a non-Anthropic rival CLI",
                )

    def test_effort_start_validates_against_help_and_persists_through_resume(self) -> None:
        session_id = uuid.UUID("9ca1b05c-19a7-4cf5-843d-6bf0bfcb78a8")
        commands_seen: list[list[str]] = []
        help_text = "  --effort <level>  Effort level (low, medium, high)\n"

        def fake_run(argv, **_kwargs):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "2.1.0\n", "")
            if "--help" in argv:
                return subprocess.CompletedProcess(argv, 0, help_text, "")
            commands_seen.append(list(argv))
            payload = {
                "type": "result",
                "session_id": str(session_id),
                "is_error": False,
                "result": "ok\nVERDICT: APPROVED",
                "total_cost_usd": 0.01,
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet1 = root / "p1.md"
            packet2 = root / "p2.md"
            packet1.write_text("round 1", encoding="utf-8")
            packet2.write_text("round 2", encoding="utf-8")
            state = root / "state.json"

            with (
                mock.patch.object(claude_review, "find_claude", return_value="/fake/claude"),
                mock.patch.object(claude_review, "run", side_effect=fake_run),
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
            ):
                bad_rc = claude_review.main(
                    [
                        "start",
                        "--repo",
                        str(root),
                        "--packet",
                        str(packet1),
                        "--output",
                        str(root / "bad.md"),
                        "--state",
                        str(state),
                        "--effort",
                        "extreme",
                    ]
                )
            self.assertEqual(bad_rc, 1)
            self.assertIn("unsupported --effort", stderr.getvalue())
            self.assertEqual(commands_seen, [])

            with (
                mock.patch.object(claude_review, "find_claude", return_value="/fake/claude"),
                mock.patch.object(claude_review, "run", side_effect=fake_run),
                mock.patch.object(claude_review.uuid, "uuid4", return_value=session_id),
                mock.patch.object(sys, "stdout", new_callable=io.StringIO),
            ):
                rc1 = claude_review.main(
                    [
                        "start",
                        "--repo",
                        str(root),
                        "--packet",
                        str(packet1),
                        "--output",
                        str(root / "out1.md"),
                        "--state",
                        str(state),
                        "--effort",
                        "high",
                    ]
                )
                rc2 = claude_review.main(
                    [
                        "resume",
                        "--repo",
                        str(root),
                        "--packet",
                        str(packet2),
                        "--output",
                        str(root / "out2.md"),
                        "--state",
                        str(state),
                    ]
                )
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)
            self.assertEqual(len(commands_seen), 2)
            for cmd in commands_seen:
                self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(saved["effort_argument"], "high")
            self.assertEqual(saved["rounds"], 2)

            with mock.patch.object(sys, "stdout", new_callable=io.StringIO) as status_out:
                status_rc = claude_review.main(["status", "--state", str(state)])
            self.assertEqual(status_rc, 0)
            status_json = json.loads(status_out.getvalue())
            self.assertEqual(
                set(status_json),
                {
                    "session_id",
                    "repo",
                    "rounds",
                    "repo_read",
                    "model_argument",
                    "effort_argument",
                    "started_at",
                    "last_review_at",
                    "last_packet_sha256",
                    "claude_version",
                },
            )
            self.assertEqual(status_json["effort_argument"], "high")
            self.assertEqual(status_json["rounds"], 2)

    def test_status_rejects_missing_or_symlink_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.json"
            real_state = root / "real.json"
            real_state.write_text("{}", encoding="utf-8")
            link_state = root / "link.json"
            link_state.symlink_to(real_state)
            for target in (missing, link_state):
                with (
                    self.subTest(target=target.name),
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                ):
                    rc = claude_review.main(["status", "--state", str(target)])
                    self.assertEqual(rc, 1)
                    self.assertEqual(len(stderr.getvalue().splitlines()), 1)


class RoleResolutionTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent
    PINNED_SKU = re.compile(
        r"\b(?:gpt-[0-9]|o[1-9]|claude[- ]?(?:opus|sonnet|haiku)[- ]?[0-9]"
        r"|gemini[- ]?[0-9]|grok[- ]?[0-9])",
        re.IGNORECASE,
    )

    def write_plan_repo(self, tmp: str) -> Path:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / "PLAN.md").write_text("# Plan\n\nShip it.\n", encoding="utf-8")
        return repo

    def build_plan_packet(self, repo: Path, extra: list[str] | None = None) -> str:
        output = repo / "packet.md"
        argv = [
            "plan",
            "--repo",
            str(repo),
            "--plan",
            "PLAN.md",
            "--output",
            str(output),
        ]
        if extra:
            argv.extend(extra)
        with mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            self.assertEqual(build_review_packet.main(argv), 0)
        return output.read_text(encoding="utf-8")

    def test_packet_records_resolved_roles_instead_of_vendor_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.write_plan_repo(tmp)
            packet = self.build_plan_packet(
                repo,
                [
                    "--primary",
                    "Cursor host-default",
                    "--reviewer",
                    "Claude Code CLI default (unpinned)",
                ],
            )
        self.assertIn("Primary operator: `Cursor host-default`", packet)
        self.assertIn(
            "Independent reviewer: `Claude Code CLI default (unpinned)`", packet
        )
        self.assertNotIn("Codex/ChatGPT", packet)
        self.assertNotIn("Claude is reviewer only", packet)

    def test_default_packet_stays_unpinned_and_host_agnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.write_plan_repo(tmp)
            packet = self.build_plan_packet(repo)
        self.assertIn("Primary operator: `current host agent`", packet)
        self.assertIn(
            "Independent reviewer: `Claude Code CLI default (unpinned)`", packet
        )
        self.assertNotIn("Codex/ChatGPT", packet)
        self.assertIsNone(self.PINNED_SKU.search(packet))

    def test_review_contracts_do_not_name_a_vendor_as_author(self) -> None:
        for name in ("plan-review-prompt.md", "code-review-prompt.md"):
            text = (self.ROOT / "references" / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotIn("OpenAI Codex or ChatGPT", text)
                self.assertIn("different lineage", text.lower())
                self.assertIsNone(self.PINNED_SKU.search(text))

    def test_skill_does_not_pin_frontier_skus(self) -> None:
        skill = (self.ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIsNone(self.PINNED_SKU.search(skill))
        self.assertIn("CLI default, unpinned", skill)
        self.assertIn("current host agent", skill.lower())
        self.assertIn("lineage", skill.lower())
        self.assertNotRegex(
            skill,
            r"Codex or ChatGPT performs recon",
        )

    def test_skill_classifies_lineage_without_sku_lists(self) -> None:
        skill = (self.ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        for name, text in (("SKILL.md", skill), ("README.md", readme)):
            with self.subTest(name=name):
                self.assertIsNone(self.PINNED_SKU.search(text))
                self.assertNotRegex(
                    text,
                    r"(?i)match(?:ing)? (?:an? )?(?:id|name|sku).*(?:opus|sonnet|haiku)",
                )
                self.assertIn("provider", text.lower())
        self.assertIn("Skip unless this loop earns its keep", skill)
        self.assertIn("remembered product names", skill.lower())
        self.assertIn("model-loop", skill)

    def test_blank_role_labels_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.write_plan_repo(tmp)
            argv = [
                "plan",
                "--repo",
                str(repo),
                "--plan",
                "PLAN.md",
                "--output",
                str(repo / "packet.md"),
                "--primary",
                "   ",
            ]
            with (
                mock.patch.object(sys, "stderr", new_callable=io.StringIO),
                self.assertRaises(SystemExit) as raised,
            ):
                build_review_packet.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)


class PacketSafetyTests(unittest.TestCase):
    def test_redacts_tokens_and_secret_assignments(self) -> None:
        gh_tok = "".join(["ghp", "_", "a" * 26, "123456"])
        bearer_tok = "a" * 26 + "012345=="
        slack_bot = "-".join(["xoxb", "0" * 10, "a" * 10])
        slack_user = "-".join(["xoxp", "0" * 10, "a" * 10])
        google_key = "".join(["AI", "za", "0" * 35])
        pg_pass = "test_" + "pg_pass"
        mongo_pass = "test_" + "mongo_pass"
        redis_pass = "test_" + "redis_pass"
        amqp_pass = "test_" + "amqp_pass"
        stripe_wh = "_".join(["whsec", "test", "0" * 16])
        source = (
            f'token="{gh_tok}"\n'
            "password='correct horse battery staple'\n"
            f"Authorization: Bearer {bearer_tok}\n"
            f"slack_bot={slack_bot}\n"
            f"slack_user={slack_user}\n"
            f"google_key={google_key}\n"
            "DATABASE_URL=" + "postgres" + "://" + f"appuser:{pg_pass}@db.example.com:5432/app\n"
            "MONGO_URL=" + "mongodb+srv" + "://" + f"admin:{mongo_pass}@cluster0.example.net/prod\n"
            "REDIS_URL=" + "redis" + "://" + f"default:{redis_pass}@cache.internal:6379/0\n"
            "AMQP_URL=" + "amqp" + "://" + f"guest:{amqp_pass}@mq.internal:5672/\n"
            "API_KEY=unquoted_secret_value_123\n"
            "export DB_PASSWORD=my_unquoted_password # inline comment\n"
            f"+STRIPE_SECRET_KEY={stripe_wh}\n"
        )
        redacted = build_review_packet.redact(source)
        self.assertEqual(redacted.count, 13)
        for leaked in (
            gh_tok,
            "correct horse",
            bearer_tok,
            slack_bot,
            slack_user,
            google_key,
            pg_pass,
            mongo_pass,
            redis_pass,
            amqp_pass,
            "unquoted_secret_value_123",
            "my_unquoted_password",
            stripe_wh,
        ):
            self.assertNotIn(leaked, redacted.text)
        self.assertIn("postgres://<REDACTED_CREDENTIALS>@db.example.com:5432/app", redacted.text)
        self.assertIn("mongodb+srv://<REDACTED_CREDENTIALS>@cluster0.example.net/prod", redacted.text)
        self.assertIn("export DB_PASSWORD=<REDACTED_SECRET> # inline comment", redacted.text)

    def test_redactor_preserves_placeholders_and_code_references(self) -> None:
        source = (
            "API_KEY=$MY_API_KEY\n"
            "AUTH_TOKEN=${AUTH_TOKEN}\n"
            "CLIENT_SECRET=<insert-secret-here>\n"
            "SECRET_KEY={secret_from_vault}\n"
            "PASSWORD=short\n"
            "api_key=config.api_key\n"
            "    password=hashed_password,\n"
        )
        redacted = build_review_packet.redact(source)
        self.assertEqual(redacted.count, 0)
        self.assertEqual(redacted.text, source)

    def test_sensitive_path_recognizes_expanded_names_and_suffixes(self) -> None:
        for rel in (
            ".npmrc",
            ".pypirc",
            ".netrc",
            "id_ecdsa",
            "id_dsa",
            "kubeconfig",
            "infra/prod.tfvars",
            "certs/keystore.jks",
            "certs/release.keystore",
        ):
            with self.subTest(rel=rel):
                self.assertTrue(build_review_packet.is_sensitive_path(Path(rel)))
        self.assertFalse(build_review_packet.is_sensitive_path(Path("src/app.py")))

    def test_code_mode_unified_and_exclude_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            (repo / "PLAN.md").write_text("# Plan\n\nTest.\n", encoding="utf-8")
            lines = [f"line {i}" for i in range(1, 31)]
            (repo / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (repo / "package-lock.json").write_text('{"v": 1}\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

            lines[29] = "line 30 updated"
            (repo / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (repo / "package-lock.json").write_text('{"v": 2}\n', encoding="utf-8")
            (repo / "extra.lock").write_text("untracked lock\n", encoding="utf-8")
            (repo / "notes.py").write_text("print('keep')\n", encoding="utf-8")

            packet_path = Path(tmp) / "packet.md"
            with mock.patch.object(sys, "stdout", new_callable=io.StringIO):
                rc = build_review_packet.main(
                    [
                        "code",
                        "--repo",
                        str(repo),
                        "--plan",
                        "PLAN.md",
                        "--base",
                        "HEAD",
                        "--unified",
                        "3",
                        "--exclude",
                        "package-lock.json",
                        "--exclude",
                        "*.lock",
                        "--output",
                        str(packet_path),
                    ]
                )
            self.assertEqual(rc, 0)
            packet = packet_path.read_text(encoding="utf-8")
            self.assertIn("+line 30 updated", packet)
            self.assertNotIn("line 1\n", packet)
            self.assertNotIn('{"v": 2}', packet)
            self.assertNotIn("untracked lock", packet)
            self.assertIn("print('keep')", packet)
            self.assertIn("package-lock.json (excluded by --exclude)", packet)
            self.assertIn("extra.lock (excluded by --exclude)", packet)

            for bad_argv in (
                ["code", "--base", "HEAD", "--unified", "2", "--output", str(Path(tmp) / "p2.md")],
                ["code", "--base", "HEAD", "--unified", "201", "--output", str(Path(tmp) / "p3.md")],
                ["plan", "--unified", "10", "--output", str(Path(tmp) / "p4.md")],
                ["plan", "--exclude", "*.lock", "--output", str(Path(tmp) / "p5.md")],
            ):
                with (
                    self.subTest(bad_argv=bad_argv),
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO),
                    self.assertRaises(SystemExit) as raised,
                ):
                    build_review_packet.parse_args(bad_argv)
                self.assertEqual(raised.exception.code, 2)

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
