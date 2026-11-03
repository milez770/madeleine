# Madeleine operating manual

Claude Code and Codex CLI read this file first when they work in this vault. Every AI task is done by the session itself (Claude Code or Codex CLI). The scripts in `99_Scripts/` (`session_to_thread.py`, `claude_import.py`, `recall_ppr.py`, `lint.py`, `vault_lock.py`, `link_memory.sh`) do mechanical work only. Judgment belongs to the session.

`recall_ppr.py` runs as a hook on every prompt. What it injects is association, not search results: past conversations related to the current one. If the hook does not run, the session works without memory; treat that as a fault.

Codex CLI's hooks and agents are in `.codex/`, and its skills are in `.agents/skills/` (a symlink to `.claude/skills/`). `session_to_thread.py` also captures Codex sessions (`--source codex`, hooks in `.codex/hooks.json`).

## Identity

A personal knowledge system built from the user's AI conversations. **The goal is a digital twin**: it holds the user's views, experiences, and ways of thinking, so that an AI reading it thinks the way the user does. Publishing is optional. There is no vector database; the session reads files directly and judges.

Three layers:
1. **Experience record** (`20_Wiki/summaries/`): structured cards of past conversations. What the user thought, did, and went through. Immutable.
2. **Mind layer** (`99_Config/claude_memory/`): two parts. **Behavior rules** are imperative notes that Claude follows strictly. **Event records** (`episodes.md`) are moments when the user actually thought or decided something, quoted verbatim, with no interpretation. `MEMORY.md` is the index and loads at session start. This layer is the core of the twin.
3. **Entities** (`20_Wiki/entities/`): people, companies, and other concrete references.

**Design principle: deep analysis, minimal process.** New workflows, skills, lifecycles, classification dimensions, and status systems are rejected by default. **Ways of thinking are not written down as rules in the mind layer.** A named concept or an imperative lens makes Claude think to fit the written text. The session reads the events in `episodes.md` and judges ways of thinking each time. Imperative text is used only for behavior rules, where strict compliance is required. Depth comes from faithful capture, from stating what does not fit (residuals), and from refusing surface answers, not from procedure.

## Invariants

1. **Sources are immutable (content only)**: never edit the analysis content (frontmatter values, summary text) in `00_Inbox/_processed/` and `20_Wiki/summaries/`. Adding `[[ ]]` navigation links is allowed.
2. **Mind-layer capture goes through the agent**: writing or changing behavior rules and adding events to `episodes.md` go through the `mind-manager` agent. Events are written from the source thread, never reconstructed from notes or memory. They carry no interpretation, concept names, or commands; only the sequence of events and the verbatim words. Adding or changing a behavior rule needs the user's approval.
3. **The user's own words are kept**: the user's own terms stay verbatim; no AI paraphrase. Never promote an AI formulation to the user's own. When unsure, reject.
4. **The "Manual notes" section of an entity is read-only.**
5. **Look up the relevant past first**: before substantive work, apply the mind layer loaded in the session. The first source of association is the PPR candidates that the hook injects every turn (see "Recall hook" below). Do not read `20_Wiki/recall.md` in full each session. `recall.md` is the source the hook reads, and a backup: when a candidate or clue appears, Read that summary and follow its `## Links` one or two hops to restore detail. If the ledger has nothing, grep the raw threads as the last step. If grep found what recall missed, add the search words that found it to that ledger line as hooks (the ledger is derived; edit it freely).
   - **Two tiers**: unused lines move to `20_Wiki/recall_cold.md`. This is not deletion; the line moves unchanged. The criterion is use, not age: created more than 30 days ago and not used in the last 180 days. The cold file is never read in full; only grep and INGEST read it. When a cold line is used again (a new summary links it, a session reads its summary, grep recovers it), it moves back to `recall.md` at once. Promotion happens any time; demotion happens only at DISTILL, by judgment (lines of ongoing projects and reactivated domains stay).
   - **Recall hook**: every turn, the UserPromptSubmit hook injects candidates (`99_Scripts/recall_ppr.py`). On a machine with the raw-text index (`20_Wiki/.recall_raw/`, derived, per machine), three methods give up to 2 summary candidates each, without overlap. [keyword] takes the PPR top and shows the whole ledger line. [model] uses vectors learned from the user's own conversations (exchanges from the same conversation and hand links). [raw] compares character pieces of the raw text. [model] and [raw] query with the current prompt mixed with the previous two exchanges, and show the closest user turn in that card and its line in the thread. `recall_ppr.py build` makes the index; when there is no index or a summary changed, the hook builds it in the background. A rebuild also aligns the model's pairs with the index: new pairs are computed from the learned piece vectors and removed pairs are dropped. Only training learns new connections. Training is optional and done by hand: `uv run --no-project --python 3.12 --with numpy --with scipy python 99_Scripts/recall_ppr.py train`. The model files stay in the same folder. Without model files, [keyword] takes the [model] slots. A machine without the index gets the PPR top 5 as whole ledger lines. Queued threads that are not ingested yet appear at the top only when a seed word of the prompt also appears in that thread's `**User**` turns: at most 2, each with a slice of user turns (first, last, and four random ones). After ingest they appear as summaries. Open the source (path or `[[link]]`) only for candidates relevant to the current work. **If you actually Read a summary from the candidates**, run `python3 99_Scripts/recall_ppr.py learn "<that turn's prompt>" <summary file names you read...>`; only the links of the cards you read get stronger Hebbian weights. Run the same command for a card recovered by grep.
   - **Answer-side check**: for an answer that states facts or judgments, run `python3 99_Scripts/recall_ppr.py precheck "<gist>"` with the gist of the answer before writing the final answer, and compare the memories it prints with the answer. If the turn already calls tools, put it in the last tool call. precheck seeds the graph with the gist and prints up to 2 cards that the prompt side did not show and whose score reaches the 90th percentile of this machine's last 300 precheck scores (0.041 until there are 50), or one line, "No memory reached". In Claude Code, the Stop hook (`recall_ppr.py stopcheck`) blocks once when the last answer is 300 characters or longer and no tool call in that turn ran precheck; then run precheck with the gist and add only what conflicts.
   - **Use ledger**: the same Stop hook compares the cards the hook showed in that turn with the summaries and threads the session opened, and appends to `20_Wiki/recall_use.tsv`: `used` for a shown card that was opened, `missed` for an opened card that was not shown, both with the prompt, and one `turn` line with the number of cards shown per method. Nothing is written while the vault is unlocked.
