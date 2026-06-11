#!/usr/bin/env python3
"""extract_memory.py — turn a Claude Code session JSONL into a structured memory.md.

Reads the latest session JSONL (or one specified by --session-id / --jsonl),
normalises it into a clean conversation transcript, redacts known secret
patterns, asks Haiku 4.5 for metadata + structured summary sections, then
writes memories/YYYY-MM-DD/HH-MM-SS.md.

ANTHROPIC_API_KEY is loaded from ~/.config/anthropic/.env (env-style file,
no shell interpretation).

Usage:
  extract_memory.py                          # latest session
  extract_memory.py --session-id <uuid>      # specific session
  extract_memory.py --jsonl <path>           # specific file
  extract_memory.py --output-dir <dir>       # default ~/memories/memories
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── config ─────────────────────────────────────────────────────────
# Repo root = parent of scripts/. Override with MEMORIES_REPO env var.
MEMORIES_REPO = Path(os.environ.get(
    "MEMORIES_REPO", str(Path(__file__).resolve().parent.parent)))
DEFAULT_OUTPUT_DIR = MEMORIES_REPO / "memories"
# Claude Code stores session JSONLs at ~/.claude/projects/<sanitised-cwd>/.
# Default: derive the sanitised-cwd from the current working directory.
# Override with CLAUDE_PROJECTS_DIR if you run extract_memory from a different
# directory than the session was started in.
PROJECTS_DIR = Path(os.environ.get(
    "CLAUDE_PROJECTS_DIR",
    str(Path.home() / ".claude" / "projects"
        / ("-" + str(Path.cwd()).lstrip("/").replace("/", "-")))))
ANTHROPIC_ENV = Path(os.environ.get(
    "ANTHROPIC_ENV", str(Path.home() / ".config" / "anthropic" / ".env")))
MODEL = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS = 4096
API_TIMEOUT_S = 180

# Conservative char budget for the transcript portion of the user message.
# Haiku 4.5 has a 200k-token context. Empirically, code/JSON-heavy session
# transcripts tokenise at ~2.9 chars/token (not the textbook ~4). At 480k
# chars this lands around ~165k input tokens, leaving headroom for the
# system prompt, tool schema, and 4k of output.
MAX_TRANSCRIPT_CHARS = 480_000

# ── redaction patterns (applied in order) ──────────────────────────
REDACTION_PATTERNS = [
    # SSH / GPG private key blocks (multiline, non-greedy)
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'),
     '<REDACTED:private-key>'),
    # JWTs (header.payload.signature)
    (re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b'),
     '<REDACTED:jwt>'),
    # Anthropic keys
    (re.compile(r'\bsk-ant-[a-zA-Z0-9_\-]{20,}\b'), '<REDACTED:anthropic-key>'),
    # GitHub tokens (ghp_ ghu_ gho_ ghs_ ghr_)
    (re.compile(r'\bgh[opusr]_[A-Za-z0-9]{20,}\b'), '<REDACTED:github-token>'),
    # AWS access keys
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '<REDACTED:aws-key>'),
    # Discord bot tokens: <base64 user-id>.<base64 timestamp>.<hmac>
    (re.compile(r'\b[MN][A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b'),
     '<REDACTED:discord-token>'),
    # OpenAI keys (sk-... 40+ chars; must come AFTER sk-ant- match above)
    (re.compile(r'\bsk-[A-Za-z0-9]{40,}\b'), '<REDACTED:openai-key>'),
    # Backblaze application keys (K0xx prefix; base64-ish charset incl. + / _ -)
    (re.compile(r'\bK[0-9]{3}[A-Za-z0-9+/=_\-]{25,}\b'), '<REDACTED:b2-key>'),
    # Generic long hex strings (32+ chars covers SHA256, HMAC secrets, restic IDs)
    (re.compile(r'\b[a-f0-9]{32,}\b'), '<REDACTED:hex>'),
    # KEY=VALUE / KEY: VALUE pairs where the key name suggests secret material.
    # Optional prefix lets us catch both bare PASSWORD= and MY_APP_PASSWORD=.
    (re.compile(
        r'(?im)^(\s*(?:[A-Z][A-Z0-9_]*_)?(?:PASSWORD|PASSPHRASE|SECRET|TOKEN|KEY|APIKEY)[A-Z0-9_]*)\s*[=:]\s*\S.*$'
     ),
     r'\1=<REDACTED>'),
    # HTTP Authorization headers
    (re.compile(r'(?i)\b(authorization)\s*:\s*\S+'), r'\1: <REDACTED>'),
    (re.compile(r'(?i)\b(bearer)\s+\S+'), r'\1 <REDACTED>'),
]


def redact(text: str) -> str:
    for pattern, replacement in REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate_for_haiku(transcript: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """If transcript exceeds the model's input budget, keep head+tail and
    insert a clear marker in the middle. Sessions this long are rare; the
    head usually carries intent, the tail carries decisions/outcomes."""
    if len(transcript) <= limit:
        return transcript
    head_share = int(limit * 0.3)
    tail_share = limit - head_share
    omitted = len(transcript) - limit
    marker = (
        f"\n\n... [transcript truncated to fit context window: "
        f"{omitted:,} chars from the middle omitted] ...\n\n"
    )
    return transcript[:head_share] + marker + transcript[-tail_share:]


# ── env loading ────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    if not path.exists():
        return {}
    out = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        k, _, v = line.partition('=')
        out[k.strip()] = v
    return out


# ── session loading ────────────────────────────────────────────────
def find_latest_jsonl(projects_dir: Path) -> Path:
    files = sorted(projects_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"No session JSONL files found in {projects_dir}")
    return files[-1]


def load_session(path: Path) -> list:
    events = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def event_text(content) -> str:
    """Render any content shape into transcript text."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get('type')
            if btype == 'text':
                parts.append(block.get('text', '').strip())
            elif btype == 'tool_use':
                name = block.get('name', '?')
                inp = block.get('input', {})
                parts.append(f"[tool_use: {name}]\n```json\n{json.dumps(inp, indent=2, ensure_ascii=False)}\n```")
            elif btype == 'tool_result':
                tc = block.get('content', '')
                if isinstance(tc, list):
                    tc = '\n'.join(b.get('text', '') if isinstance(b, dict) else str(b) for b in tc)
                parts.append(f"[tool_result]\n```\n{tc}\n```")
        return '\n\n'.join(p for p in parts if p).strip()
    return str(content).strip()


