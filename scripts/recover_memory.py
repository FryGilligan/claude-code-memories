#!/usr/bin/env python3
"""recover_memory.py — recover a session whose extraction failed with packed XML.

When Haiku occasionally returns the structured fields packed as XML inside one
string (instead of as proper JSON arrays), `extract_memory.py` hard-rejects it
to avoid corrupt output. This tool re-runs the extraction, structurally
unpacks the XML on failure, and writes a published memory to
~/memories/memories/YYYY-MM-DD/HH-MM-SS.md.

Two packed shapes are handled:

  A) all-into-one — every field is packed into ONE str (usually `topics`):
       "topics": "<item>...</item>\\n</topics>\\n
                  <parameter name=\\"tags\\"><item>...</item></tags>\\n
                  <parameter name=\\"tools_table\\">
                    <item><path>...</path><purpose>...</purpose></item>
                  </tools_table>\\n
                  <parameter name=\\"decisions\\"><item>...</item></decisions>\\n
                  <parameter name=\\"follow_ups\\"><item>...</item></follow_ups>"

  B) per-field — each str field separately contains `<item>...</item>` blocks:
       "topics":    "<item>...</item><item>...</item>"
       "decisions": "<item>...</item><item>...</item>"
       (etc.)

If the JSONL no longer exists, we report skipped (Claude Code expires old
session logs; without raw events there is nothing to recover).

Usage:
  recover_memory.py --session-id <uuid>
  recover_memory.py --jsonl <path>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract_memory as em  # noqa: E402


# ── unpack shape A: everything packed into one str ────────────────────────
# Each named-parameter block terminates on its proper closer OR on the next
# <parameter name="..."> OR on end-of-string. The model frequently runs out
# of budget mid-pack and emits a block with no closing tag.
_TERMINATOR = r'(?=</(?:topics|tags|tools_table|decisions|follow_ups)>|<parameter\s+name=|\Z)'
_ENVELOPE_PATTERNS = {
    'topics':      r'^(.*?)' + _TERMINATOR,
    'tags':        r'<parameter\s+name="tags">\s*(.*?)' + _TERMINATOR,
    'tools_table': r'<parameter\s+name="tools_table">\s*(.*?)' + _TERMINATOR,
    'decisions':   r'<parameter\s+name="decisions">\s*(.*?)' + _TERMINATOR,
    'follow_ups':  r'<parameter\s+name="follow_ups">\s*(.*?)' + _TERMINATOR,
}

_ITEM_RE       = re.compile(r'<item>(.*?)</item>', re.S)
_TOOL_PATH_RE  = re.compile(r'<path>(.*?)</path>', re.S)
_TOOL_PURP_RE  = re.compile(r'<purpose>(.*?)</purpose>', re.S)


def _items(text: str) -> list:
    return [m.group(1).strip() for m in _ITEM_RE.finditer(text or '')]


def _tools(text: str) -> list:
    out = []
    for m in _ITEM_RE.finditer(text or ''):
        inner = m.group(1)
        path_m = _TOOL_PATH_RE.search(inner)
        purp_m = _TOOL_PURP_RE.search(inner)
        if path_m:
            out.append({
                'path': path_m.group(1).strip(),
                'purpose': (purp_m.group(1).strip() if purp_m else ''),
            })
    return out


def _unpack_shape_a(memo: dict) -> bool:
    """If any str field contains the full structured-XML packing, expand all
    sibling fields out of it. Returns True if we found shape A."""
    for carrier in ('topics', 'tags', 'decisions', 'follow_ups', 'tools_table'):
        v = memo.get(carrier)
        if not isinstance(v, str):
            continue
        if '<parameter name=' not in v and '</topics>' not in v:
            continue
        # Found shape A. Carrier itself uses its own envelope pattern; siblings
        # use their named-parameter envelopes inside the carrier's content.
        for field, pat in _ENVELOPE_PATTERNS.items():
            m = re.search(pat, v, re.S)
            if not m:
                continue
            block = m.group(1)
            if field == 'tools_table':
                memo[field] = _tools(block)
            else:
                memo[field] = _items(block)
        return True
    return False


def _unpack_shape_b(memo: dict) -> list:
    """For any remaining str field that just contains <item>...</item> blocks,
    expand into a list (or tool-rows). Returns the names of unpacked fields."""
    unpacked = []
    for field in ('topics', 'tags', 'decisions', 'follow_ups'):
        v = memo.get(field)
        if isinstance(v, str) and '<item>' in v:
            memo[field] = _items(v)
            unpacked.append(field)
    v = memo.get('tools_table')
    if isinstance(v, str) and '<item>' in v:
        memo['tools_table'] = _tools(v)
        unpacked.append('tools_table')
    return unpacked


def recover_memo(memo: dict) -> dict:
    """Mutate the memo in-place to recover packed-XML shapes A and B.
    Always tries shape A first (handles 'everything in one field') then
    shape B (handles 'each field has <item>...' wrappers). Returns the memo."""
    found_a = _unpack_shape_a(memo)
    unpacked_b = _unpack_shape_b(memo)
    if not (found_a or unpacked_b):
        return memo  # nothing recognisable to unpack
    # Make sure every required list field exists as a list.
    for field in ('topics', 'tags', 'decisions', 'follow_ups'):
        if not isinstance(memo.get(field), list):
            memo[field] = []
    if not isinstance(memo.get('tools_table'), list):
        memo['tools_table'] = []
    return memo


# ── extraction + recovery flow ────────────────────────────────────────────
def call_with_recovery(api_key: str, transcript: str) -> tuple:
    """Run extract_memory.call_haiku and normalize; on packed-XML SystemExit,
    rerun with recovery. Returns (memo, was_recovered: bool)."""
    memo = em.call_haiku(api_key, transcript)
    try:
        em.normalize_memo(memo)
        return memo, False
    except SystemExit as exc:
        msg = str(exc) or ""
        if "packed structured XML" not in msg:
            raise
    # Packed-XML path: unpack and retry normalize.
    recover_memo(memo)
    em.normalize_memo(memo)
    return memo, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session-id', help='Session UUID')
    ap.add_argument('--jsonl', help='Path to a specific JSONL file')
    ap.add_argument('--output-dir', type=Path, default=em.DEFAULT_OUTPUT_DIR,
                    help='Default: ~/memories/memories (publishes directly).')
    ap.add_argument('--projects-dir', type=Path, default=em.PROJECTS_DIR)
    ap.add_argument('--dry-run', action='store_true',
                    help='Print the recovered memo + body to stdout; do not write.')
    args = ap.parse_args()

    env = em.load_env(em.ANTHROPIC_ENV)
    api_key = env.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise SystemExit(f"ANTHROPIC_API_KEY not found in {em.ANTHROPIC_ENV}")

    if args.jsonl:
        jsonl_path = Path(args.jsonl)
    elif args.session_id:
        jsonl_path = args.projects_dir / f"{args.session_id}.jsonl"
    else:
        raise SystemExit("must supply --session-id or --jsonl")

    if not jsonl_path.exists():
        # Claude Code expires older session logs; without the JSONL there is
        # no raw transcript to re-extract from. This is an unrecoverable
        # failure — report and exit non-zero so callers (e.g. the Discord
        # briefing) can mark it as dead.
        raise SystemExit(f"JSONL missing: {jsonl_path} (session is unrecoverable)")

    session_id = jsonl_path.stem
    print(f"reading {jsonl_path} ({jsonl_path.stat().st_size:,} bytes)", file=sys.stderr)
    events = em.load_session(jsonl_path)
    print(f"  → {len(events)} events", file=sys.stderr)

    transcript, first_ts, _, derived = em.build_transcript(events)
    if not transcript:
        raise SystemExit("transcript is empty — nothing to recover")
    print(f"  → transcript {len(transcript):,} chars, "
          f"duration ≈ {derived['duration_mins']} min", file=sys.stderr)

    transcript_redacted = em.redact(transcript)
    transcript_for_haiku = em.truncate_for_haiku(transcript_redacted)
    print(f"calling {em.MODEL} (input ≈ {len(transcript_for_haiku) // 4:,} tokens)",
          file=sys.stderr)

    memo, recovered = call_with_recovery(api_key, transcript_for_haiku)
    if recovered:
        print("  → recovered packed-XML response (structural unpack)", file=sys.stderr)
    else:
        print("  → extraction succeeded without recovery (no packed-XML this run)",
              file=sys.stderr)

    body = em.redact(em.assemble_body(memo))

    if args.dry_run:
        sys.stdout.write(em.build_frontmatter(session_id, first_ts, derived, memo))
        sys.stdout.write(body.rstrip())
        sys.stdout.write("\n")
        return

    target = em.write_memory(args.output_dir, session_id, first_ts,
                             derived, memo, body, transcript_redacted)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)", file=sys.stderr)
    print(target)


if __name__ == '__main__':
    main()
