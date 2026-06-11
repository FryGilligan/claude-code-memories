#!/usr/bin/env python3
"""
memories-bot — Discord approve/reject for session memory drafts.

Driven by a systemd timer (oneshot, ~every 30 minutes). Each run:
  1. Reconciles previously-posted drafts: looks up Discord reactions on the
     pending messages, and for each owner-reacted message either publishes
     the draft (git commit + push to the memories source repo) or moves it
     to _drafts/_rejected/.
  2. Announces newly-arrived drafts: posts an embed for each draft not yet
     in state.json and pre-adds the ✅ / ❌ reactions so the owner can
     one-tap from mobile.

Env (loaded by systemd EnvironmentFile=bot.env):
  DISCORD_BOT_TOKEN, CHANNEL_ID, OWNER_USER_ID,
  MEMORIES_REPO, STATE_FILE, LOG_FILE, SKIP_TRIVIAL, SCRUB_EMBEDS

Flags:
  --dry-run    log intended actions without posting / publishing.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ── env ───────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID        = os.environ.get("CHANNEL_ID", "").strip()
OWNER_USER_ID     = os.environ.get("OWNER_USER_ID", "").strip()
# Default to <repo-root> = two levels up from this file (bot/memories_bot.py).
_DEFAULT_REPO     = Path(__file__).resolve().parent.parent
MEMORIES_REPO     = Path(os.environ.get("MEMORIES_REPO", str(_DEFAULT_REPO)))
STATE_FILE        = Path(os.environ.get(
    "STATE_FILE", str(Path(__file__).resolve().parent / "state.json")))
LOG_FILE          = Path(os.environ.get(
    "LOG_FILE", str(Path(__file__).resolve().parent / "bot.log")))
SKIP_TRIVIAL      = os.environ.get("SKIP_TRIVIAL", "false").lower() == "true"
SCRUB_EMBEDS      = os.environ.get("SCRUB_EMBEDS", "true").lower() == "true"
# Comma-separated suffix list (e.g. "example.com,example.org"). When SCRUB_EMBEDS
# is true, any hostname ending in one of these is replaced with <host> in the
# embed text. Memory file on disk is untouched — scrub is embed-only.
SCRUB_HOST_SUFFIXES = [
    h.strip().lower() for h in os.environ.get("SCRUB_HOST_SUFFIXES", "").split(",")
    if h.strip()
]

DRAFTS_DIR   = MEMORIES_REPO / "memories" / "_drafts"
PUBLIC_DIR   = MEMORIES_REPO / "memories"
REJECTED_DIR = DRAFTS_DIR / "_rejected"

DISCORD_API = "https://discord.com/api/v10"

# Discord reaction emoji — source kept ASCII; Python decodes the escapes at
# runtime so the bytes sent to the Discord API are unchanged.
CHECK = "✅"  # white heavy check mark
CROSS = "❌"  # cross mark

DRY_RUN = "--dry-run" in sys.argv


# ── logging ───────────────────────────────────────────────────────────────
def log(msg, level="info"):
    line = f"{datetime.now().isoformat(timespec='seconds')} [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── http (stdlib) ─────────────────────────────────────────────────────────
def discord_request(method, path, body=None):
    url = DISCORD_API + path
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": "memories-bot (self-hosted, 0.1)",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else None


# ── state ─────────────────────────────────────────────────────────────────
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError as e:
        log(f"state.json corrupt: {e}; starting fresh", "warn")
        return {}


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


# ── drafts ────────────────────────────────────────────────────────────────
def find_drafts():
    if not DRAFTS_DIR.exists():
        return []
    out = []
    for p in DRAFTS_DIR.rglob("*.md"):
        try:
            p.relative_to(REJECTED_DIR)
            continue
        except ValueError:
            pass
        out.append(p)
    return sorted(out)


def parse_frontmatter(path):
    txt = path.read_text(encoding="utf-8", errors="replace")
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    meta = {}
    for line in txt[3:end].strip().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip('"')
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = ([item.strip().strip('"') for item in inner.split(",")]
                       if inner else [])
        elif v.lower() in ("true", "false"):
            meta[k] = v.lower() == "true"
        else:
            meta[k] = v
    return meta


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


# ── scrubber ──────────────────────────────────────────────────────────────
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Built from SCRUB_HOST_SUFFIXES (env). Empty list → no host scrubbing.
HOST_RE = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:"
    + "|".join(re.escape(s) for s in SCRUB_HOST_SUFFIXES)
    + r")\b", re.IGNORECASE) if SCRUB_HOST_SUFFIXES else None


def scrub(text):
    if not SCRUB_EMBEDS or not text:
        return text
    text = IP_RE.sub("<ip>", text)
    if HOST_RE is not None:
        text = HOST_RE.sub("<host>", text)
    return text


# ── filters ───────────────────────────────────────────────────────────────
def is_trivial(meta):
    if not SKIP_TRIVIAL:
        return False
    try:
        dur = int(meta.get("duration_mins", 0))
    except (TypeError, ValueError):
        dur = 0
    return dur < 1 and not (meta.get("tools_produced") or [])


# ── discord ops ───────────────────────────────────────────────────────────
def post_embed(meta, summary, draft_path):
    date  = meta.get("date", "")
    time_ = meta.get("time", "")
    dur   = meta.get("duration_mins", "")
    tags  = meta.get("tags") or []
    tools = meta.get("tools_produced") or []

    fields = []
    if dur != "":
        fields.append({"name": "duration", "value": f"{dur} min", "inline": True})
    if tools:
        fields.append({"name": "tools", "value": f"{len(tools)} produced",
                       "inline": True})
    if tags:
        fields.append({"name": "tags", "value": ", ".join(tags[:8]),
                       "inline": False})
    fields.append({"name": "draft",
                   "value": f"`{draft_path.relative_to(MEMORIES_REPO)}`",
                   "inline": False})

    embed = {
        "title": f"memory draft | {date} {time_}",
        "description": scrub(summary) or "(no summary)",
        "color": 0x58A6FF,
        "fields": fields,
        "footer": {"text": f"react {CHECK} to publish | {CROSS} to reject"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if DRY_RUN:
        log(f"[dry-run] would post embed for {draft_path.name}: "
            f"{embed['description'][:80]}")
        return None

    msg = discord_request("POST", f"/channels/{CHANNEL_ID}/messages",
                          {"embeds": [embed]})
    msg_id = msg["id"]
    for emoji in (CHECK, CROSS):
        encoded = urllib.parse.quote(emoji)
        try:
            discord_request(
                "PUT",
                f"/channels/{CHANNEL_ID}/messages/{msg_id}/reactions/{encoded}/@me",
            )
            time.sleep(0.4)
        except Exception as e:
            log(f"failed to add {emoji} on msg {msg_id}: {e}", "warn")
    return msg_id


def reaction_users(msg_id, emoji):
    encoded = urllib.parse.quote(emoji)
    try:
        users = discord_request(
            "GET",
            f"/channels/{CHANNEL_ID}/messages/{msg_id}/reactions/{encoded}?limit=100",
        )
        return [u["id"] for u in (users or [])]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


# ── publish / reject ──────────────────────────────────────────────────────
def commit_subject(memory_path):
    date_part = memory_path.parent.name
    time_part = memory_path.stem
    hour_min = time_part.rsplit("-", 1)[0].replace("-", ":")
    summary = summary_first_line(memory_path)
    short = summary.split(". ", 1)[0]
    if short and not short.endswith("."):
        short += "."
    if len(short) > 80:
        short = short[:77] + "..."
    base = f"memory: {date_part} {hour_min}"
    return f"{base} - {short}" if short else base


def approve(draft_path):
    rel  = draft_path.relative_to(DRAFTS_DIR)
    dest = PUBLIC_DIR / rel

    if DRY_RUN:
        log(f"[dry-run] would copy {draft_path} → {dest} and git commit/push")
        return

    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(draft_path, dest)

    rel_to_repo = str(dest.relative_to(MEMORIES_REPO))
    subprocess.run(["git", "add", "--", rel_to_repo],
                   cwd=MEMORIES_REPO, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=MEMORIES_REPO)
    if diff.returncode == 0:
        log(f"approve: nothing to commit for {rel_to_repo}", "warn")
        return
    subj = commit_subject(dest)
    subprocess.run(
        ["git", "commit", "-m", subj,
         "-m", "Co-Authored-By: memories-bot <noreply@anthropic.com>"],
        cwd=MEMORIES_REPO, check=True,
    )
    subprocess.run(["git", "push"], cwd=MEMORIES_REPO, check=True)
    log(f"approved + pushed: {rel_to_repo}")


def reject(draft_path):
    rel  = draft_path.relative_to(DRAFTS_DIR)
    dest = REJECTED_DIR / rel
    if DRY_RUN:
        log(f"[dry-run] would mv {draft_path} → {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(draft_path), str(dest))
    log(f"rejected: {draft_path.relative_to(MEMORIES_REPO)} → _rejected/")


# ── reconcile + announce ──────────────────────────────────────────────────
def reconcile(state):
    for draft_str, entry in list(state.items()):
        if entry.get("status") != "pending":
            continue
        msg_id = entry.get("msg_id")
        if not msg_id:
            continue
        draft_path = Path(draft_str)
        if not draft_path.exists():
            log(f"draft missing: {draft_str}; marking expired", "warn")
            entry["status"] = "expired"
            continue
        try:
            check_users = reaction_users(msg_id, CHECK)
            cross_users = reaction_users(msg_id, CROSS)
        except Exception as e:
            log(f"reconcile failed for msg {msg_id}: {e}", "error")
            continue

        owner_check = OWNER_USER_ID in check_users
        owner_cross = OWNER_USER_ID in cross_users

        if owner_check and owner_cross:
            log(f"both ✅ and ❌ from owner on msg {msg_id}; leaving pending",
                "warn")
            continue
        if owner_check:
            try:
                approve(draft_path)
                entry["status"] = "approved"
                entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                log(f"approve failed for {draft_str}: {e}", "error")
        elif owner_cross:
            try:
                reject(draft_path)
                entry["status"] = "rejected"
                entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                log(f"reject failed for {draft_str}: {e}", "error")


def announce(state):
    for draft_path in find_drafts():
        key = str(draft_path)
        if key in state:
            continue
        # Skip drafts whose mirror already exists in the public tree — they
        # were published manually (or by an earlier bot run that wasn't
        # tracked) and shouldn't be re-announced.
        rel = draft_path.relative_to(DRAFTS_DIR)
        if (PUBLIC_DIR / rel).exists():
            log(f"skip already-published: {key}")
            state[key] = {
                "msg_id": None,
                "status": "already-published",
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            continue
        meta = parse_frontmatter(draft_path)
        if is_trivial(meta):
            log(f"skip trivial: {key}")
            state[key] = {
                "msg_id": None,
                "status": "skipped",
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            continue
        summary = summary_first_line(draft_path)
        try:
            msg_id = post_embed(meta, summary, draft_path)
        except Exception as e:
            log(f"post_embed failed for {key}: {e}", "error")
            continue
        state[key] = {
            "msg_id": msg_id,
            "status": "pending" if msg_id else "dry-run",
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }


# ── main ──────────────────────────────────────────────────────────────────
def main():
    if not (DISCORD_BOT_TOKEN and CHANNEL_ID and OWNER_USER_ID):
        log("missing env (DISCORD_BOT_TOKEN/CHANNEL_ID/OWNER_USER_ID)", "error")
        return 2
    log(f"start (dry_run={DRY_RUN}, scrub={SCRUB_EMBEDS}, "
        f"skip_trivial={SKIP_TRIVIAL})")
    state = load_state()
    reconcile(state)
    announce(state)
    save_state(state)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