def build_transcript(events: list):
    """Build ### Role transcript from events. Returns (markdown, first_ts, last_ts, metadata)."""
    blocks = []
    timestamps = []
    cwds = set()
    branches = set()
    for ev in events:
        if 'cwd' in ev and ev['cwd']:
            cwds.add(ev['cwd'])
        br = ev.get('gitBranch')
        if br:
            branches.add(br)
        ev_type = ev.get('type')
        if ev_type not in ('user', 'assistant'):
            continue
        msg = ev.get('message')
        if not isinstance(msg, dict):
            continue
        text = event_text(msg.get('content'))
        if not text:
            continue
        ts = ev.get('timestamp')
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts.replace('Z', '+00:00')))
            except (ValueError, TypeError):
                pass
        role_label = '### User' if ev_type == 'user' else '### Assistant'
        blocks.append(f"{role_label}\n\n{text}")
    transcript = '\n\n'.join(blocks)
    first_ts = min(timestamps) if timestamps else datetime.now(timezone.utc)
    last_ts = max(timestamps) if timestamps else first_ts
    return transcript, first_ts, last_ts, {
        'cwds': sorted(cwds),
        'branches': sorted(branches),
        'duration_mins': max(1, int((last_ts - first_ts).total_seconds() // 60)),
    }


# ── Anthropic call (forced structured output via tool_use) ─────────
SYSTEM_PROMPT = """You summarise Claude Code session transcripts.

Always call the `record_memory` tool with the structured fields. Do not produce free-form prose; the only valid response is a tool call.

REDACTION RULES (apply when filling any string field):
- NEVER reproduce: passwords, API keys, secrets, tokens, hex strings ≥ 16 chars, JWTs, B2/AWS/Anthropic/GitHub tokens, restic passphrases, tunnel tokens, SSH private keys, contents of .env or config.json files, cloud subscription/tenant IDs.
- Reference such values by name only, e.g. "rotated the webhook HMAC secret", never quoting the value.
- Paths, container names, hostnames, public IPs, user email — fine.

For a transcript that is genuinely empty / contentless, fill every field with the closest accurate value (empty arrays, "None.", false). Do not invent activity.
"""

TOOL_SCHEMA = {
    "name": "record_memory",
    "description": "Record a structured memory of a Claude Code session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 6,
                "description": "REQUIRED. Always provide 2-6 short lowercase hyphenated topic tags that capture the dominant themes (e.g. 'caddy', 'github-actions', 'memory-extraction'). Only return an empty array if the transcript contained literally no actions or topics — in any other case, give 2-6 tags. Do not skip this field."
            },
            "summary": {
                "type": "string",
                "description": "2-4 sentence narrative of what happened, past tense. Plain prose, no markdown."
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-10 distinct topics or areas worked on."
            },
            "tools_table": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "purpose": {"type": "string"}
                    },
                    "required": ["path", "purpose"]
                },
                "description": "Every file path that was created or substantially modified during the session, each with a one-line purpose. This is the authoritative list of artefacts; it feeds both the body table AND the frontmatter `tools_produced` list. Do not omit files even if there are many."
            },
            "decisions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Architectural / design / process decisions made, with a brief reason. Empty array if none."
            },
            "follow_ups": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Unresolved follow-up items left at session end. Empty array if everything was closed out."
            }
        },
        "required": ["tags", "summary", "topics", "tools_table", "decisions", "follow_ups"]
    }
}


