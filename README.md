# Clodex Loop

The current agent builds. An independent reviewer from a different lineage attacks the plan and the diff.

Clodex Loop is a bounded workflow for consequential software changes. The current host agent remains the primary operator: it researches, resolves decisions, writes the plan, implements, and runs proof. Claude Code receives bounded review packets and acts only as an independent adversarial reviewer, using its CLI default model unless the user names an exact ID.

The core invariant is simple:

> The model that creates an artifact does not grade it.

Roles are resolved at runtime, so new frontier releases do not require editing this skill. Do not pin remembered product SKUs. If the current agent is already Anthropic-lineage, stop: the shipped reviewer adapter cannot provide independent review.

This project reverses the roles in Chase AI's MIT-licensed [claudex-loop](https://github.com/chaseai-yt/claudex-loop). The upstream notices are preserved in [LICENSE](LICENSE).

## What the loop does

1. The primary operator inspects the repository and records sourced assumptions.
2. The primary operator and the user resolve load-bearing decisions and lock `PLAN.md`.
3. An independent reviewer adversarially reviews that plan from a redacted, task-scoped packet.
4. The user approves the converged plan.
5. The primary operator implements and runs the specified proof commands.
6. A fresh reviewer session reviews the completed diff against the locked plan.
7. The primary operator adjudicates findings, reruns proof after accepted fixes, and reports unresolved risks.
8. The user retains the final gate for commits, pushes, deployments, releases, submissions, or other consequential actions.

The reviewer's output is advisory. It cannot expand the user's scope or grant new authority.

## Requirements

- A coding agent with local filesystem and shell access
- Python 3.10 or newer
- Git
- An installed and authenticated [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) CLI, used as the shipped independent-reviewer adapter

Plain browser chat without local tools cannot orchestrate the reviewer CLI.

## Install

For a shared installation across supported agent harnesses, use the skills installer:

```bash
npx skills add https://github.com/flowmar47/clodex-loop --global --all
```

For a manual installation, clone the repository into the current agent's skill directory. A Codex example:

```bash
git clone https://github.com/flowmar47/clodex-loop.git ~/.codex/skills/clodex-loop
```

If the current task does not discover a newly installed skill, start a new task and invoke the skill there.

Before first use, verify the local Claude Code integration:

```bash
cd ~/.codex/skills/clodex-loop
python3 scripts/claude_review.py doctor
```

The doctor reports a sanitized CLI basename plus authentication-method, subscription, and required-flag status. It does not print credentials or the resolved executable path. The reviewer model stays at the CLI default unless the user overrides it.

Run the local regression suite without invoking the reviewer:

```bash
python3 scripts/test_clodex.py -v
```

## Use

Ask the current coding agent:

```text
Use $clodex-loop to plan and implement this change, with an independent reviewer attacking the plan and diff.
```

The skill will resolve live roles, then show its assumptions, decision map, review boundary, and human gates before proceeding. Defaults are five plan-review rounds and two code-review rounds.

## Review boundary

Packet-only review is the default:

- Packet-only commands request an empty Claude tool set with `--tools ""` under safe mode; `doctor` records the installed CLI version and verifies that the required flag is present, so upgrades can be rechecked.
- Chrome, MCP, slash commands, and project customizations are disabled by the runner.
- Plan and code packets are capped below Claude Code's stdin limit.
- The plan and files passed with `--include` must remain inside the target repository. Direct symlink inputs are rejected for plan, include, notes, proof, packet, output, and state paths; notes and proof also reject sensitive-looking paths.
- Common private-key and credential forms are redacted.
- Session UUIDs are captured exactly; a phase resumes only its recorded session.
- Plan review and code review always use separate reviewer sessions.
- Outputs and state are preserved without overwriting prior evidence.
- Structured reviewer errors return exit `4`, preserve reviewer text behind a visible `PARTIAL OUTPUT` marker, and report `verdict: null`; an embedded approval line never overrides that failure state.

Automatic redaction is a backstop, not a guarantee. Inspect every packet before sending it. Do not include secrets, credentials, personal data, `.env` files, or unrelated proprietary material. Review packets are sent to Anthropic through Claude Code.

An optional repository-read mode requests only `Read`, `Glob`, and `Grep` through both `--tools` and `--allowedTools`. It requires explicit approval because it can disclose repository content beyond the packet. No Bash, Edit, Write, browser, or MCP capability is requested, but this CLI-enforced boundary is not an operating-system sandbox.

## Repository layout

```text
SKILL.md                              Workflow and safety contract
agents/openai.yaml                    Codex skill metadata
references/plan-review-prompt.md      Independent plan-review contract
references/code-review-prompt.md      Independent code-review contract
scripts/build_review_packet.py        Bounded packet builder and redactor
scripts/claude_review.py              Claude Code doctor/session runner
scripts/test_clodex.py                Standard-library regression suite
.github/workflows/test.yml            Push and pull-request regression check
```

Runtime dependencies are Python's standard library. The scripts refuse unsafe path forms, oversized packets, missing exact verdicts, unexpected session IDs, and evidence overwrites.

## License and provenance

MIT. Clodex Loop is a role-reversed adaptation of [chaseai-yt/claudex-loop](https://github.com/chaseai-yt/claudex-loop). See [LICENSE](LICENSE) for the preserved copyright and permission notices.
