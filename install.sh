#!/usr/bin/env bash
set -euo pipefail

SKILL_NAME="clodex-loop"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CODEX_ROOT="${CODEX_HOME:-$HOME/.codex}"
OPENCODE_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
CLAUDE_SKILLS_DIR="$HOME/.claude/skills"
SKIP_REASON="Claude Code cannot use Claude Code as an independent reviewer; use model-loop with a non-Anthropic rival CLI instead."

HOST_NAMES=(
  "Codex"
  "Cursor"
  "Gemini CLI"
  "Antigravity (AGY)"
  "GitHub Copilot"
  "Pi"
  "OpenCode"
  "Shared agents"
)

HOST_ROOTS=(
  "$CODEX_ROOT"
  "$HOME/.cursor"
  "$HOME/.gemini"
  "$HOME/.gemini/antigravity"
  "$HOME/.copilot"
  "$HOME/.pi"
  "$OPENCODE_ROOT"
  "$HOME/.agents"
)

HOST_SKILLS=(
  "$CODEX_ROOT/skills"
  "$HOME/.cursor/skills"
  "$HOME/.gemini/skills"
  "$HOME/.gemini/antigravity/skills"
  "$HOME/.copilot/skills"
  "$HOME/.pi/agent/skills"
  "$OPENCODE_ROOT/skills"
  "$HOME/.agents/skills"
)

usage() {
  cat <<'EOF'
Usage: ./install.sh [--list | --copy | --uninstall | --help]

Install, inspect, or remove the clodex-loop skill across supported coding-agent
harnesses (Codex, Cursor, Gemini CLI, Antigravity/AGY, Copilot, Pi, OpenCode,
and ~/.agents/skills).

Options:
  (default)    Symlink clodex-loop into detected host skill directories
  --copy       Copy skill files instead of creating symlinks
  --list       Show supported target directories and current install status
  --uninstall  Remove clodex-loop from host skill directories
  -h, --help   Show this help message

Note:
  ~/.claude/skills is always skipped because Claude Code cannot act as an
  independent reviewer for itself.
EOF
}

print_claude_skip() {
  printf '  [SKIP] %-18s %s/clodex-loop (%s)\n' "Claude Code" "$CLAUDE_SKILLS_DIR" "$SKIP_REASON"
}

target_status() {
  local skills_dir="$1"
  local target="$skills_dir/$SKILL_NAME"
  if [ -L "$target" ]; then
    local link_dest
    link_dest="$(readlink "$target")"
    printf 'installed (symlink -> %s)' "$link_dest"
  elif [ -d "$target" ]; then
    printf 'installed (copy)'
  elif [ -e "$target" ]; then
    printf 'blocked (non-directory file exists)'
  else
    printf 'not installed'
  fi
}

list_targets() {
  local i root skills_dir state
  printf 'Supported host skill directories (source: %s):\n' "$SCRIPT_DIR"
  for i in "${!HOST_NAMES[@]}"; do
    root="${HOST_ROOTS[$i]}"
    skills_dir="${HOST_SKILLS[$i]}"
    state="$(target_status "$skills_dir")"
    if [ -d "$root" ] || [ -e "$skills_dir/$SKILL_NAME" ] || [ -L "$skills_dir/$SKILL_NAME" ]; then
      printf '  [HOST] %-18s %s/%s [%s]\n' "${HOST_NAMES[$i]}" "$skills_dir" "$SKILL_NAME" "$state"
    else
      printf '  [ABSENT] %-16s %s/%s [%s; host dir missing]\n' "${HOST_NAMES[$i]}" "$skills_dir" "$SKILL_NAME" "$state"
    fi
  done
  print_claude_skip
}

copy_skill_tree() {
  local dest="$1"
  mkdir -p "$dest"
  cp "$SCRIPT_DIR/SKILL.md" "$SCRIPT_DIR/LICENSE" "$SCRIPT_DIR/README.md" "$SCRIPT_DIR/install.sh" "$dest/"
  cp -R "$SCRIPT_DIR/agents" "$SCRIPT_DIR/references" "$SCRIPT_DIR/scripts" "$dest/"
  rm -rf "$dest/scripts/__pycache__"
  find "$dest" -type f \( -name '*.pyc' -o -name '.DS_Store' \) -delete
}

