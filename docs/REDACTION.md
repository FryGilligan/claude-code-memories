# Redaction

The extractor runs every transcript through a fixed list of regex patterns
before sending it to the model, and again before writing the memory body to
disk. The bot also scrubs IPs (and optionally hostnames) from Discord embed
text — but never from the file on disk.

## What gets redacted in transcripts + memory bodies

Edit `scripts/extract_memory.py` (`REDACTORS` list) to add or remove patterns.

| Pattern | Replacement |
|---------|-------------|
| `ANTHROPIC_API_KEY=...` style env-style lines | `<REDACTED:env>` |
| Anthropic `sk-ant-...` keys | `<REDACTED:anthropic-key>` |
| OpenAI `sk-...` keys | `<REDACTED:openai-key>` |
| AWS access keys (`AKIA...`) | `<REDACTED:aws-key>` |
| Discord bot tokens (`M...`/`N...`.`...`.`...` shape) | `<REDACTED:discord-token>` |
| GitHub tokens (`ghp_...`, `gho_...`, etc.) | `<REDACTED:github-token>` |
| Bare `*_TOKEN=`, `*_SECRET=`, `*_KEY=` env-style lines | `<REDACTED:env>` |
| Private SSH key blocks | `<REDACTED:ssh-key>` |

The redactor is intentionally fail-open per pattern: each regex runs
independently, so adding a new one is a one-line change.

### Adding a pattern

Edit `scripts/extract_memory.py`:

```python
REDACTORS = [
    # ... existing patterns ...
    (re.compile(r'YOUR_PATTERN_HERE'), '<REDACTED:your-label>'),
]
```

Always commit the redactor change *first*, then re-run any extractions you
want re-redacted.

## What gets scrubbed from Discord embeds (and only embeds)

`bot/memories_bot.py` scrubs the embed text it posts to Discord. The
memory file on disk and on the static site is untouched.

| Env var | Behaviour |
|---------|-----------|
| `SCRUB_EMBEDS=true` (default) | Strip IPs from embed text |
| `SCRUB_HOST_SUFFIXES=example.com,internal.lan` | Also strip hostnames ending in any of these suffixes |

Use this when you want to keep an internal hostname or IP out of Discord
previews but still have it in the published memory.

## What is NOT redacted

- Tool paths (filenames, directories) — these are core context.
- Code snippets — assumed safe; if you paste a secret into the model's
  context, you should rotate it.
- Memory frontmatter (date, time, session_id, model, duration).
- File contents the model decides to include in its summary.

If you handle especially sensitive data, run a final pass over
`memories/YYYY-MM-DD/*.md` before publishing the site — the redactor is the
last line, not the first.
