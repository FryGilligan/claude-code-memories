#!/usr/bin/env python3
"""
briefing.py — Monday-morning Discord status digest for the memories pipeline.

Reads:
  - ~/memories/memories/_drafts/.auto_draft.log  (failures, phantoms)
  - ~/memories/memories/_drafts/                 (pending drafts)
  - ~/memories/memories/YYYY-MM-DD/              (published, for dedup)
  - ~/platform/apps/memories/bot/state.json      (Discord msg_ids for deep links)

Posts a single embed to the same CHANNEL_ID the per-draft bot uses (shared
bot.env). Runs on a Monday 08:00 systemd timer.

Flags:
  --dry-run    print the embed to stdout, do not POST
  --since N    look back N days instead of 7
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── env (shared bot.env) ──────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID        = os.environ.get("CHANNEL_ID", "").strip()
GUILD_ID          = os.environ.get("DISCORD_GUILD_ID", "").strip()  # optional, for deep links
# Default to <repo-root>, which is two levels up from this file (bot/briefing.py).
_DEFAULT_REPO     = Path(__file__).resolve().parent.parent
MEMORIES_REPO     = Path(os.environ.get("MEMORIES_REPO", str(_DEFAULT_REPO)))
STATE_FILE        = Path(os.environ.get(
    "STATE_FILE", str(Path(__file__).resolve().parent / "state.json")))
# Where Claude Code stores session JSONLs. Override if your sessions live
# under a non-standard projects dir (Claude Code names dirs after cwd).
CLAUDE_PROJECTS_DIR = Path(os.environ.get(
    "CLAUDE_PROJECTS_DIR",
    str(Path.home() / ".claude" / "projects"
        / ("-" + str(Path.home()).lstrip("/").replace("/", "-")))))

DRAFTS_DIR   = MEMORIES_REPO / "memories" / "_drafts"
PUBLIC_DIR   = MEMORIES_REPO / "memories"
LOG_FILE     = DRAFTS_DIR / ".auto_draft.log"
REJECTED_DIR = DRAFTS_DIR / "_rejected"

DISCORD_API = "https://discord.com/api/v10"
DRY_RUN     = "--dry-run" in sys.argv

SINCE_DAYS = 7
for i, a in enumerate(sys.argv):
    if a == "--since" and i + 1 < len(sys.argv):
        try:
            SINCE_DAYS = int(sys.argv[i + 1])
        except ValueError:
            pass


# ── log parsing ───────────────────────────────────────────────────────────
RUN_HDR = re.compile(r'^=== (\S+) auto_draft args=(.+) ===$')
SID_FROM_ARGS = re.compile(r'(?:--session-id\s+|--jsonl\s+\S*/)([0-9a-f-]{36})')


def parse_log_runs(since_cutoff):
    """Yield {ts, session_id, exit, cause} for each run since cutoff."""
    if not LOG_FILE.exists():
        return
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r'^(=== \S+ auto_draft args=.+ ===)$', text, flags=re.M)
    # blocks: [pre, hdr1, body1, hdr2, body2, ...]
    for hdr, body in zip(blocks[1::2], blocks[2::2]):
        m = RUN_HDR.match(hdr)
        if not m:
            continue
        ts_str, args = m.group(1), m.group(2)
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < since_cutoff:
            continue
        sid_m = SID_FROM_ARGS.search(args)
        sid = sid_m.group(1) if sid_m else None
        exit_m = re.search(r'^exit=(\d+)', body, re.M)
        exit_code = int(exit_m.group(1)) if exit_m else None
        cause = "ok"
        if exit_code != 0:
            if "packed structured XML" in body:
                cause = "packed-xml"
            elif "429" in body or "rate_limit" in body:
                cause = "rate-limit"
            else:
                cause = "other"
        elif "dedup: session" in body:
            cause = "dedup-skip"
        yield {"ts": ts, "session_id": sid, "exit": exit_code, "cause": cause}


# ── draft + dedup helpers ─────────────────────────────────────────────────
def find_pending_drafts():
    """Drafts in _drafts/ that have no published mirror in memories/."""
    if not DRAFTS_DIR.exists():
        return []
    pending = []
    for p in sorted(DRAFTS_DIR.rglob("*.md")):
        try:
            p.relative_to(REJECTED_DIR)
            continue
        except ValueError:
            pass
        rel = p.relative_to(DRAFTS_DIR)
        if (PUBLIC_DIR / rel).exists():
            continue
        pending.append(p)
    return pending


def is_session_published(sid):
    """True iff any published memory file references this session_id."""
    if not sid:
        return False
    for p in PUBLIC_DIR.glob("2*/*.md"):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:600]
        except OSError:
            continue
        if f'session_id: "{sid}"' in head:
            return True
    return False


def parse_frontmatter(path):
    txt = path.read_text(encoding="utf-8", errors="replace")
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in txt[3:end].strip().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"')
    return out


def summary_first_line(path):
    in_summary = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s == "## Summary":
            in_summary = True
            continue
        if in_summary:
            if s.startswith("## "):
                break
            if s:
                return s
    return ""


def published_this_week(since_cutoff):
    count = 0
    for p in PUBLIC_DIR.glob("2*/*.md"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= since_cutoff:
            count += 1
    return count


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def msg_link_for_draft(state, draft_path):
    """Deep-link to the per-draft message, when we have one."""
    entry = state.get(str(draft_path), {})
    msg_id = entry.get("msg_id")
    if msg_id and GUILD_ID and CHANNEL_ID:
        return f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{msg_id}"
    return None


# ── embed assembly ────────────────────────────────────────────────────────
def build_embed(now, since_cutoff):
    runs = list(parse_log_runs(since_cutoff))
    state = load_state()

    failures = [r for r in runs if r["exit"] not in (0, None)]
    phantoms_in_failures = [r for r in failures if is_session_published(r["session_id"])]
    real_failures = [r for r in failures if r not in phantoms_in_failures]
    dedup_skips = [r for r in runs if r["cause"] == "dedup-skip"]
    ok_runs = [r for r in runs if r["exit"] == 0 and r["cause"] != "dedup-skip"]

    pending = find_pending_drafts()
    pub_count = published_this_week(since_cutoff)

    # Pending drafts field — up to 8, with deep links when known
    pending_lines = []
    for p in pending[:8]:
        meta = parse_frontmatter(p)
        date = meta.get("date", "")
        time_ = meta.get("time", "")
        dur = meta.get("duration_mins", "?")
        link = msg_link_for_draft(state, p)
        bullet = f"- `{date} {time_}` ({dur}m)"
        if link:
            bullet += f" [draft msg]({link})"
        pending_lines.append(bullet)
    if len(pending) > 8:
        pending_lines.append(f"- … and {len(pending) - 8} more")
    pending_field = "\n".join(pending_lines) if pending_lines else "_none_"

    # Failure field — dedupe by session_id, distinguish recoverable from dead
    seen_sids = set()
    unique_failures = []
    for r in real_failures:
        if r["session_id"] in seen_sids:
            continue
        seen_sids.add(r["session_id"])
        jsonl = (CLAUDE_PROJECTS_DIR / f"{r['session_id']}.jsonl"
                 ) if r["session_id"] else None
        r["recoverable"] = bool(jsonl and jsonl.exists())
        unique_failures.append(r)
    recoverable = [r for r in unique_failures if r["recoverable"]]
    dead        = [r for r in unique_failures if not r["recoverable"]]

    # Emoji kept as \u escapes so source stays ASCII; Python decodes at runtime
    # so Discord receives the same bytes it would for the literal emoji.
    RECYCLE = "♻️"  # recoverable (recycling symbol)
    COFFIN  = "⚰️"  # dead (coffin)
    if unique_failures:
        lines = []
        recover_script = MEMORIES_REPO / "scripts" / "recover_memory.py"
        for r in recoverable[:5]:
            cmd = f"`python3 {recover_script} --session-id {r['session_id']}`"
            lines.append(f"- {RECYCLE} `{r['session_id']}` ({r['cause']}) -> {cmd}")
        for r in dead[:3]:
            lines.append(f"- {COFFIN} `{r['session_id']}` ({r['cause']}) - JSONL expired, unrecoverable")
        extra = len(unique_failures) - len(lines)
        if extra > 0:
            lines.append(f"- ... and {extra} more")
        failure_field = (f"**{len(recoverable)}** recoverable | "
                         f"**{len(dead)}** dead\n" + "\n".join(lines))
    else:
        failure_field = "_none_"

    pipeline_field = (
        f"published: **{pub_count}** | "
        f"ok extractions: **{len(ok_runs)}** | "
        f"dedup skips: **{len(dedup_skips)}** | "
        f"phantom failures suppressed: **{len(phantoms_in_failures)}**"
    )

    embed = {
        "title": "memories weekly briefing",
        "description": (
            f"Pipeline status for the last {SINCE_DAYS} days "
            f"(through {now.strftime('%Y-%m-%d')})."
        ),
        "color": 0x58A6FF,
        "fields": [
            {"name": f"pending drafts ({len(pending)})",
             "value": pending_field, "inline": False},
            {"name": "real failures",
             "value": failure_field, "inline": False},
            {"name": "pipeline",
             "value": pipeline_field, "inline": False},
        ],
        "footer": {"text": (
            f"react ✅/❌ on per-draft messages to publish/reject | "
            f"{RECYCLE} failures are recoverable via the listed command"
        )},
        "timestamp": now.isoformat(),
    }
    return embed


# ── discord post ──────────────────────────────────────────────────────────
def post_embed(embed):
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "memories-briefing (self-hosted, 0.1)",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def main():
    if not DRY_RUN and not (DISCORD_BOT_TOKEN and CHANNEL_ID):
        print("missing DISCORD_BOT_TOKEN/CHANNEL_ID env", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    since_cutoff = now - timedelta(days=SINCE_DAYS)
    embed = build_embed(now, since_cutoff)
    if DRY_RUN:
        print(json.dumps(embed, indent=2, ensure_ascii=False))
        return 0
    try:
        post_embed(embed)
    except urllib.error.HTTPError as e:
        print(f"discord HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}",
              file=sys.stderr)
        return 1
    print("briefing posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
