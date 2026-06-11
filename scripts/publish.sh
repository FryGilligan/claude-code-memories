#!/usr/bin/env bash
# publish.sh — extract a session memory and push it to your memories repo.
#
# Usage:
#   publish.sh                          # latest session
#   publish.sh --session-id <uuid>      # specific session
#   publish.sh --jsonl <path>           # specific file
#
# The extracted memory is written to $MEMORIES_REPO/memories/YYYY-MM-DD/HH-MM-SS.md
# then committed and pushed. Wire up your own deploy (GitHub Pages, Cloudflare
# Pages, rsync, etc.) to publish $MEMORIES_REPO/_site/ after the 11ty build.
set -euo pipefail

# Repo root: env override or the parent dir of this script.
SCRIPT_PATH="$(readlink -f "$0")"
REPO_DIR="${MEMORIES_REPO:-$(dirname "$(dirname "$SCRIPT_PATH")")}"
SCRIPT_DIR="$REPO_DIR/scripts"

cd "$REPO_DIR"

# Run the extractor; it prints the output path on stdout.
output_path="$(python3 "$SCRIPT_DIR/extract_memory.py" "$@")"
if [[ -z "$output_path" || ! -f "$output_path" ]]; then
  echo "publish.sh: extractor did not produce a memory file" >&2
  exit 1
fi

# Path relative to repo root for git
rel_path="${output_path#"$REPO_DIR/"}"

# Stage and commit
git add -- "$rel_path"
if git diff --cached --quiet; then
  echo "publish.sh: no changes to commit ($rel_path already up to date)" >&2
  exit 0
fi

# Format commit subject as: "memory: YYYY-MM-DD HH:MM - <summary first line>"
date_part="$(basename "$(dirname "$rel_path")")"
time_part="$(basename "$rel_path" .md)"
hour_min="${time_part%-*}"; hour_min="${hour_min/-/:}"

# Pull the first sentence of the ## Summary section as a one-line hint.
summary_first_line="$(awk '
  /^## Summary[[:space:]]*$/ { flag=1; next }
  flag && /^## / { exit }
  flag && NF { print; exit }
' "$output_path" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"

# Trim summary line to first sentence (period or 80 chars, whichever first).
if [[ -n "$summary_first_line" ]]; then
  summary_short="${summary_first_line%%. *}"
  [[ "$summary_short" != "$summary_first_line" ]] && summary_short="${summary_short}."
  if [[ ${#summary_short} -gt 80 ]]; then
    summary_short="${summary_short:0:77}..."
  fi
  commit_subject="memory: ${date_part} ${hour_min} - ${summary_short}"
else
  commit_subject="memory: ${date_part} ${hour_min}"
fi
git commit -m "$commit_subject"

# Push (set upstream on first push)
branch="$(git rev-parse --abbrev-ref HEAD)"
if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
  git push
else
  git push -u origin "$branch"
fi

echo "published: $rel_path"
