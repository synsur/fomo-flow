#!/usr/bin/env python3
"""profphet (@profphetic) / fohomo: the contract's five calls. Shapes: feed + social.

The content lane. `run --live` offers a shortlist of candidate posts; the operator taps one;
it becomes a draft to approve; an approved draft becomes a post to publish. Voice, facts and
validation live in `profphet_voice.py` - this file is the state machine and the cards.

Two things it deliberately does not do:

- **It does not write numbers.** Every figure in a data post is computed from the current
  database by `profphet_voice`, handed to the model as a locked fact block, and any candidate
  containing a figure that was not in that block is dropped before the operator ever sees it.
- **It does not post to X.** `kontrol.yaml` declares `accounts.x.posting: manual` and the
  `x-posting-method` research question is still open, so the last step is a manual-step card
  carrying the text and the chart. `publish_ready()` is the single seam where an API path
  plugs in once that question is answered and a credential exists; nothing else moves.

The feed (PLAN.md A1-A5) is still unbuilt; `status` reports that rather than implying it runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import profphet_voice as voice

ROOT = Path(__file__).resolve().parent
KDIR = ROOT / "kontrol_data"
STATE = KDIR / "state.json"
POSTS = KDIR / "posts.jsonl"
DRAFTS = KDIR / "drafts"
ONBOARDED = "2026-09-13"
HANDLE = "profphetic"
SHORTLIST_N = 5
PER_LANE_MAX = 3          # so a shortlist is a choice of angles, not one fact five ways
TARGET_PER_WEEK = 7

ITEMS = [
    {"id": "confirm-intake", "gate": "confirm-intake", "kind": "decision",
     "summary": "Intake for profphet (@profphetic) was not answered; kontrol assumed from PLAN.md: nothing paused, nothing running, no records to freeze, everything delegable except spend and referral terms. Confirm or correct.",
     "options": ["confirm", "correct"], "instructions": "decide <id> confirm, or correct --note '...'"},
    {"id": "referral-terms", "gate": "referral-terms", "kind": "manual-step",
     "summary": "Verify the fomo.family referral programme: rate, spot vs perps, recurring or time-boxed, attribution, payout asset and cadence, and whether promoting with a link or publishing on-chain data about users is restricted (PLAN.md §7). Then put the link in .env as REFERRAL_LINK.",
     "options": ["done", "defer"], "evidence": ["PLAN.md"]},
    {"id": "telegram-channel", "gate": "telegram-channel", "kind": "manual-step",
     "summary": "Create the Telegram channel for the feed and a bot with posting rights; put the token in .env as TELEGRAM_BOT_TOKEN.",
     "options": ["done", "defer"], "evidence": ["PLAN.md"]},
    {"id": "reference-accounts", "gate": "reference-accounts", "kind": "decision",
     "summary": "Reference accounts that set the post mix and formats for profphet (PLAN.md 3B). The ten kontrol decided on 2026-09-13 stand for the data side and the blended voice; the operator added @josbjohnson on 2026-09-16 as the model for the life posts' mechanics. Reply with any change to the list.",
     "options": ["set"], "instructions": "decide <id> set --note '@a, @b, ... (+@josbjohnson for life-post mechanics)'"},
    {"id": "openrouter-key", "gate": "openrouter-key", "kind": "manual-step",
     "summary": "The post generator needs an OpenRouter key to write candidate posts. Without it kontrol still offers a shortlist, but every option is a deterministic template rather than written copy. Put it in the project's .env on the PC as OPENROUTER_API_KEY.",
     "options": ["done", "defer"], "evidence": ["VOICE.md"]},
]


def now():
    return datetime.now(UTC)


def iso(d):
    return d.isoformat(timespec="seconds") if d else None


def emit(obj, code=0):
    print(json.dumps(obj, default=str))
    sys.exit(code)


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_state():
    try:
        s = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {"onboarded_at": ONBOARDED}
    s.setdefault("items", {})
    s.setdefault("queue", [])
    s.setdefault("seq", 0)
    s.setdefault("raised", False)
    s.setdefault("answers", {})
    s.setdefault("passed", [])          # candidate texts shown and not chosen
    s.setdefault("posted_facts", [])    # fact ids already used, so the account does not repeat
    s.setdefault("used_seeds", [])      # note ids already shaped into a post
    return s


def save_state(s):
    KDIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(tmp, STATE)


def holds():
    try:
        return {h.get("scope") for h in json.loads(os.environ.get("KONTROL_HOLDS") or "[]")}
    except json.JSONDecodeError:
        return set()


def new_item(s, gate, kind, summary, **extra):
    s["seq"] += 1
    q = {"id": f"q_{s['seq']:04d}", "gate": gate, "kind": kind, "summary": summary,
         "raised_at": iso(now()), "expires_at": None, "if_unanswered": "wait",
         "delegable": False, "options": [], "evidence": [], "instructions": ""}
    q.update(extra)
    s["queue"].append(q)
    return q


def posts_rows():
    try:
        return [json.loads(x) for x in POSTS.read_text().splitlines() if x.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def has_keys():
    """Whether an X credential exists. False today: kontrol.yaml declares posting as manual and
    the posting method is an open research question, so the flow ends on a manual-step card."""
    return bool(voice.env("X_API_KEY") and voice.env("X_API_SECRET"))


# ---- the lane ratio ------------------------------------------------------------------

def next_lane(s):
    """VOICE.md: one life post for every data post. Whichever lane is behind goes next."""
    rows = posts_rows()
    d = sum(1 for r in rows if r.get("lane") == "data")
    l = sum(1 for r in rows if r.get("lane") == "life")
    for it in s["items"].values():
        if it.get("state") in ("draft", "approved", "posting"):
            (d, l) = (d + 1, l) if it.get("lane") == "data" else (d, l + 1)
    return "life" if l < d else "data"


# ---- the shortlist -------------------------------------------------------------------

def build_shortlist(s, live, errors):
    """(options, notes). An option is {text, lane, format, fact_id, seed_id, source}.

    Candidates are generated, then validated, and only survivors are offered. A candidate the
    operator has already passed on is never shown twice."""
    passed = set(s.get("passed") or [])
    opts, notes, spent = [], [], [0.0]
    facts, rejected = voice.available_facts(exclude=set(s.get("posted_facts") or []))
    for f, why in rejected:
        notes.append(f"{f.id}: {why}")
    open_seeds = [x for x in voice.seeds(used=set(s.get("used_seeds") or []))]
    start = next_lane(s)
    order = ["data", "life"] if start == "data" else ["life", "data"]

    for lane in order:
        room = SHORTLIST_N - len(opts)
        if room <= 0:
            break
        per = min(room, PER_LANE_MAX)
        if lane == "data":
            for f in facts:
                if len(opts) >= SHORTLIST_N or sum(1 for o in opts if o["lane"] == "data") >= per:
                    break
                try:
                    cands, c = voice.generate("data", 2, fact=f, live=live)
                    spent[0] += c
                except voice.BudgetReached as e:
                    errors.append(str(e))
                    break
                for c in cands:
                    bad = voice.check(c["text"], "data", f)
                    if bad:
                        notes.append(f"dropped a {f.id} option: {bad[0]}")
                        continue
                    if c["text"] in passed:
                        continue
                    opts.append({"text": c["text"], "lane": "data", "format": "data",
                                 "fact_id": f.id, "seed_id": None, "source": c["source"],
                                 "headline": f.headline, "chart": f.chart})
                    break
        else:
            for n, sd in enumerate(open_seeds):
                if len(opts) >= SHORTLIST_N or sum(1 for o in opts if o["lane"] == "life") >= per:
                    break
                fmt = voice.format_for(n)
                try:
                    cands, c = voice.generate("life", 2, seed=sd, fmt=fmt, live=live)
                    spent[0] += c
                except voice.BudgetReached as e:
                    errors.append(str(e))
                    break
                for c in cands:
                    bad = voice.check(c["text"], "life", None, sd["text"])
                    if bad:
                        notes.append(f"dropped a life option: {bad[0]}")
                        continue
                    if c["text"] in passed:
                        continue
                    opts.append({"text": c["text"], "lane": "life", "format": fmt,
                                 "fact_id": None, "seed_id": sd["id"], "source": c["source"],
                                 "headline": None, "chart": None})
                    break
    if not open_seeds:
        notes.append("no life-post seeds: nothing filed under #profphet-life")
    return opts[:SHORTLIST_N], notes, spent[0]


def raise_pick_card(s, gates, actions, errors, live):
    if any(q.get("gate") == "pick-post" for q in s["queue"]):
        return
    opts, notes, cost = build_shortlist(s, live, errors)
    s["run_cost"] = round(s.get("run_cost", 0.0) + cost, 6)
    if not live:
        actions.append({"kind": "produce", "target": "shortlist", "external": False, "evidence": [],
                        "note": f"dry-run: would offer {len(opts)} option(s); " + ("; ".join(notes) or "no notes")})
        return
    if not opts:
        raise_seed_card(s, gates, notes)
        return
    blocks, choices, labels = [], [], {}
    for n, o in enumerate(opts, 1):
        tag = o["headline"] if o["lane"] == "data" else o["format"]
        blocks.append({"label": f"{n} · {o['lane']} · {tag}", "text": o["text"]})
        choices.append(str(n))
        labels[str(n)] = str(n)
    choices.append("regenerate")
    labels["regenerate"] = "↻ Generate new"
    q = new_item(
        s, "pick-post", "decision",
        f"Pick today's post: {len(opts)} option(s)",
        options=choices, labels=labels, copy=blocks, candidates=opts, delegable=True,
        title="Pick today's post", minutes=1, noun="post to choose", group="posts to choose",
        instructions="Tap a number and that one becomes a draft to approve. Nothing goes to X yet.",
        why=("These were written from the current database and from your own seeds, then checked "
             "against VOICE.md. Anything that quoted a number it could not prove was dropped before "
             "you saw it." + (" Notes: " + "; ".join(notes[:2]) if notes else "")))
    gates.append(q["id"])


def raise_seed_card(s, gates, notes):
    """Nothing to offer. Say which lane is empty and why, rather than a silent empty card."""
    if any(q.get("gate") == "need-seeds" for q in s["queue"]):
        return
    q = new_item(
        s, "need-seeds", "manual-step",
        "No post options: every candidate was unavailable or rejected",
        options=["done", "defer"], lane="setup", delegable=False, minutes=2,
        title="profphet has nothing to post",
        how=("Send a life-post seed from your phone: a message starting #profphet-life followed by "
             "something that actually happened - a red day, the hour after a loss, a specific detail. "
             "One line is enough, and kontrol shapes it. Data posts come back on their own once the "
             "database is fresh."),
        why="; ".join(notes[:4]) or "no facts were fresh enough and no seeds were filed",
        evidence=["VOICE.md"])
    gates.append(q["id"])


# ---- drafts --------------------------------------------------------------------------

def make_draft(s, chosen, actions):
    s["seq"] += 1
    iid = f"p_{now().strftime('%Y%m%dT%H%M%S')}"
    it = {"id": iid, "state": "draft", "created": iso(now()), "lane": chosen["lane"],
          "format": chosen["format"], "text": chosen["text"], "fact_id": chosen.get("fact_id"),
          "seed_id": chosen.get("seed_id"), "source": chosen.get("source"),
          "chart": chosen.get("chart"), "qid": None, "url": None, "posted_at": None}
    s["items"][iid] = it
    if chosen.get("fact_id"):
        s["posted_facts"] = sorted(set(s.get("posted_facts") or []) | {chosen["fact_id"]})
    if chosen.get("seed_id"):
        s["used_seeds"] = sorted(set(s.get("used_seeds") or []) | {chosen["seed_id"]})
    DRAFTS.mkdir(parents=True, exist_ok=True)
    (DRAFTS / f"{iid}.json").write_text(json.dumps(it, indent=2, sort_keys=True) + "\n")
    actions.append({"kind": "produce", "target": iid, "external": False,
                    "evidence": [str(DRAFTS / f"{iid}.json")], "note": f"drafted a {it['lane']} post"})
    return it


def localize(p):
    """Records are shared between the Mac and the PC; a card's media path must be local."""
    if not p:
        return p
    t = str(p)
    for a, b in (("/Users/cynful/fohomo", str(ROOT)), ("/home/keylow/projects/fohomo", str(ROOT))):
        if t.startswith(a):
            return t.replace(a, b, 1)
    return t


