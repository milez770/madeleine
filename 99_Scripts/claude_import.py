#!/usr/bin/env python3
"""
claude_import.py — Claude.ai Export JSON → Inbox/_queue/

Usage:
    python claude_import.py --input claude_export.json --mode new_only
    python claude_import.py --input claude_export.json --mode all
"""

import json
import argparse
import re
import sys
from pathlib import Path
from datetime import datetime, timezone


VAULT_ROOT = Path(__file__).parent.parent
QUEUE_DIR = VAULT_ROOT / "00_Inbox" / "_queue"
PROCESSED_DIR = VAULT_ROOT / "00_Inbox" / "_processed"


def slugify(text: str, max_len: int = 40) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s가-힣]", "", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:max_len].strip("_")


def existing_conversation_ids() -> set[str]:
    ids = set()
    for folder in [QUEUE_DIR, PROCESSED_DIR]:
        for f in folder.glob("*.md"):
            content = f.read_text(encoding="utf-8")
            m = re.search(r"claude_conversation_id:\s*(.+)", content)
            if m:
                ids.add(m.group(1).strip().strip('"'))
    return ids


def extract_message_text(msg: dict) -> str:
    """Claude export: text can be in top-level `text` or `content[].text` (type=text)."""
    top = msg.get("text", "")
    if isinstance(top, str) and top.strip():
        return top.strip()

    parts = []
    for block in msg.get("content", []):
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text", "")
            if t:
                parts.append(t)
        # skip: thinking, token_budget, tool_use, tool_result
    return "\n\n".join(parts).strip()


def format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        # Claude export uses `sender`: "human" | "assistant"
        sender = msg.get("sender") or msg.get("author", {}).get("role", "unknown")
        label = "**User**" if sender == "human" else "**Claude**"

        text = extract_message_text(msg)
        if text:
            lines.append(f"{label}: {text}")
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines).rstrip("-\n ").strip()


def conversation_to_markdown(conv: dict, exported_at: str) -> tuple[str, str] | None:
    conv_id = conv.get("uuid") or conv.get("id", "unknown")
    messages = conv.get("chat_messages") or conv.get("messages") or []

    body = format_messages(messages)
    if not body:
        return None  # skip empty conversations

    # Title: `name` → first human message snippet → "untitled"
    title = (conv.get("name") or conv.get("title") or "").strip()
    if not title:
        for msg in messages:
            if (msg.get("sender") == "human") or (msg.get("author", {}).get("role") == "human"):
                text = extract_message_text(msg)
                if text:
                    title = text.replace("\n", " ")[:60].strip()
                    break
    if not title:
        title = "untitled"

    created_at = conv.get("created_at", "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    slug = slugify(title)
    filename = f"{date_str}_{slug}__thread.md"

    model = "claude"

    safe_title = title.replace('"', "'")
    frontmatter = f"""---
date: {date_str}
type: conversation_original
source: {model}
claude_conversation_id: {conv_id}
original_title: "{safe_title}"
exported_at: {exported_at}
---

# {title}

"""

    content = frontmatter + body

    return filename, content


def main():
    parser = argparse.ArgumentParser(description="Import Claude export JSON to vault inbox")
    parser.add_argument("--input", required=True, help="Path to Claude export JSON file")
    parser.add_argument("--mode", choices=["new_only", "all"], default="new_only",
                        help="new_only: skip existing conversation IDs. all: import everything")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        print(f"Error: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    # Claude export format: top-level list or {"conversations": [...]}
    if isinstance(data, list):
        conversations = data
    elif isinstance(data, dict):
        conversations = data.get("conversations", [])
    else:
        print("Error: unexpected JSON format", file=sys.stderr)
        sys.exit(1)

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    existing_ids = existing_conversation_ids() if args.mode == "new_only" else set()

    imported = 0
    skipped_existing = 0
    skipped_empty = 0
    failed = 0

    for conv in conversations:
        conv_id = conv.get("uuid") or conv.get("id", "")
        if args.mode == "new_only" and conv_id in existing_ids:
            skipped_existing += 1
            continue

        try:
            result = conversation_to_markdown(conv, exported_at)
            if result is None:
                skipped_empty += 1
                continue

            filename, content = result
            out_path = QUEUE_DIR / filename

            if out_path.exists():
                stem = out_path.stem.replace("__thread", "")
                out_path = QUEUE_DIR / f"{stem}_{conv_id[:8]}__thread.md"

            out_path.write_text(content, encoding="utf-8")
            imported += 1
        except Exception as e:
            print(f"Warning: failed to import conversation {conv_id}: {e}", file=sys.stderr)
            failed += 1

    print(f"Imported: {imported}, Skipped (existing): {skipped_existing}, Skipped (empty): {skipped_empty}, Failed: {failed}")
    print(f"Files saved to: {QUEUE_DIR}")


if __name__ == "__main__":
    main()
