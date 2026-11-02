#!/usr/bin/env python3
"""Vault integrity lint — mechanical checks only, no judgment.

Four checks:
1. MEMORY.md index — every mind note is in the index, and the index points to no missing file
2. Orphan summary — a summary with no line in recall.md or recall_cold.md
3. Thread-hub dead link — a hub in threads/ points to a summary that does not exist
4. Unlisted top level — a top-level folder or root file not in the folder list of CLAUDE.md

Prints the list and exits 1 on violations, otherwise prints one OK line. No auto-fix — the session fixes.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIND = ROOT / "99_Config" / "claude_memory"
SUMM = ROOT / "20_Wiki" / "summaries"
THREADS = ROOT / "20_Wiki" / "threads"
RECALL = [ROOT / "20_Wiki" / "recall.md", ROOT / "20_Wiki" / "recall_cold.md"]

ALLOWED_TOP = {
    "00_Inbox", "20_Wiki", "30_Drafts", "40_Published",
    "90_Vaulted", "99_Config", "99_Scripts", "99_Style", "99_Templates",
}
ALLOWED_ROOT_FILES = {"CLAUDE.md", "README.md", "README.ko.md", "LICENSE", ".gitignore"}

problems = []

# 1. MEMORY.md index
memory_md = (MIND / "MEMORY.md").read_text(encoding="utf-8")
indexed = set(re.findall(r"\]\(([^)]+\.md)\)", memory_md))
notes = {p.name for p in MIND.glob("*.md")} - {"MEMORY.md"}
for n in sorted(notes - indexed):
    problems.append(f"missing from index: {n} — no line in MEMORY.md")
for n in sorted(indexed - notes):
    problems.append(f"dead index: MEMORY.md points to missing file {n}")

# 2. Orphan summary
ledger = "\n".join(p.read_text(encoding="utf-8") for p in RECALL if p.exists())
for s in sorted(SUMM.glob("*__summary.md")):
    if s.stem not in ledger:
        problems.append(f"orphan summary: {s.name} — no line in the recall ledger")

# 3. Thread-hub dead link
for t in sorted(THREADS.glob("*.md")):
    for link in re.findall(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]", t.read_text(encoding="utf-8")):
        if link.endswith("__summary") and not (SUMM / f"{link}.md").exists():
            problems.append(f"thread-hub dead link: {t.name} → {link}")

# 4. Unlisted top level
for p in sorted(ROOT.iterdir()):
    if p.name.startswith("."):
        continue
    if p.is_dir() and p.name not in ALLOWED_TOP:
        problems.append(f"unlisted folder: {p.name}/ — list it in CLAUDE.md or remove it")
    if p.is_file() and p.name not in ALLOWED_ROOT_FILES:
        problems.append(f"stray root file: {p.name}")

if problems:
    print(f"LINT: {len(problems)} violation(s)")
    for p in problems:
        print(f"- {p}")
    sys.exit(1)
print("LINT: OK — index consistent · 0 orphans · 0 dead links · 0 unlisted")