6. **Log**: append one line to `20_Wiki/log.md` for each major operation (INGEST / CAPTURE / DISTILL / LINT).
7. **Memory is the mind layer, in one place**: `99_Config/claude_memory/`, with `MEMORY.md` as the index, loaded at session start. `~/.claude/projects/<project>/memory` is only a symlink to this store; `99_Scripts/link_memory.sh` recreates it at every session start on every device. Do not write into `~/.claude` directly.
8. **No first-pass output**: output only what has passed a self-critical review.

## File naming

- Thread: `00_Inbox/_processed/YYYY-MM-DD_slug__thread.md`
- Summary: `20_Wiki/summaries/YYYY-MM-DD_slug__summary.md`
- Entity: `20_Wiki/entities/snake_case.md`
- Mind note: `99_Config/claude_memory/snake_case.md`, frontmatter `metadata.type`: feedback / user / project / reference. Ways of thinking go into `episodes.md` as events, not into notes. One index line in `MEMORY.md` per note.

No classification kinds. No edge taxonomy.

All top-level folders: besides the structure above, `30_Drafts/` (drafts; move to `40_Published/` when published; plugins, scripts, and tools made in a session also go into a subfolder here unless they are their own git repository), `40_Published/` (published final versions), and `90_Vaulted/` (encrypted container for sensitive memory, opened and closed only with `99_Scripts/vault_lock.py`; see "Sensitive memory lock"). Any other top-level folder or root file must be listed here or removed; `lint.py` reports it.

---

## Workflows

### IMPORT

**Trigger**: `import [json_path]`

```bash
python3 99_Scripts/claude_import.py --input [json_path] --mode new_only
```

Imports a claude.ai data export into `00_Inbox/_queue/`.

### SESSION_CAPTURE

**Trigger**: automatic (SessionStart and SessionEnd hooks; for Codex, SessionStart, Stop, and SessionEnd). Manual: `python3 99_Scripts/session_to_thread.py --auto` (Codex: `--source codex --auto`).

Saves CLI sessions as threads in `00_Inbox/_queue/`. The script defines the filtering policy and idempotency (`session_id`). The same SessionStart hook runs `link_memory.sh`, so the mind layer loads on every device.

### INGEST

**Trigger**: `ingest`, `process next`, `process [filename]`