install_one() {
  local name="$1"
  local skills_dir="$2"
  local copy_mode="$3"
  local target="$skills_dir/$SKILL_NAME"

  if [ "$target" = "$SCRIPT_DIR" ]; then
    printf '  [OK]   %-18s %s (already in place)\n' "$name" "$target"
    return 0
  fi

  mkdir -p "$skills_dir"

  if [ -L "$target" ]; then
    rm -f "$target"
  elif [ -d "$target" ]; then
    if [ ! -f "$target/SKILL.md" ]; then
      printf '  [WARN] %-18s %s exists without SKILL.md; skipping\n' "$name" "$target" >&2
      return 1
    fi
    rm -rf "$target"
  elif [ -e "$target" ]; then
    printf '  [WARN] %-18s %s exists and is not a skill dir; skipping\n' "$name" "$target" >&2
    return 1
  fi

  if [ "$copy_mode" = "true" ]; then
    copy_skill_tree "$target"
    printf '  [COPY] %-18s %s\n' "$name" "$target"
  else
    ln -s "$SCRIPT_DIR" "$target"
    printf '  [LINK] %-18s %s -> %s\n' "$name" "$target" "$SCRIPT_DIR"
  fi
}

install_targets() {
  local copy_mode="$1"
  local i installed_any=0 root
  for i in "${!HOST_NAMES[@]}"; do
    root="${HOST_ROOTS[$i]}"
    if [ -d "$root" ]; then
      install_one "${HOST_NAMES[$i]}" "${HOST_SKILLS[$i]}" "$copy_mode"
      installed_any=1
    fi
  done

  if [ "$installed_any" -eq 0 ]; then
    install_one "Shared agents" "$HOME/.agents/skills" "$copy_mode"
  fi

  print_claude_skip
}

uninstall_targets() {
  local i skills_dir target removed=0
  for i in "${!HOST_NAMES[@]}"; do
    skills_dir="${HOST_SKILLS[$i]}"
    target="$skills_dir/$SKILL_NAME"
    if [ "$target" = "$SCRIPT_DIR" ]; then
      printf '  [KEEP] %-18s %s (installer source checkout)\n' "${HOST_NAMES[$i]}" "$target"
      continue
    fi
    if [ -L "$target" ]; then
      rm -f "$target"
      printf '  [DEL]  %-18s %s (symlink)\n' "${HOST_NAMES[$i]}" "$target"
      removed=$((removed + 1))
    elif [ -d "$target" ]; then
      if [ ! -f "$target/SKILL.md" ]; then
        printf '  [WARN] %-18s %s exists without SKILL.md; skipping\n' "${HOST_NAMES[$i]}" "$target" >&2
        continue
      fi
      rm -rf "$target"
      printf '  [DEL]  %-18s %s (copy)\n' "${HOST_NAMES[$i]}" "$target"
      removed=$((removed + 1))
    fi
  done
  if [ "$removed" -eq 0 ]; then
    printf 'No installed %s entries found to remove.\n' "$SKILL_NAME"
  fi
  print_claude_skip
}

main() {
  local mode="install"
  local copy_mode="false"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --list)
        mode="list"
        ;;
      --copy)
        copy_mode="true"
        ;;
      --uninstall)
        mode="uninstall"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        printf 'Unknown option: %s\n\n' "$1" >&2
        usage >&2
        exit 2
        ;;
    esac
    shift
  done

  if [ "$mode" = "list" ] && [ "$copy_mode" = "true" ]; then
    printf 'Cannot combine --list with --copy\n' >&2
    exit 2
  fi
  if [ "$mode" = "uninstall" ] && [ "$copy_mode" = "true" ]; then
    printf 'Cannot combine --uninstall with --copy\n' >&2
    exit 2
  fi

  case "$mode" in
    list)
      list_targets
      ;;
    install)
      install_targets "$copy_mode"
      ;;
    uninstall)
      uninstall_targets
      ;;
  esac
}

main "$@"