def draft_items(s):
    """Cards for work in flight, derived from state on every read so a crash can never leave a
    draft without a card (the discipline publicreceipt settled on)."""
    out = []
    for it in sorted(s["items"].values(), key=lambda x: x["created"]):
        st = it.get("state")
        if st == "draft":
            checks = voice.check(it["text"], it["lane"], None, None) if it["lane"] == "life" else []
            ok = not checks
            out.append({
                "id": it["id"] + "_a", "item": it["id"], "gate": "approve-post", "kind": "approve-item",
                "summary": f"Approve this {it['lane']} post", "raised_at": it["created"],
                "expires_at": None, "if_unanswered": "wait", "delegable": True, "lane": "daily",
                "title": f"{it['lane'].capitalize()} post: approve or edit",
                "why": ("Approving moves it to a post card with the text ready to copy. "
                        "Nothing reaches X until you post it yourself."),
                "how": "\n".join([f"{'✓' if ok else '✗'} {len(it['text'])}/280 characters",
                                  f"• written by {it.get('source') or 'unknown'}",
                                  f"• {'chart attached' if it.get('chart') else 'no chart'}"]
                                 + [f"✗ {c}" for c in checks]),
                "copy": [{"label": "Post", "text": it["text"]}],
                "media": localize(it.get("chart")),
                "options": (["approve"] if ok else []) + ["edit", "regenerate", "skip"],
                "labels": {"regenerate": "↻ Other options", "skip": "Not this one"},
                "note_required": ["edit"], "ask": {"choice": "edit", "prompt": "Send the post exactly as it should read."},
                "evidence": [str(DRAFTS / f"{it['id']}.json")], "noun": "draft to approve",
                "group": "drafts to approve", "minutes": 1, "instructions": ""})
        elif st == "approved":
            out.append({
                "id": it["id"] + "_p", "item": it["id"], "gate": "post", "kind": "manual-step",
                "summary": f"Post this to @{HANDLE}", "raised_at": it.get("approved_at") or it["created"],
                "expires_at": None, "if_unanswered": "wait", "delegable": True, "lane": "daily",
                "follows": it["id"] + "_a",
                "title": f"Post this to @{HANDLE}",
                "how": ("Copy the text below and post it on X as @" + HANDLE +
                        (". The chart is attached to this card - attach it to the post."
                         if it.get("chart") else ".") +
                        " Then tap Posted and paste the link."),
                "why": ("kontrol does not post to X for this account: kontrol.yaml declares posting as "
                        "manual and the posting method is still an open question."),
                "copy": [{"label": "Post", "text": it["text"]}],
                "media": localize(it.get("chart")),
                "options": ["done", "skip"],
                "labels": {"done": "Posted it", "skip": "Not now"},
                "note_required": ["done"],
                "ask": {"choice": "done", "prompt": "Paste the link to the post on X. Send - if you have not got it."},
                "evidence": [str(DRAFTS / f"{it['id']}.json")], "noun": "post to publish",
                "group": "posts to publish", "minutes": 2, "instructions": ""})
    return out


