#!/usr/bin/env python3
"""
session_to_thread.py — Claude Code · Codex CLI transcript jsonl → Inbox/_queue/

Captures CLI conversations as Threads so vault interactions accumulate as content.

Usage:
    # Auto mode (intended for SessionStart hook): capture ALL settled,
    # not-yet-captured jsonls other than --exclude-session. Sessions modified
    # within --min-idle seconds are skipped (may still be live in another
    # terminal) and get picked up on a later run.
    python3 session_to_thread.py --auto --exclude-session <current_session_id>

    # Manual: convert a specific jsonl
    python3 session_to_thread.py --input /path/to/session.jsonl

    # Codex CLI: source is ~/.codex/sessions/**/rollout-*.jsonl (one folder for all
    # projects — only sessions whose session_meta.cwd is inside the vault).
    # Stop/SessionEnd capture that session; SessionStart sweeps all.
    # Captures up to the last completed answer.
    python3 session_to_thread.py --source codex --from-hook
    python3 session_to_thread.py --source codex --auto

Filtering policy:
    - Skip headless `claude -p` sessions entirely (no file-history-snapshot marker).
      These are typically auto-summary sessions spawned by SessionEnd hooks and
      would otherwise shadow the real interactive jsonl.
    - Codex: only sessions whose session_meta.source is "cli" or "vscode" (TUI).
      `codex exec` and subagent sessions are skipped. Developer messages (skill lists, hook output) and the
      user-role wrappers Codex inserts (<environment_context> etc.) are dropped.
    - Drop tool_use, tool_result, thinking blocks
    - Drop user messages that are pure meta (system-reminder, slash command tags,
      isMeta entries, harness text such as compaction summaries; see is_injected)
    - Strip embedded meta tags inside user prose
    - Compress code fences > 5 lines to "<code: N lines elided>"

Idempotent, with growth tracking (captured_turns frontmatter). The session key is
`session_id` (`claude_session_id` is also read):
    - A capture file is never rewritten, even while it is still in the queue.
      If the session grows, only the new turns are written to a new
      "(continued)" file (same rule whether the original is in _queue or _processed).
    - not grown -> skip
    (Files are not updated in place: if an update lands while ingest is reading
     a queue file and moving it to _processed, the new turns end up in
     _processed without a summary and never return to the queue.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path(__file__).parent.parent
QUEUE_DIR = VAULT_ROOT / "00_Inbox" / "_queue"
PROCESSED_DIR = VAULT_ROOT / "00_Inbox" / "_processed"
# Claude Code stores sessions in a folder named after the project's absolute path with "/" replaced by "-". Derived at runtime so it works wherever the vault lives.
TRANSCRIPT_DIR = Path.home() / ".claude" / "projects" / str(VAULT_ROOT.resolve()).replace("/", "-")

CODEX_SESSIONS_DIR = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"

CODE_FENCE_THRESHOLD = 5  # lines inside fence above which to elide

# Wrappers Codex inserts with the user role — not prompts typed by a person
CODEX_WRAPPER_TAGS = {
    "environment_context", "codex_internal_context", "user_instructions",
    "skills_instructions", "permissions", "collaboration_mode",
    "multi_agent_role", "multi_agent_mode", "turn_aborted", "INSTRUCTIONS",
    "recommended_plugins",
}
_LEAD_TAG_RE = re.compile(r"^\s*<([A-Za-z_][\w.-]*)[\s>/]")
# Codex inserts project instructions (AGENTS.md) as a user-role message
_CODEX_PROJECT_DOC_RE = re.compile(r"^\s*# AGENTS\.md instructions for ")

META_TAG_PATTERNS = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.DOTALL),
    re.compile(r"<command-name>.*?</command-name>", re.DOTALL),
    re.compile(r"<command-message>.*?</command-message>", re.DOTALL),
    re.compile(r"<command-args>.*?</command-args>", re.DOTALL),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL),
    re.compile(r"<local-command-stderr>.*?</local-command-stderr>", re.DOTALL),
    re.compile(r"<user-prompt-submit-hook>.*?</user-prompt-submit-hook>", re.DOTALL),
    re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL),
]

# The one definition of harness text that sits in the user's place but was not typed
# by the user. recall_ppr.py imports it. A leading tag block ('<') is checked
# separately: capture strips tags with META_TAG_PATTERNS, while readers
# (recall_ppr.py) treat any text starting with '<' as injected.
INJECTED_PREFIXES = (
    "This session is being continued",   # compaction summary
    "Another Claude session sent",       # subagent report
    "[Image: original",                  # image attachment notice
    "Stop hook feedback:",
    "A session-scoped Stop hook is now active",
    "# Autonomous loop",
)
INJECTED_LEAD_TAG = "<"
_SLASH_ONLY_RE = re.compile(r"^/[a-z][\w:-]*\s*$")


def is_injected(text: str, tag: bool = True) -> bool:
    """Did the harness put this text in the user's place? With tag=False a leading '<' is not checked."""
    t = text.lstrip()
    if tag and t.startswith(INJECTED_LEAD_TAG):
        return True
    return t.startswith(INJECTED_PREFIXES) or bool(_SLASH_ONLY_RE.match(t))