def call_haiku(api_key: str, transcript: str) -> dict:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "tools": [TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": "record_memory"},
        "messages": [{"role": "user", "content": f"Session transcript follows. Call record_memory.\n\n---\n\n{transcript}"}],
    }).encode('utf-8')
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='replace')
        raise SystemExit(f"Anthropic API error {e.code}: {err}")

    # Forced tool_use → first content block is the tool_use
    for block in data.get('content', []):
        if block.get('type') == 'tool_use' and block.get('name') == 'record_memory':
            return block.get('input', {})
    raise SystemExit(f"Haiku did not return a tool_use block; got: {data}")


# Fields that should be a list of strings, and the one that's a list of dicts.
_STR_LIST_FIELDS = ('tags', 'topics', 'decisions', 'follow_ups')
_TABLE_FIELD = 'tools_table'

# Tag names that only appear when the model has packed the whole structured
# payload into one field as XML (see the 2026-05-12 entry). If a coerced string
# still carries these, splitting on newlines won't save it — reject instead.
_PACKED_XML_RE = re.compile(
    r'<\s*/?\s*(?:item|topics|decisions|follow_ups|tools_table|tags)\b', re.I
)


def _split_markdown_list(text: str) -> list:
    """Recover a list from a newline-delimited markdown string: one item per
    non-empty line, with any leading bullet ('- ', '* ', '• ') or numbering
    ('1. ', '2) ') stripped."""
    items = []
    for line in text.splitlines():
        line = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s+', '', line.strip())
        if line:
            items.append(line)
    return items


def _reject_if_packed(memo: dict, field: str, value: str) -> None:
    if _PACKED_XML_RE.search(value):
        raise SystemExit(
            f"record_memory packed structured XML into `{field}` — refusing to "
            f"render (would corrupt the entry). Re-run extraction. Full input:\n"
            f"{json.dumps(memo, indent=2, ensure_ascii=False)[:2000]}"
        )


