#!/usr/bin/env bash
# auto_draft.sh — SessionEnd hook: write a redacted memory draft so nothing
# is lost if the user forgets to run /publish-memories. Drafts land in
# ~/memories/memories/_drafts/ (gitignored). Never blocks session end.
#
# Reads a JSON payload on stdin (Claude Code SessionEnd hook), expects
# either {"session_id": "..."} or {"transcript_path": "/path/to/file.jsonl"}.

set -uo pipefail

REPO_DIR="${MEMORIES_REPO:-$HOME/memories}"
DRAFT_DIR="$REPO_DIR/memories/_drafts"
LOG="$DRAFT_DIR/.auto_draft.log"
RETAIN_DAYS="${MEMORIES_DRAFT_RETAIN_DAYS:-30}"

mkdir -p "$DRAFT_DIR"

# Prune drafts older than RETAIN_DAYS so this dir doesn't grow forever.
find "$DRAFT_DIR" -type f -name '*.md' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null
find "$DRAFT_DIR" -type d -empty -not -path "$DRAFT_DIR" -delete 2>/dev/null

# Read whatever the hook sends; ignore parse failures.
payload="$(cat || true)"

session_id=""
jsonl_path=""

if command -v jq >/dev/null 2>&1; then
  session_id="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)"
  jsonl_path="$(printf '%s' "$payload" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
else
  # Crude fallback: regex extract.
  session_id="$(printf '%s' "$payload" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  jsonl_path="$(printf '%s' "$payload" | grep -oE '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*"([^"]+)"$/\1/' || true)"
fi

args=(--output-dir "$DRAFT_DIR")
if [[ -n "$jsonl_path" && -f "$jsonl_path" ]]; then
  args+=(--jsonl "$jsonl_path")
elif [[ -n "$session_id" ]]; then
  args+=(--session-id "$session_id")
fi
# else: extractor falls back to latest JSONL

# Derive a session_id for dedup. Explicit wins; otherwise it's the JSONL stem.
dedup_sid=""
if [[ -n "$session_id" ]]; then
  dedup_sid="$session_id"
elif [[ -n "$jsonl_path" ]]; then
  dedup_sid="$(basename "$jsonl_path" .jsonl)"
fi

# Run detached so session end is never blocked. Log to file for forensics.
{
  echo "=== $(date -Iseconds) auto_draft args=${args[*]} ==="
  # Dedup: skip if this session is already published. Long-running resumed
  # sessions otherwise re-extract on every SessionEnd, producing duplicate
  # drafts and (when the transcript has grown) burning input tokens — a 5500-
  # min resumed session reached 120K input tokens and tripped a rate limit
  # on 2026-05-27, producing a phantom failure. _drafts/ is excluded from
  # the match so a previously-drafted-but-unpublished session still re-runs.
  if [[ -n "$dedup_sid" ]] \
     && grep -lr "session_id: \"$dedup_sid\"" "$REPO_DIR/memories" 2>/dev/null \
        | grep -v '/_drafts/' | grep -q .; then
    echo "dedup: session $dedup_sid already published — skipping extraction"
    echo "exit=0"
  else
    python3 "$REPO_DIR/scripts/extract_memory.py" "${args[@]}" 2>&1
    echo "exit=$?"
  fi
} >>"$LOG" 2>&1 </dev/null &
disown || true

exit 0