def slugify(text: str, max_len: int = 40) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s가-힣]", "", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:max_len].strip("_") or "untitled"


def captured_index() -> dict[str, list[tuple[Path, bool, int, int | None]]]:
    """sid → [(thread path, is processed, covers_from, captured_turns|None)].
    captured_turns = total turns in the session at capture time (source of truth
    for coverage). Files without the field return None; the caller counts turns
    in the body instead."""
    idx: dict[str, list[tuple[Path, bool, int, int | None]]] = {}
    for folder in (QUEUE_DIR, PROCESSED_DIR):
        if not folder.exists():
            continue
        for f in folder.glob("*.md"):
            try:
                head = f.read_text(encoding="utf-8", errors="ignore")[:2000]
            except OSError:
                continue
            m = re.search(r"(?m)^(?:claude_)?session_id:\s*(.+)", head)
            if not m:
                continue
            sid = m.group(1).strip().strip('"')
            cap_m = re.search(r"captured_turns:\s*(\d+)", head)
            from_m = re.search(r"continues_from_turn:\s*(\d+)", head)
            idx.setdefault(sid, []).append((
                f,
                folder is PROCESSED_DIR,
                int(from_m.group(1)) if from_m else 0,
                int(cap_m.group(1)) if cap_m else None,
            ))
    return idx


