---
name: mind-capture
description: Write or update an approved behavior rule in the vault's mind layer. Use for "make this a rule" or "capture this" requests. Ways of thinking and event records are not handled by this skill.
---

# Writing a behavior rule

Write one behavior rule that `mind-manager` got approved by the user. Keep the user's expression and the requested scope. Add no AI interpretation. Do not name a way of thinking or turn it into a command. Events follow the recording procedure for `episodes.md`.

Read the related mind notes and the source thread. Quote only words confirmed by the user speaker marker in the thread, exactly as written. A project's access, deployment, accounts, permissions, and internal procedures stay in that project.

Write the file to `99_Config/claude_memory/<snake_case>.md`. The name states the behavior to follow. The frontmatter has `name`, `description`, and `metadata.type`. `metadata.type` is `feedback` for a behavior guideline, `user` for the user's identity or preferences, `project` for an ongoing goal or status, and `reference` for reference information.

The body states the situation the rule came from, the concrete behavior to follow every time, and the source evidence. Write inclusions and exclusions only when the scope is confirmed. State uncertain scope and evidence as uncertain. Do not fill gaps with interpretation. Do not rewrite the user's expressions as AI sentences.

Review the whole file and remove distortions of the source, added interpretation, widened scope, and filler. Follow the style in `99_Style/voice.md`.

Add the title, file link, and a one-line description to `MEMORY.md`, and one line to `20_Wiki/log.md`. When changing an existing rule, check the index too. Do not write change history in the body.

The report states the file, the changed behavior, what could not be captured, and what is uncertain, briefly. If there is none, say so.