# ---- the five calls ------------------------------------------------------------------

def status(s):
    rows = posts_rows()
    last = max((r.get("posted_at") or "" for r in rows), default="") or None
    week = [r for r in rows if (r.get("posted_at") or "") >= iso(now() - timedelta(days=7))]
    facts, rejected = voice.available_facts(exclude=set(s.get("posted_facts") or []))
    open_seeds = voice.seeds(used=set(s.get("used_seeds") or []))
    drafts = [i for i in s["items"].values() if i.get("state") in ("draft", "approved")]
    q = len(s["queue"]) + len(draft_items(s))
    notes = [f"{len(facts)} fact(s) fresh enough to post, {len(open_seeds)} life seed(s) unused",
             "feed process not built (PLAN.md A1-A5)"]
    if not facts:
        notes.append("no fresh facts: " + "; ".join(f"{f.id} {why}" for f, why in rejected[:2]))
    if not voice.env("OPENROUTER_API_KEY"):
        notes.append("no OPENROUTER_API_KEY: options would be templates, not written copy")
    if not has_keys():
        notes.append("no X credential: posting is manual, by design (kontrol.yaml)")
    emit({
        "data": "reconciled" if rows else "unknown", "reconciled_at": None, "needs_human": q,
        "alive": False, "last_heartbeat": None, "emitted_24h": None, "dead_man_armed": False,
        "limit_headroom": None,
        "accounts": [{"id": "x", "handle": f"@{HANDLE}", "last_post_at": last,
                      "followers": None, "pending_replies": None},
                     {"id": "telegram", "handle": s.get("answers", {}).get("telegram-channel") or "(pending)",
                      "last_post_at": None, "followers": None, "pending_replies": None}],
        "buffer_days": len(drafts), "buffer_items": len(drafts), "last_published_at": last,
        "cadence_7d": len(week) or None, "on_track": (len(week) >= TARGET_PER_WEEK) if rows else None,
        "cost_30d_usd": round(voice.spent_30d(), 4), "streak_note": "not started" if not rows else None,
        "notes": notes,
    }, 2 if q else 0)