def _turns_in_file(path: Path) -> int:
    """Turn count for a thread without captured_turns (counts speaker labels in the body)."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return len(re.findall(r"(?m)^\*\*(?:User|Claude|Codex)\*\*: ", text))


def strip_meta_tags(text: str) -> str:
    for pat in META_TAG_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def compress_code_fences(text: str) -> str:
    """Replace contents of fenced code blocks longer than threshold with a placeholder."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    fence_re = re.compile(r"^(\s*)(```+|~~~+)(.*)$")
    while i < len(lines):
        m = fence_re.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, fence, info = m.group(1), m.group(2), m.group(3)
        # find matching close
        j = i + 1
        body: list[str] = []
        while j < len(lines):
            cm = fence_re.match(lines[j])
            if cm and cm.group(2).startswith(fence[0]) and len(cm.group(2)) >= len(fence) and not cm.group(3).strip():
                break
            body.append(lines[j])
            j += 1
        if j >= len(lines):
            # unterminated fence, leave as-is
            out.append(lines[i])
            i += 1
            continue
        if len(body) > CODE_FENCE_THRESHOLD:
            out.append(f"{indent}{fence}{info}")
            out.append(f"{indent}<code: {len(body)} lines elided>")
            out.append(f"{indent}{fence}")
        else:
            out.extend(lines[i : j + 1])
        i = j + 1
    return "\n".join(out)


def extract_assistant_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text":
            t = blk.get("text", "")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
    return "\n\n".join(parts).strip()


def extract_user_text(content) -> str:
    if isinstance(content, list):
        # tool_result lists are dropped entirely
        return ""
    if not isinstance(content, str):
        return ""
    if "<!--capture:skip-->" in content:
        # Internal instruction turns marked capture:skip — not part of the conversation
        return ""
    cleaned = strip_meta_tags(content)
    if is_injected(cleaned, tag=False):
        return ""
    return cleaned.strip()


def is_interactive_session(jsonl_path: Path) -> bool:
    """Headless `claude -p` sessions never emit file-history-snapshot events;
    interactive UI sessions always do. Used to skip auto-summary jsonls spawned
    by SessionEnd hooks, so they don't shadow the real conversation."""
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "file-history-snapshot":
                    return True
    except OSError:
        return False
    return False


def parse_transcript(jsonl_path: Path) -> dict:
    """Returns dict with: session_id, title, first_iso, turns (list of (role, text))."""
    session_id = jsonl_path.stem
    custom_titles: list[str] = []
    first_iso: str | None = None
    turns: list[tuple[str, str]] = []  # (role, text)

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = d.get("type")
            if t == "custom-title":
                ct = d.get("customTitle")
                if isinstance(ct, str) and ct.strip():
                    custom_titles.append(ct.strip())
                continue

            if t == "file-history-snapshot" and first_iso is None:
                ts = (d.get("snapshot") or {}).get("timestamp")
                if isinstance(ts, str):
                    first_iso = ts
                continue

            if t not in ("user", "assistant"):
                continue

            # headless (-p) sessions have no snapshot — use the first message timestamp for the date
            if first_iso is None:
                ts = d.get("timestamp")
                if isinstance(ts, str):
                    first_iso = ts

            msg = d.get("message") or {}
            role = msg.get("role")
            content = msg.get("content")

            if role == "user":
                if d.get("isMeta"):
                    continue
                text = extract_user_text(content)
                if text:
                    turns.append(("user", text))
            elif role == "assistant":
                text = extract_assistant_text(content)
                if text:
                    text = compress_code_fences(text)
                    turns.append(("assistant", text))

    merged = _merge_turns(turns)

    title = custom_titles[-1] if custom_titles else ""
    if not title:
        for role, text in merged:
            if role == "user":
                title = text.replace("\n", " ")[:60].strip()
                break
    if not title:
        title = "untitled"

    return {
        "session_id": session_id,
        "title": title,
        "first_iso": first_iso,
        "turns": merged,
    }


def _merge_turns(turns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge consecutive turns from the same speaker."""
    merged: list[tuple[str, str]] = []
    for role, text in turns:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n" + text)
        else:
            merged.append((role, text))
    return merged


def codex_meta(path: Path) -> dict | None:
    """session_meta.payload from the first rollout line, else None."""
    try:
        with path.open(encoding="utf-8") as f:
            d = json.loads(f.readline() or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("type") != "session_meta":
        return None
    p = d.get("payload")
    return p if isinstance(p, dict) else None


def is_codex_vault_session(meta: dict) -> bool:
    cwd = meta.get("cwd")
    if not isinstance(cwd, str):
        return False
    try:
        c = Path(cwd).resolve()
        root = VAULT_ROOT.resolve()
    except OSError:
        return False
    return c == root or root in c.parents


def is_codex_interactive(meta: dict) -> bool:
    # The source of a terminal TUI session is "cli" or "vscode", depending on the Codex
    # version. exec sessions are "exec" and subagent sessions carry a dict, so both are skipped
    return meta.get("source") in ("cli", "vscode")


def _codex_text(content, kind: str) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == kind:
            tx = blk.get("text", "")
            if isinstance(tx, str) and tx.strip():
                parts.append(tx.strip())
    return "\n\n".join(parts).strip()


def parse_transcript_codex(path: Path) -> dict:
    """Codex rollout → same dict as parse_transcript. Only response_item.message
    user (input_text) and assistant (output_text). Developer messages, tool calls
    and event_msg are dropped."""
    meta = codex_meta(path) or {}
    session_id = meta.get("id") or path.stem
    first_iso = meta.get("timestamp") if isinstance(meta.get("timestamp"), str) else None
    turns: list[tuple[str, str]] = []
    has_phases = False
    completed_through = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "response_item":
                continue
            p = d.get("payload") or {}
            if p.get("type") != "message":
                continue
            role = p.get("role")
            if role == "user":
                text = _codex_text(p.get("content"), "input_text")
                m = _LEAD_TAG_RE.match(text)
                if not text or "<!--capture:skip-->" in text or (m and m.group(1) in CODEX_WRAPPER_TAGS) or _CODEX_PROJECT_DOC_RE.match(text):
                    continue
                text = strip_meta_tags(text)
                if text:
                    turns.append(("user", text))
            elif role == "assistant":
                phase = p.get("phase")
                if phase is not None:
                    has_phases = True
                text = _codex_text(p.get("content"), "output_text")
                if text:
                    turns.append(("assistant", compress_code_fences(text)))
                    if phase == "final_answer":
                        completed_through = len(turns)
    # If an in-progress answer were saved, the turn count after merging would not
    # change when the answer completes, so the completed answer would be skipped
    # as unchanged.
    if has_phases:
        turns = turns[:completed_through]
    merged = _merge_turns(turns)
    title = ""
    for role, text in merged:
        if role == "user":
            title = text.replace("\n", " ")[:60].strip()
            break
    return {
        "session_id": session_id,
        "title": title or "untitled",
        "first_iso": first_iso,
        "turns": merged,
    }


def render_thread(parsed: dict, covers_from: int = 0, source: str = "claude") -> tuple[str, str]:
    """covers_from > 0 means a continued thread — it holds only turns from that
    index on (the session went on, e.g. via --resume, after the original was captured)."""
    iso = parsed["first_iso"]
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    title = parsed["title"] + (" (continued)" if covers_from else "")
    safe_title = title.replace('"', "'")
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    body_lines: list[str] = []
    for role, text in parsed["turns"][covers_from:]:
        label = "**User**" if role == "user" else ("**Codex**" if source == "codex" else "**Claude**")
        body_lines.append(f"{label}: {text}")
        body_lines.append("")
        body_lines.append("---")
        body_lines.append("")
    body = "\n".join(body_lines).rstrip("-\n ").strip()

    cont_lines = f"continues_from_turn: {covers_from}\n" if covers_from else ""
    frontmatter = (
        "---\n"
        f"date: {date_str}\n"
        "type: conversation_original\n"
        f"source: {'codex_cli' if source == 'codex' else 'claude_code'}\n"
        f"session_id: {parsed['session_id']}\n"
        f'original_title: "{safe_title}"\n'
        f"exported_at: {exported_at}\n"
        f"captured_turns: {len(parsed['turns'])}\n"
        f"{cont_lines}"
        "---\n\n"
        f"# {title}\n\n"
    )

    # The suffix is added after the slug so slugify's 40-char cut cannot drop it
    # (otherwise the file would get the original's name and overwrite it in _processed)
    suffix = "_continued" if covers_from else ""
    filename = f"{date_str}_{slugify(parsed['title'])}{suffix}__thread.md"
    return filename, frontmatter + body


def find_candidates(exclude_session: str | None, min_idle: int, source: str = "claude") -> list[Path]:
    """All capture candidates, oldest first. A jsonl modified within min_idle
    seconds may still be live in another terminal, so it is skipped and picked up
    on a later run. Capturing every candidate (not only the latest) means no
    session is missed when several end between runs."""
    now = time.time()
    out: list[Path] = []
    if source == "codex":
        if not CODEX_SESSIONS_DIR.exists():
            return []
        for p in sorted(CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime):
            meta = codex_meta(p)
            if not meta or not is_codex_vault_session(meta):
                continue
            if exclude_session and (meta.get("id") == exclude_session or p.stem.endswith(exclude_session)):
                continue
            if now - p.stat().st_mtime < min_idle:
                continue
            if not is_codex_interactive(meta):
                continue
            out.append(p)
        return out
    if not TRANSCRIPT_DIR.exists():
        return []
    for p in sorted(TRANSCRIPT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        if exclude_session and p.stem == exclude_session:
            continue
        if now - p.stat().st_mtime < min_idle:
            continue
        if not is_interactive_session(p):
            continue
        out.append(p)
    return out


def convert(jsonl_path: Path, *, dry_run: bool = False, allow_headless: bool = False,
            source: str = "claude") -> tuple[bool, str]:
    if source == "codex":
        meta = codex_meta(jsonl_path)
        if meta is None:
            return False, f"skip (not a codex rollout): {jsonl_path.name}"
        if not is_codex_vault_session(meta):
            return False, f"skip (other project): {jsonl_path.name}"
        if not allow_headless and not is_codex_interactive(meta):
            return False, f"skip (codex exec, not interactive): {jsonl_path.name}"
        parsed = parse_transcript_codex(jsonl_path)
    else:
        if not allow_headless and not is_interactive_session(jsonl_path):
            return False, f"skip (headless session, not interactive): {jsonl_path.name}"
        parsed = parse_transcript(jsonl_path)
    if not parsed["turns"]:
        return False, f"skip (empty after filtering): {jsonl_path.name}"

    body_chars = sum(len(t) for _, t in parsed["turns"])
    if body_chars < 300:
        return False, f"skip (trivial, {body_chars} chars): {jsonl_path.name}"

    sid = parsed["session_id"]
    cur = len(parsed["turns"])

    # Sessions moved into the encrypted container are never captured again — their
    # thread is gone from the plain tree, and must not come back from the transcript
    vaulted_f = VAULT_ROOT / "90_Vaulted" / "vaulted_session_ids.txt"
    try:
        if vaulted_f.exists() and sid in vaulted_f.read_text(encoding="utf-8").split():
            return False, f"skip (vaulted): {sid}"
    except OSError:
        pass

    # Turns already captured to files, in _queue or _processed. Capture files are
    # never rewritten (no race with ingest), so the max is enough.
    covered_through = 0
    for path, is_proc, covers_from, cap in captured_index().get(sid, []):
        if cap is None:
            cap = covers_from + _turns_in_file(path)
        covered_through = max(covered_through, cap)

    if cur <= covered_through:
        return False, f"skip (unchanged, {covered_through} turns captured): {sid}"

    if covered_through:
        delta_chars = sum(len(t) for _, t in parsed["turns"][covered_through:])
        if delta_chars < 300:
            # Small growth is not split into its own file; the next capture includes it
            return False, f"skip (trivial growth, {delta_chars} chars): {sid}"

    filename, content = render_thread(parsed, covered_through, source)
    out_path = QUEUE_DIR / filename
    if out_path.exists() or (PROCESSED_DIR / filename).exists():
        stem = out_path.stem.replace("__thread", "")
        suffix = f"_{sid[:8]}_{covered_through}" if covered_through else f"_{sid[:8]}"
        out_path = QUEUE_DIR / f"{stem}{suffix}__thread.md"

    span = f"turns {covered_through}~{cur}" if covered_through else f"{cur} turns"
    if dry_run:
        return True, f"would write: {out_path.name} ({body_chars} chars, {span})"

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return True, f"wrote: {out_path.name} ({body_chars} chars, {span})"


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--auto", action="store_true", help="Find prev jsonl automatically")
    g.add_argument("--input", type=Path, help="Path to a specific jsonl")
    g.add_argument("--session", help="Capture this session id now (for the SessionEnd hook — the session just ended, so no idle wait)")
    g.add_argument("--from-hook", action="store_true",
                   help="Read hook JSON from stdin: Stop/SessionEnd capture that session, other events sweep all except session_id")
    ap.add_argument("--source", choices=["claude", "codex"], default="claude",
                    help="Transcript source (default: claude)")
    ap.add_argument("--allow-headless", action="store_true",
                    help="Also capture headless (claude -p) sessions")
    ap.add_argument("--exclude-session", default=os.environ.get("CLAUDE_SESSION_ID", ""),
                    help="Session id to exclude (defaults to $CLAUDE_SESSION_ID)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="Suppress output unless error")
    ap.add_argument("--min-idle", type=int, default=1800,
                    help="With --auto, skip sessions modified in the last N seconds (avoids capturing live sessions; default 30 min)")
    args = ap.parse_args()

    if args.auto:
        for path in find_candidates(args.exclude_session or None, args.min_idle, args.source):
            ok, msg = convert(path, dry_run=args.dry_run, source=args.source)
            if ok or not args.quiet:
                print(msg)
        return 0

    if args.from_hook:
        try:
            hook = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
        except json.JSONDecodeError:
            hook = {}
        sid = hook.get("session_id") or ""
        is_stop = hook.get("hook_event_name") == "Stop"
        if is_stop or hook.get("hook_event_name") == "SessionEnd":
            tp = hook.get("transcript_path")
            path = Path(tp) if isinstance(tp, str) and tp else None
            if (path is None or not path.exists()) and sid and args.source == "codex":
                found = sorted(CODEX_SESSIONS_DIR.rglob(f"rollout-*{sid}.jsonl"))
                path = found[-1] if found else None
            if path is None or not path.exists():
                if is_stop:
                    print(json.dumps({"systemMessage": "Conversation capture failed: transcript path not found"}, ensure_ascii=False))
                elif not args.quiet:
                    print(f"skip (no transcript): {sid or '?'}")
                return 0
            ok, msg = convert(path, dry_run=args.dry_run, allow_headless=args.allow_headless, source=args.source)
            if is_stop:
                print("{}")
            elif ok or not args.quiet:
                print(msg)
            return 0
        for path in find_candidates(sid or None, args.min_idle, args.source):
            ok, msg = convert(path, dry_run=args.dry_run, source=args.source)
            if ok or not args.quiet:
                print(msg)
        return 0

    if args.session is not None:
        sid = args.session
        if not sid and not sys.stdin.isatty():
            # $CLAUDE_SESSION_ID can be empty in the SessionEnd hook —
            # fall back to session_id from the hook's stdin JSON
            try:
                sid = json.loads(sys.stdin.read() or "{}").get("session_id") or ""
            except json.JSONDecodeError:
                sid = ""
        if not sid:
            if not args.quiet:
                print("skip (no session id)")
            return 0
        path = (next(CODEX_SESSIONS_DIR.glob(f"**/rollout-*-{sid}.jsonl"), None)
                if args.source == "codex" else TRANSCRIPT_DIR / f"{sid}.jsonl")
        if path is None or not path.exists():
            if not args.quiet:
                print(f"skip (no transcript): {sid}")
            return 0
        ok, msg = convert(path, dry_run=args.dry_run, allow_headless=args.allow_headless, source=args.source)
        if ok or not args.quiet:
            print(msg)
        return 0

    path = args.input
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        return 1
    ok, msg = convert(path, dry_run=args.dry_run, allow_headless=args.allow_headless, source=args.source)
    if not args.quiet or not ok:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