1. Pick a file from `_queue/` (`process next` = the oldest).
2. Write the summary as `99_Config/prompts/summarize_conversation.md` describes (frontmatter card; the body holds only `## Links`) → `20_Wiki/summaries/`.
3. Move the thread to `_processed/`.
4. **Ledger line and links**: append one hook line to `20_Wiki/recall.md` (rules in the prompt's ledger section). If the ledger shows past summaries related to this conversation, add links to them in the new summary's `## Links`. Only certain ones; no forced links. **INGEST reads both ledgers** (`recall.md` and `recall_cold.md`). If a cold line gets linked, move it to `recall.md` unchanged.
5. **Thread hubs**: if the conversation belongs to an existing hub note in `20_Wiki/threads/`, add one line linking the summary to that hub. A hub is **a topic or project that continues across conversations**. Do not assign hubs by ways of thinking or lenses. If no hub fits, leave the summary unattached; do not attach it to a big hub that fits only loosely. If a new hub seems needed, do not create it right away; report it and record it in the log as a candidate. **When the same candidate has been logged 3 times, INGEST creates the hub.** Creation goes by count, not judgment; say in the report that it was created. Closing and merging hubs happen only at DISTILL.
6. **No concept creation**: the summary's `mind_candidates` are settled observations, not note proposals. One exception: if the user, in their own voice, reversed, corrected, rejected, or concluded something decisively, flag it in the report as an event candidate for `episodes.md`.
7. Append to `log.md` and report (the gist of the summary and one line of settled observation).

### DISTILL

**Trigger**: `distill` (manual only; never scheduled or automated)

**It creates no concepts.** It picks two kinds of things from the settled observations and reports them:

1. Scan the `mind_candidates` of summaries created since the last `DISTILL:` line in `log.md`.
2. **Event candidates**: moments when the user actually reversed, corrected, rejected, or concluded something. **Pattern discovery happens here.** The same move across several domains is the reason to select events, and the report may say "these events look like the same move". Store only the events: the name or definition of a pattern is written nowhere. Each reading session finds the pattern again from the events. When approved, `mind-manager` **writes the events from the source thread** into `episodes.md`: no interpretation or concept names, verbatim words, in time order.
3. **Behavior rule candidates**: only things that **need strict compliance**, such as Claude's repeated mistakes or the user's repeated requests. When approved, write them as imperative notes through CAPTURE.
4. The user's decision is binary: **keep or drop.** There is no lifecycle; if a dropped item was real, it will settle again.
5. **Ledger demotion**: move lines of `recall.md` created more than 30 days ago and not used in the last 180 days (linked, given a new hook, recovered) to `recall_cold.md`. Move lines unchanged, by judgment only; lines of ongoing projects and reactivated domains stay.
6. **Hub review**: check whether hubs created by count are right; split, close, or merge hubs that grew too large.
7. Append a `DISTILL:` line to `log.md`. The next pass starts after this line.

### CAPTURE

**Trigger**: the user's approval, or `capture X`

`mind-manager` agent → writes a **behavior rule** with capture discipline (faithful, verbatim voice, residuals, self-critique) → `99_Config/claude_memory/`. One index line in `MEMORY.md` and one log line.

Scope: behavior rules only (feedback / user / project / reference). **Ways of thinking and lenses are not CAPTURE targets**; they go into `episodes.md` as events. **Work context (access, deployment, permissions, internal procedures) is not a target either**. The experience layer (threads, summaries, recall) carries work context. The mind layer holds only twin material: cross-project behavior rules, the user's identity, and events. Never force-fill a slot.

### CREATE_ENTITY

**Trigger**: the user's approval, or `create entity X`

`99_Templates/entity_template.md` → `20_Wiki/entities/[name].md`. The "Manual notes" section is not edited afterwards.

### Sensitive memory lock

Sensitive memory is stored in `90_Vaulted/store.tar.age`, an encrypted container made with `age`. The default state is locked: the recall hook, grep, and INGEST see only non-sensitive files. Sensitive files are included only while unlocked (`90_Vaulted/open/` plus a marker).

- Create the key pair once with `python3 99_Scripts/vault_lock.py init`.
- Unlock and lock **in a separate terminal**: `python3 99_Scripts/vault_lock.py unlock|lock|status`. Never type the passphrase into the chat; session capture would record it. The session never asks for the passphrase and never accepts it.
- Idle auto-lock (3 hours after the last prompt, `VAULT_LOCK_IDLE_H`): reseal, delete `open/`, capture the session, then seal its transcript (`.age`; resume works again after the next unlock).
- Do not use Codex while unlocked. `lock` does not seal Codex session logs (`~/.codex/`).
- INGEST: if a queued thread looks sensitive, produce no plaintext output (summary, ledger line). While locked, only seal it with `vault_lock.py seal <thread path>` and mark it in the report; it is unpacked under `open/` at the next unlock. While unlocked, move it with `vault_lock.py add <thread path>` and process it under `open/`. Outputs of conversations from the `open/` queue are written under `open/` at the same relative path from the start, and their ledger line goes into `open/20_Wiki/recall_vaulted.md`. A summary already processed in plaintext can be moved after approval, while unlocked, with `vault_lock.py add <summary path>`; its thread and ledger line move with it, and the raw-text index is rebuilt without them.
- `vault_lock.py seal <file>` uses the public key, so it works while locked.
- If asked for sensitive content while locked, explain only the unlock procedure. No workaround access (git history, caches, transcripts).

### LINT

**Trigger**: `lint the wiki`

```bash
python3 99_Scripts/lint.py
```

The script judges all four checks mechanically: index consistency, orphan summaries, dead hub links, unlisted top-level entries. The session only runs it, fixes violations, and appends to the log. No automatic fixes.

### WRITING_QUERY

**Trigger**: "what have I been thinking about lately", "I want to write something, what about?"

Read the relevant mind notes and recent summary cards and present them. If the user wants, write a draft at `30_Drafts/YYYY-MM-DD_slug.md`.

---

## Writing

- Names, titles, descriptions, and reports are written the way a person would say them. No filler, no AI phrasing.
- Before writing or editing a draft, read `99_Style/voice.md`. When you notice a meaningful editing pattern, add one or two lines to `voice.md` (by judgment, not on every edit).
- Summaries are neutral and descriptive. A `log.md` entry is one line.
- Write vault files in the language of the conversation they come from.
