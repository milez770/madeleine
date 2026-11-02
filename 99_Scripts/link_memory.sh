#!/usr/bin/env bash
# Ensure Claude Code's per-project memory dir symlinks to the vault's canonical
# memory store, so MEMORY.md auto-loads at session start on ANY device.
#
# Why this exists: the symlink lives under ~/.claude/projects/<slug>/memory, which
# is NOT in the vault git repo, so it does not travel with a `git pull`. Without it,
# a fresh device syncs the memory *content* but never auto-loads it. This script
# recreates the link from $CLAUDE_PROJECT_DIR at runtime, so the <slug> matches
# whatever absolute path the vault lives at on this device.
#
# Idempotent and non-destructive: never clobbers a real (non-symlink) memory dir.
set -euo pipefail

VAULT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
CANON="$VAULT/99_Config/claude_memory"
[ -d "$CANON" ] || exit 0

# Claude Code derives the project dir name by replacing every "/" with "-".
SLUG="$(printf '%s' "$VAULT" | sed 's:/:-:g')"
LINK_DIR="$HOME/.claude/projects/$SLUG"
LINK="$LINK_DIR/memory"

# Already correct → nothing to do.
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$CANON" ]; then
  exit 0
fi

# A real dir/file sits there (not our symlink) → leave it, warn, do not destroy data.
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "link_memory: $LINK exists and is not a symlink; leaving untouched" >&2
  exit 0
fi

mkdir -p "$LINK_DIR"
ln -sfn "$CANON" "$LINK"
