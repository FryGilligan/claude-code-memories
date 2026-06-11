# claude-code-memories

Capture every Claude Code session you finish, distil it into a structured
markdown memory, optionally review it through a Discord bot, and publish the
approved ones to a searchable static site.

![placeholder screenshot of the published site](docs/screenshot.png)

## What it does

1. When a Claude Code session ends, a `SessionEnd` hook runs `auto_draft.sh`.
2. The script feeds the session JSONL through Claude Haiku 4.5 (forced tool
   use) and writes a structured draft to `memories/_drafts/YYYY-MM-DD/HH-MM-SS.md`.
3. (Optional) A Discord bot posts each draft for review. React `✅` to publish,
   `❌` to reject.
4. Published drafts move to `memories/YYYY-MM-DD/` and get committed + pushed.
5. An 11ty static site (`site/`) turns the published memories into a calendar
   you can browse, navigate with `j`/`k`, and search via Pagefind.
6. (Optional) A weekly Discord briefing surfaces pending drafts and any
   failures, with one-line recovery commands per failure.

Everything is stdlib Python + Node devDependencies only. No external services
besides Anthropic and (if you want it) Discord.

See `docs/ARCHITECTURE.md` for the flow diagram and `docs/REDACTION.md` for the
secret-scrubbing rules.

## Requirements

- Linux or macOS (the scripts use bash + `python3` from the stdlib only).
- Python 3.10+ (no pip install needed for the scripts themselves).
- Node 20+ if you want to build the site.
- An Anthropic API key.
- Claude Code installed and writing session JSONLs under `~/.claude/projects/`.
- (Optional) A Discord bot token + channel ID for the review layer.

## Install

### 1. Clone the repo

```bash
git clone https://github.com/FryGilligan/claude-code-memories.git
cd claude-code-memories
```

### 2. Set your Anthropic API key

The extractor reads the key from `~/.config/anthropic/.env` by default:

```bash
mkdir -p ~/.config/anthropic
printf 'ANTHROPIC_API_KEY=sk-ant-...\n' > ~/.config/anthropic/.env
chmod 600 ~/.config/anthropic/.env
```

Or copy `.env.example` to `.env` in the repo root and set `ANTHROPIC_ENV` to
point at it.

### 3. Test the extractor

```bash
python3 scripts/extract_memory.py
```

This pulls your most recent Claude Code session, redacts secrets, calls Haiku,
and writes `memories/YYYY-MM-DD/HH-MM-SS.md`. Open it and check the shape.

If the script can't find your session JSONLs, set `CLAUDE_PROJECTS_DIR` to
their location (typically `~/.claude/projects/<sanitised-cwd>/`).

### 4. Wire up the SessionEnd hook

Copy the example into your Claude Code settings so a new memory drafts itself
every time you end a session:

```bash
# Inspect first — it just runs scripts/auto_draft.sh on SessionEnd.
cat hooks/session-end.example.json
```

Add the contents to your Claude Code `settings.json` (`~/.claude/settings.json`
or `.claude/settings.json` in a project), pointing the script path at your
clone.

Drafts now land in `memories/_drafts/` automatically.

### 5. (Optional) Build the static site

```bash
cd site
npm install
npm run build
# Serves at http://localhost:8080
npm run dev
```

Output goes to `site/_site/`. Deploy that directory wherever you like:
GitHub Pages, Cloudflare Pages, Netlify, plain rsync to a VPS, etc.

### 6. (Optional) Discord review bot

```bash
cp bot/bot.env.example bot/bot.env
chmod 600 bot/bot.env
$EDITOR bot/bot.env          # fill in DISCORD_BOT_TOKEN, CHANNEL_ID, OWNER_USER_ID

# Test once
python3 bot/memories_bot.py --dry-run

# Run on a timer (systemd example included)
$EDITOR systemd/memories-bot.service  # adjust paths to your clone
sudo cp systemd/memories-bot.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memories-bot.timer
```

### 7. (Optional) Weekly Monday briefing

Same shape as the bot — a separate systemd timer:

```bash
$EDITOR systemd/memories-briefing.service  # replace REPLACE_USER + REPLACE_REPO_PATH
sudo cp systemd/memories-briefing.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memories-briefing.timer
```

## Layout

```
claude-code-memories/
  scripts/         extractor, dedup, packed-XML recovery, git push
  bot/             Discord review bot + weekly briefing (stdlib only)
  site/            11ty static site + Pagefind search
  systemd/         optional timer units for bot + briefing
  hooks/           Claude Code SessionEnd hook example
  docs/            architecture + redaction notes
  memories/        your drafts + published memories (gitignored by default)
```

## Recovery

If Haiku occasionally returns its structured fields packed as XML inside one
string, `extract_memory.py` rejects it (rather than guessing). To recover:

```bash
python3 scripts/recover_memory.py --session-id <uuid>
```

The weekly briefing will print one of these commands per recoverable failure.

## License

MIT. See `LICENSE`.