def queue(s):
    emit([dict(q) for q in s["queue"]] + draft_items(s), 2 if (s["queue"] or draft_items(s)) else 0)


def run(s, live, action):
    gates, actions, errors = [], [], []
    sc = holds()
    if not s.get("raised"):
        for it in ITEMS:
            q = new_item(s, it["gate"], it["kind"], it["summary"], options=it.get("options", []),
                         evidence=it.get("evidence", []), instructions=it.get("instructions", ""),
                         lane="setup", milestone=it["id"])
            gates.append(q["id"])
        s["raised"] = True
    elif not any(q.get("gate") == "openrouter-key" for q in s["queue"]) \
            and "openrouter-key" not in s.get("answers", {}) and not voice.env("OPENROUTER_API_KEY"):
        it = ITEMS[-1]
        q = new_item(s, it["gate"], it["kind"], it["summary"], options=it["options"],
                     evidence=it["evidence"], lane="setup", milestone=it["id"])
        gates.append(q["id"])

    if action in (None, "draft") and (chosen := s.pop("chosen", None)):
        make_draft(s, chosen, actions)

    produce_ok = "produce" not in sc and "run" not in sc and action in (None, "produce", "draft")
    in_flight = any(i.get("state") in ("draft", "approved") for i in s["items"].values())
    if produce_ok and not in_flight:
        raise_pick_card(s, gates, actions, errors, live)
    elif in_flight and action in (None, "produce"):
        actions.append({"kind": "produce", "target": "shortlist", "external": False, "evidence": [],
                        "note": "a draft is already in flight; not offering more"})

    save_state(s)
    emit({"run_id": os.environ.get("KONTROL_RUN_ID"), "started_at": iso(now()), "finished_at": iso(now()),
          "dry_run": not live, "actions": actions, "gates_raised": gates, "holds_raised": [],
          "cost_usd": round(s.pop("run_cost", 0.0), 6), "errors": errors,
          "next_suggested_run": iso(now() + timedelta(days=1))},
         2 if gates or errors else 0)


