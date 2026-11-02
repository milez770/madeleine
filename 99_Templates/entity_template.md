---
type: entity
entity_type: person  # person | work | organization | concept | event
created: YYYY-MM-DD
name_variants: []
---

# [Entity name]

## Basics
- [Dates, or year of publication]
- [Short description]

## Mentions

```dataview
TABLE date, title
FROM "20_Wiki/summaries"
WHERE contains(mentioned_entities, "entity_snake_case")
SORT date DESC
```

## Manual notes

_This section belongs to the user. Claude does not edit it._
