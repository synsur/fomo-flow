#!/usr/bin/env python3
"""@profphet / fohomo: the contract's five calls. Shapes: feed + social. Nothing operates yet (PLAN.md).

The first run surfaces the plan's own prerequisites as queue items. Records live under kontrol_data/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KDIR = ROOT / "kontrol_data"
STATE = KDIR / "state.json"
POSTS = KDIR / "posts.jsonl"
ONBOARDED = "2026-09-13"

ITEMS = [
    {"id": "confirm-intake", "gate": "confirm-intake", "kind": "decision",
     "summary": "Intake for @profphet was not answered; kontrol assumed from PLAN.md: nothing paused, nothing running, no records to freeze, everything delegable except spend and referral terms. Confirm or correct.",
     "options": ["confirm", "correct"], "instructions": "decide <id> confirm, or correct --note '...'"},
    {"id": "referral-terms", "gate": "referral-terms", "kind": "manual-step",
     "summary": "Verify the fomo.family referral programme: rate, spot vs perps, recurring or time-boxed, attribution, payout asset and cadence, and whether promoting with a link or publishing on-chain data about users is restricted (PLAN.md §7). Then put the link in .env as REFERRAL_LINK.",
     "options": ["done", "defer"], "evidence": ["PLAN.md"]},
    {"id": "telegram-channel", "gate": "telegram-channel", "kind": "manual-step",
     "summary": "Create the Telegram channel for the feed and a bot with posting rights; put the token in .env as TELEGRAM_BOT_TOKEN.",
     "options": ["done", "defer"], "evidence": ["PLAN.md"]},
    {"id": "reference-accounts", "gate": "reference-accounts", "kind": "decision",
     "summary": "The ten reference accounts that set the post mix and formats for @profphet (decision 2026-09-12 in PLAN.md). Reply with handles.",
     "options": ["set"], "instructions": "decide <id> set --note '@a, @b, ...'"},
]


def now():
    return datetime.now(UTC)


def iso(d):
    return d.isoformat(timespec="seconds") if d else None


def emit(obj, code=0):
    print(json.dumps(obj, default=str))
    sys.exit(code)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"onboarded_at": ONBOARDED, "items": {}, "queue": [], "seq": 0, "raised": False, "answers": {}}


def save_state(s):
    KDIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2, sort_keys=True, default=str) + "\n")


def holds():
    try:
        return {h.get("scope") for h in json.loads(os.environ.get("KONTROL_HOLDS") or "[]")}
    except json.JSONDecodeError:
        return set()


def status(s):
    emit({
        "data": "unknown", "reconciled_at": None, "needs_human": len(s["queue"]),
        "alive": False, "last_heartbeat": None, "emitted_24h": 0, "dead_man_armed": False, "limit_headroom": None,
        "accounts": [{"id": "x", "handle": "@profphet", "last_post_at": None, "followers": None, "pending_replies": None},
                     {"id": "telegram", "handle": s.get("answers", {}).get("telegram-channel") or "(pending)", "last_post_at": None, "followers": None, "pending_replies": None}],
        "buffer_days": 0.0, "buffer_items": 0, "last_published_at": None, "cadence_7d": 0, "on_track": None,
        "cost_30d_usd": 0.0, "streak_note": "not started",
        "notes": ["feed process not built (PLAN.md A1-A5)", "no X posts recorded; baseline not captured"],
    }, 2 if s["queue"] else 0)


def enrich(q):
    """Card fields (CONTRACT 7.3a)."""
    gate = q.get("gate")
    if gate == "confirm-intake":
        q.update({"title": "Confirm how @profphet was set up",
                  "why": "You didn't answer its intake, so kontrol assumed from PLAN.md: nothing paused, nothing running yet, "
                         "everything delegable except spend and referral terms.",
                  "ask": {"choice": "correct", "prompt": "What should change?"}})
    elif gate == "referral-terms":
        q.update({"title": "Check the fomo.family referral terms", "minutes": 10,
                  "how": "In the app: the referral rate, whether it covers spot and perps, how long it lasts, and whether promoting "
                         "with a link is allowed. Then add the link to the project's .env on the PC as REFERRAL_LINK."})
    elif gate == "telegram-channel":
        q.update({"title": "Create @profphet's Telegram channel and bot", "minutes": 5,
                  "how": "In Telegram: create the channel, create a bot with @BotFather, make the bot an admin of the channel, "
                         "then add its token to the project's .env on the PC as TELEGRAM_BOT_TOKEN."})
    return q


def queue(s):
    emit([enrich(dict(q)) for q in s["queue"]], 2 if s["queue"] else 0)


def run(s, live, action):
    gates, actions = [], []
    if not s.get("raised"):
        for it in ITEMS:
            s["seq"] += 1
            q = {"id": f"q_{s['seq']:04d}", "gate": it["gate"], "kind": it["kind"], "summary": it["summary"], "options": it.get("options", []),
                 "evidence": it.get("evidence", []), "instructions": it.get("instructions", ""), "raised_at": iso(now()), "expires_at": None,
                 "if_unanswered": "wait", "delegable": False, "milestone": it["id"]}
            s["queue"].append(q)
            gates.append(q["id"])
        s["raised"] = True
    actions.append({"kind": "feed", "target": "fomo_feed.py", "external": False, "evidence": ["PLAN.md"], "note": "process not built; nothing to ensure"})
    save_state(s)
    emit({"run_id": os.environ.get("KONTROL_RUN_ID"), "started_at": iso(now()), "finished_at": iso(now()), "dry_run": not live,
          "actions": actions, "gates_raised": gates, "holds_raised": [], "cost_usd": 0.0, "errors": [],
          "next_suggested_run": iso(now() + timedelta(days=1))}, 2 if gates else 0)


def decide(s, item_id, choice, note):
    q = next((x for x in s["queue"] if x["id"] == item_id), None)
    if q is None:
        print(f"unknown queue item {item_id}", file=sys.stderr)
        sys.exit(1)
    s.setdefault("answers", {})[q.get("milestone", q["gate"])] = note or choice
    s["queue"] = [x for x in s["queue"] if x["id"] != item_id]
    save_state(s)
    emit({"ok": True, "item": item_id, "choice": choice, "applied_at": iso(now())})


def learn(s):
    emit({"ok": True, "updated": 0, "metrics_at": None, "note": "no channel and no posts yet; the weekly scorecard (PLAN.md C) starts when the first piece ships"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("verb", choices=["status", "queue", "run", "decide", "learn"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--action")
    ap.add_argument("--note")
    a = ap.parse_args()
    s = load_state()
    if a.verb == "status":
        status(s)
    elif a.verb == "queue":
        queue(s)
    elif a.verb == "run":
        run(s, a.live, a.action)
    elif a.verb == "decide":
        if len(a.args) < 2:
            emit({"error": "decide <item> <choice>"}, 1)
        decide(s, a.args[0], a.args[1], a.note)
    elif a.verb == "learn":
        learn(s)


if __name__ == "__main__":
    main()