def decide(s, item_id, choice, note):
    # in-flight cards are derived, so they are matched by their derived id
    for card in draft_items(s):
        if card["id"] == item_id:
            return decide_draft(s, card, choice, note)
    q = next((x for x in s["queue"] if x["id"] == item_id), None)
    if q is None:
        fail(f"unknown queue item {item_id}")

    if q.get("gate") == "pick-post":
        cands = q.get("candidates") or []
        if choice.isdigit() and 1 <= int(choice) <= len(cands):
            pick = cands[int(choice) - 1]
            s["passed"] = sorted(set(s.get("passed") or []) |
                                 {c["text"] for c in cands if c["text"] != pick["text"]})
            s["chosen"] = pick
            s["queue"] = [x for x in s["queue"] if x["id"] != item_id]
            save_state(s)
            emit({"ok": True, "item": item_id, "choice": choice, "applied_at": iso(now()),
                  "message": "Drafting that one. It comes back as a card to approve.",
                  "run_after": {"live": True, "action": "draft"}})
        if choice == "regenerate":
            s["passed"] = sorted(set(s.get("passed") or []) | {c["text"] for c in cands})
            s["queue"] = [x for x in s["queue"] if x["id"] != item_id]
            save_state(s)
            emit({"ok": True, "item": item_id, "choice": choice, "applied_at": iso(now()),
                  "message": "Writing a fresh set.", "run_after": {"live": True, "action": "produce"}})
        fail(f"{choice} is not one of the options on that card")

    s.setdefault("answers", {})[q.get("milestone", q["gate"])] = note or choice
    s["queue"] = [x for x in s["queue"] if x["id"] != item_id]
    save_state(s)
    emit({"ok": True, "item": item_id, "choice": choice, "applied_at": iso(now())})


