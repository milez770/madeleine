# Summarizing a conversation

Read the whole thread. Keep the user's claims, corrections, actions, and open questions. Keep AI suggestions apart from the user's words, and things done apart from things planned. Add no interpretation or conclusion that is not in the thread. Keep the user's own expressions as written; write the rest neutrally.

## Output

Write only YAML frontmatter and a `## Links` section into `20_Wiki/summaries/`. Never edit the analysis content after saving. You may add `[[ ]]` links to the thread and related conversations later.

Fill the fields in this order. When there is no evidence, leave an array empty and omit a conditional field. Do not invent claims, topics, or questions to reach a count.

- `thread_ref`: path of the thread under `00_Inbox/_processed/`, relative to the vault root.
- `thread_type`: `conversation`.
- `date`: date of the conversation.
- `created`: date of the summary.
- `tags`: areas actually covered. Choose from `philosophy`, `finance`, `ai-ops`, `product`, `trading`, `career`, `writing`, `personal`, `tools`, `systems`, `psychology`, `strategy`, `reading`.
- `title`: a short statement of the actual claim or work, in the language of the conversation.
- `one_liner`: one sentence on what was covered and what remains.
- `trajectory`: the order in which claims, questions, corrections, and actions followed each other from the start. A YAML block string with numbered lines.
- `key_claims`: claims actually made. Keep the speaker and the degree of certainty. Do not turn an AI hypothesis into the user's conclusion.
- `topics_discussed`: specific topics actually covered.
- `questions_raised`: questions actually raised and not yet answered.
- `mentioned_entities`: proper names of people, companies, products, books, tickers, and specific events, in `snake_case`. Exclude common nouns. Match names in `20_Wiki/entity_aliases.tsv`; for a new entity, add one line with the id and the other spellings the user writes for it, separated by tabs.
- `disagreements_or_corrections`: when someone revised a position or pushed back, the speaker and the changed judgment.
- `connected_to_prior`: file names of past summaries that the conversation explicitly referred to, without the extension.
- `unresolved`: problems the conversation tried to solve and left open.
- `mind_candidates`: observed events in which the user judged, corrected, rejected, or decided something. At most 2; an empty array when there are none. Each item has `observation`, `domain`, `evidence`. `observation` describes only what happened in this conversation. `domain` is one word. `evidence` is the user's words exactly as written, after checking the user speaker marker in the thread. Never put AI words here. Do not name a way of thinking or turn it into a command. This field does not approve writing a behavior rule.

Escape quotes and line breaks in YAML strings correctly. Write arrays one item per line with `-`. Before saving, parse the YAML and check the field contents.

Under `## Links`, list the thread file name, the summaries in `connected_to_prior`, and the hub the conversation belongs to. Drop `.md` from file names. Any further link needs a past conversation you actually read as its basis. Do not force links or hub assignments.

## Memory ledger

After the summary, append one line to `20_Wiki/recall.md`.

The format is `- YYYY-MM-DD [[summary file name|title]] — hook · hook`. Hooks are the words that will find this conversation later, taken from the thread: names and nicknames of people, names of companies, products, and projects, the user's own expressions, events, decisions, and open problems. Do not repeat words from the title. Keep the whole line under 250 characters.

When looking for links, read both `recall.md` and `recall_cold.md`. If you link a conversation from the cold file, move its line to `recall.md` unchanged. If you recovered a conversation by searching the raw text, add the search words that found it to its line.

During a conversation, the `recall_ppr.py` hook injects candidates. Do not read the whole ledger every session. If you actually read a candidate's summary, follow the `learn` step in AGENTS.md.

## Finish

Check the summary against the thread: speakers, the user's expressions, what was actually done, revised judgments, open problems. After the check, move the thread to `_processed/`. Finish the ledger line and hub link, then append one line to `20_Wiki/log.md`.