def normalize_memo(memo: dict) -> list:
    """Coerce list fields that Haiku occasionally returns as a newline-delimited
    markdown string back into real lists, in place. Returns the names of fields
    that were coerced (for logging). Hard-fails only on the pathological case
    where the whole payload was packed into one field as XML."""
    coerced = []
    for f in _STR_LIST_FIELDS:
        v = memo.get(f)
        if isinstance(v, str):
            _reject_if_packed(memo, f, v)
            memo[f] = _split_markdown_list(v)
            coerced.append(f)
        elif v is not None and not isinstance(v, list):
            raise SystemExit(f"record_memory returned {type(v).__name__} for `{f}` (expected list).")

    tt = memo.get(_TABLE_FIELD)
    if isinstance(tt, str):
        _reject_if_packed(memo, _TABLE_FIELD, tt)
        # Degraded shape: path-only rows, no purpose. Better than failing.
        memo[_TABLE_FIELD] = [{'path': p, 'purpose': ''} for p in _split_markdown_list(tt)]
        coerced.append(_TABLE_FIELD)
    elif tt is not None and not isinstance(tt, list):
        raise SystemExit(f"record_memory returned {type(tt).__name__} for `{_TABLE_FIELD}` (expected list).")

    return coerced


def assemble_body(memo: dict) -> str:
    """Render the structured tool_use input into the markdown body."""
    parts = []

    parts.append("## Summary\n")
    parts.append((memo.get('summary') or '').strip() or 'No summary.')

    parts.append("\n\n## Topics Covered\n")
    topics = memo.get('topics') or []
    if topics:
        parts.append('\n'.join(f"- {t}" for t in topics))
    else:
        parts.append('None.')

    parts.append("\n\n## Tools / Scripts Produced\n")
    rows = memo.get('tools_table') or []
    if rows:
        parts.append("| Path | Purpose |\n| --- | --- |")
        for r in rows:
            if isinstance(r, dict):
                path = (r.get('path') or '').replace('|', '\\|')
                purpose = (r.get('purpose') or '').replace('|', '\\|')
            elif isinstance(r, str):
                path = r.replace('|', '\\|')
                purpose = ''
            else:
                continue
            parts.append(f"| `{path}` | {purpose} |")
        parts[-len(rows)-1:] = ['\n'.join(parts[-len(rows)-1:])]
    else:
        parts.append('None.')

    parts.append("\n\n## Decisions Made\n")
    decisions = memo.get('decisions') or []
    if decisions:
        parts.append('\n'.join(f"- {d}" for d in decisions))
    else:
        parts.append('None.')

    parts.append("\n\n## Open Items / Follow-ups\n")
    follow = memo.get('follow_ups') or []
    if follow:
        parts.append('\n'.join(f"- [ ] {f}" for f in follow))
    else:
        parts.append('None.')

    return ''.join(parts)


# ── output ─────────────────────────────────────────────────────────
def yaml_value(v):
    """Cheap YAML-safe rendering for the values we emit."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return json.dumps(v)  # JSON is valid YAML for arrays of scalars
    return json.dumps(str(v))


def build_frontmatter(session_id: str, first_ts: datetime, derived: dict, haiku_meta: dict) -> str:
    """Assemble the YAML frontmatter block. Booleans/tools_produced are derived
    from the authoritative structured fields (tools_table, decisions, follow_ups)
    so the model can't fill one but not the other."""
    date_str = first_ts.strftime('%Y-%m-%d')
    time_str = first_ts.strftime('%H:%M:%S')
    tools_table = haiku_meta.get('tools_table') or []
    decisions = haiku_meta.get('decisions') or []
    follow_ups = haiku_meta.get('follow_ups') or []
    fm = {
        'session_id': session_id,
        'date': date_str,
        'time': time_str,
        'duration_mins': derived['duration_mins'],
        'tags': haiku_meta.get('tags') or [],
        'projects': derived.get('cwds', []),
        'tools_produced': [r.get('path', '') for r in tools_table if r.get('path')],
        'decisions_made': bool(decisions),
        'open_items': bool(follow_ups),
    }
    return '---\n' + '\n'.join(f'{k}: {yaml_value(v)}' for k, v in fm.items()) + '\n---\n\n'