def decide_draft(s, card, choice, note):
    it = s["items"][card["item"]]
    gate = card["gate"]
    if gate == "approve-post":
        if choice == "approve":
            it.update(state="approved", approved_at=iso(now()))
            save_state(s)
            emit({"ok": True, "item": card["id"], "choice": choice, "applied_at": iso(now()),
                  "message": f"Approved. The post card has the text ready for @{HANDLE}."})
        if choice == "edit":
            if not note or not note.strip() or note.strip() == "-":
                fail("send the post exactly as it should read")
            bad = voice.check(note.strip(), it["lane"], None, None)
            it.update(text=note.strip(), source="operator", edited_at=iso(now()))
            save_state(s)
            emit({"ok": True, "item": card["id"], "choice": choice, "applied_at": iso(now()),
                  "keep_card": True,
                  "message": "Saved your wording." + (f" Heads up: {bad[0]}." if bad else "")})
        if choice in ("skip", "regenerate"):
            it.update(state="skipped", decided_at=iso(now()), note=note)
            save_state(s)
            emit({"ok": True, "item": card["id"], "choice": choice, "applied_at": iso(now()),
                  "message": "Dropped." + (" Writing a fresh set." if choice == "regenerate" else ""),
                  **({"run_after": {"live": True, "action": "produce"}} if choice == "regenerate" else {})})
    if gate == "post":
        if choice == "done":
            url = (note or "").strip()
            url = url if url.startswith("http") else None
            it.update(state="posted", posted_at=iso(now()), url=url, by="operator")
            with POSTS.open("a") as fh:
                fh.write(json.dumps({"id": url or f"manual:{it['id']}", "url": url, "item": it["id"],
                                     "lane": it["lane"], "format": it["format"],
                                     "fact_id": it.get("fact_id"), "seed_id": it.get("seed_id"),
                                     "posted_at": it["posted_at"], "source": "operator"}) + "\n")
            save_state(s)
            emit({"ok": True, "item": card["id"], "choice": choice, "applied_at": iso(now()),
                  "message": "Recorded." + ("" if url else " No link saved, so `learn` cannot read it back.")})
        if choice == "skip":
            it.update(state="skipped", decided_at=iso(now()), note=note)
            save_state(s)
            emit({"ok": True, "item": card["id"], "choice": choice, "applied_at": iso(now()),
                  "message": "Left it. The next run offers a fresh shortlist."})
    fail(f"{choice} is not one of the options on that card")


def learn(s):
    rows = posts_rows()
    linked = sum(1 for r in rows if r.get("url"))
    emit({"ok": True, "updated": 0, "metrics_at": None,
          "note": (f"{len(rows)} post(s) recorded, {linked} with a link. Impressions need the X "
                   "analytics export or an API read; the posting method is still an open question "
                   "(kontrol.yaml research: x-posting-method).")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("verb", choices=["status", "run", "queue", "decide", "learn"])
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
