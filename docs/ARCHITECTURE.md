# Architecture

```
   ┌───────────────────────┐
   │   Claude Code session │
   └──────────┬────────────┘
              │ SessionEnd hook
              ▼
   ┌───────────────────────┐
   │  scripts/auto_draft   │  dedup vs existing memories first
   └──────────┬────────────┘  (skip API call if session already published)
              │
              ▼
   ┌───────────────────────┐
   │ scripts/extract_memory│  reads JSONL → redact → Haiku 4.5
   └──────────┬────────────┘  (forced tool use: record_memory)
              │ on packed-XML rejection
              │ ┌───────────────────────┐
              ├─┤ scripts/recover_memory│  unpacks shape A or B
              │ └───────────────────────┘  writes direct to published
              ▼
   memories/_drafts/YYYY-MM-DD/HH-MM-SS.md
              │
              │ (optional) Discord review
              ▼
   ┌───────────────────────┐
   │   bot/memories_bot    │  posts each draft as an embed
   └──────────┬────────────┘  react ✅ → publish, ❌ → reject
              ▼
   memories/YYYY-MM-DD/HH-MM-SS.md   (committed + pushed)
              │
              ▼
   ┌───────────────────────┐
   │ site/ (11ty+Pagefind) │  static site build → deploy anywhere
   └───────────────────────┘
```

A separate Monday-morning `bot/briefing.py` reads `.auto_draft.log`,
`_drafts/`, and `memories/` to post a weekly digest: pending drafts,
recoverable failures (with inline recovery commands), dead failures
(JSONL expired), and pipeline counts.

## Components

| Path | Role |
|------|------|
| `scripts/auto_draft.sh`    | SessionEnd hook entry; dedup + invoke extractor |
| `scripts/extract_memory.py`| Haiku call + redact + write draft. Strict — rejects packed XML rather than guess |
| `scripts/recover_memory.py`| Recovers packed-XML failures by structurally unpacking shape A/B |
| `scripts/publish.sh`       | Move draft → published, commit, push |
| `bot/memories_bot.py`      | Per-draft Discord posts, reaction-driven publish/reject |
| `bot/briefing.py`          | Weekly digest of pipeline state |
| `site/`                    | 11ty static site with Pagefind search |

## Why the extractor is strict

Haiku 4.5 occasionally returns the structured list fields as XML packed
inside one string instead of as proper JSON arrays. Two shapes have been
observed:

- **Shape A** (all-into-one): every field is crammed into one carrier
  string with `<parameter name="...">...</...>` envelopes.
- **Shape B** (per-field): each list field is a string containing
  `<item>...</item>` blocks.

Rather than guessing inside `extract_memory.py`, we hard-reject and surface
the failure. `recover_memory.py` handles both shapes structurally. The
weekly briefing prints the exact recovery command per failure. This keeps
the hot path simple and gives you an audit trail of when the model
misbehaved.

## Session JSONL location

Claude Code stores session JSONLs at:

```
~/.claude/projects/<sanitised-cwd>/<session-id>.jsonl
```

Where `<sanitised-cwd>` is the cwd at session start, with slashes replaced
by dashes (e.g. `/home/alice` → `-home-alice`). The extractor derives this
default from `Path.cwd()`. Override with `CLAUDE_PROJECTS_DIR` if you run
the extractor from a different directory than the session was started in.

JSONLs expire after some time (Claude Code prunes older sessions). When a
JSONL is missing, `recover_memory.py` reports the session as unrecoverable
and the briefing labels it `⚰️ dead`.