def write_memory(output_dir: Path, session_id: str, first_ts: datetime,
                 derived: dict, haiku_meta: dict, body: str, transcript_redacted: str) -> Path:
    date_str = first_ts.strftime('%Y-%m-%d')
    target_dir = output_dir / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{first_ts.strftime('%H-%M-%S')}.md"

    fm_text = build_frontmatter(session_id, first_ts, derived, haiku_meta)

    raw_section = (
        '\n\n## Raw Context Dump\n\n'
        '<details class="raw-dump">\n'
        '<summary>session transcript (redacted)</summary>\n'
        '<div class="content">\n\n'
        + transcript_redacted +
        '\n\n</div>\n</details>\n'
    )

    target.write_text(fm_text + body.rstrip() + raw_section)
    return target


# ── main ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session-id', help='Session UUID (filename without .jsonl)')
    ap.add_argument('--jsonl', help='Path to a specific JSONL file')
    ap.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    ap.add_argument('--projects-dir', type=Path, default=PROJECTS_DIR)
    ap.add_argument('--dry-run', action='store_true',
                    help='Print the rendered memory to stdout instead of writing.')
    ap.add_argument('--transcript-only', action='store_true',
                    help='Print just the parsed+redacted transcript and exit (no Haiku call).')
    ap.add_argument('--show-tool-input', action='store_true',
                    help='In addition to normal output, print the raw record_memory tool_use input as JSON to stderr.')
    args = ap.parse_args()

    env = load_env(ANTHROPIC_ENV)
    api_key = env.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise SystemExit(f"ANTHROPIC_API_KEY not found in {ANTHROPIC_ENV} or env")

    if args.jsonl:
        jsonl_path = Path(args.jsonl)
    elif args.session_id:
        jsonl_path = args.projects_dir / f"{args.session_id}.jsonl"
    else:
        jsonl_path = find_latest_jsonl(args.projects_dir)
    if not jsonl_path.exists():
        raise SystemExit(f"Session JSONL not found: {jsonl_path}")

    session_id = jsonl_path.stem
    print(f"reading {jsonl_path} ({jsonl_path.stat().st_size:,} bytes)", file=sys.stderr)

    events = load_session(jsonl_path)
    print(f"  → {len(events)} events", file=sys.stderr)

    transcript, first_ts, last_ts, derived = build_transcript(events)
    if not transcript:
        raise SystemExit("transcript is empty — nothing to extract")
    print(f"  → transcript {len(transcript):,} chars, duration ≈ {derived['duration_mins']} min", file=sys.stderr)

    transcript_redacted = redact(transcript)

    if args.transcript_only:
        sys.stdout.write(transcript_redacted)
        return

    transcript_for_haiku = truncate_for_haiku(transcript_redacted)
    if len(transcript_for_haiku) != len(transcript_redacted):
        print(f"  → transcript exceeds {MAX_TRANSCRIPT_CHARS:,} chars; truncated middle for Haiku call (full text still embedded in output)", file=sys.stderr)

    print(f"calling {MODEL} (input ≈ {len(transcript_for_haiku) // 4:,} tokens)", file=sys.stderr)
    memo = call_haiku(api_key, transcript_for_haiku)
    coerced = normalize_memo(memo)
    if coerced:
        print(f"  → coerced string→list field(s): {', '.join(coerced)}", file=sys.stderr)
    if args.show_tool_input:
        print("=== record_memory tool_use input ===", file=sys.stderr)
        print(json.dumps(memo, indent=2, ensure_ascii=False), file=sys.stderr)
        print("=== end tool_use input ===", file=sys.stderr)
    haiku_body = redact(assemble_body(memo))  # second redaction pass over generated text

    if args.dry_run:
        sys.stdout.write(build_frontmatter(session_id, first_ts, derived, memo))
        sys.stdout.write(haiku_body.rstrip())
        sys.stdout.write(
            '\n\n## Raw Context Dump\n\n'
            '<details class="raw-dump">\n'
            '<summary>session transcript (redacted)</summary>\n'
            '<div class="content">\n\n'
            + transcript_redacted +
            '\n\n</div>\n</details>\n'
        )
        return

    target = write_memory(args.output_dir, session_id, first_ts,
                          derived, memo, haiku_body, transcript_redacted)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)", file=sys.stderr)
    print(target)  # path on stdout for the publish wrapper


if __name__ == '__main__':
    main()
