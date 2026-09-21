#!/usr/bin/env python3
"""End-to-end test of profphet's content lane. No network, no keys, no real records touched.

Run: python3 test_profphet.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILS: list[str] = []


def ok(cond, label, detail=""):
    print(f"{'  ok  ' if cond else ' FAIL '} {label}{'  ' + str(detail) if detail and not cond else ''}")
    if not cond:
        FAILS.append(label)


def run_cli(kdir, *args, live=False, action=None, note=None):
    cmd = [sys.executable, str(ROOT / "kontrol_adapter.py"), *args]
    if live:
        cmd.append("--live")
    if action:
        cmd += ["--action", action]
    if note is not None:
        cmd += ["--note", note]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PROFPHET_KDIR": str(kdir),
                            "HOME": str(kdir.parent)}, cwd=ROOT)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]), p.returncode, p.stderr
    except (IndexError, json.JSONDecodeError):
        return None, p.returncode, (p.stderr or p.stdout)


def main():
    import profphet_voice as voice

    # ---- the voice spec -------------------------------------------------------------
    spec = voice.load_spec()
    ok(set(spec) >= {"register", "banned", "data", "life"}, "VOICE.md exposes all four spec blocks", spec.keys())
    ok("LIFE POSTS NEVER CARRY THE REFERRAL LINK" in spec.get("life", ""),
       "the life spec carries the referral-link ban")
    for lane in ("data", "life"):
        ok(len(voice.system_prompt(lane)) > 400, f"{lane} system prompt is assembled")

    # ---- facts ----------------------------------------------------------------------
    facts, rejected = voice.available_facts(today=date(2026, 9, 20))
    ok(all(f.numbers and f.body and f.as_of for f in facts), "every usable fact carries numbers, body and a date")
    ok(all(why for _, why in rejected), "every rejected fact says why")
    stale = [f for f, why in rejected if "staler" in why]
    ok(bool(stale), "stale facts are refused rather than posted", f"{len(stale)} refused")

    # a fact must never be usable when its data is older than its allowance
    if facts:
        f = facts[0]
        ok(f.stale_by(0, date(2099, 1, 1)) > 0, "a fact goes stale as the clock moves")

    # ---- the validator --------------------------------------------------------------
    f = facts[0] if facts else None
    seed = "lost 4k on a red day in march. sat at the screen three hours after the close."
    cases = [
        ("data", "the markup runs 1.86 bp to 9.31 bp. a 5.0x spread on the same fill.", None, True),
        ("data", "the spread is 7.2x across front-ends.", None, False),
        ("data", "same book, 5.0x the price \U0001f525", None, False),
        ("data", "same book, 5.0x the price #crypto", None, False),
        ("life", "you lost 4k. you sat there three hours. close the laptop.", seed, True),
        ("life", "you lost 9k. close the laptop.", seed, False),
        ("life", "red day. the numbers are at fomo.family", seed, False),
        ("life", "the grind never stops.", seed, False),
        ("life", "i turned 2k into 40k.", seed, False),
        ("life", "down bad — anyway; keep going", seed, False),
        ("life", "you will lose. thoughts?", seed, False),
        ("data", "THIS is the real cost.", None, False),
        ("life", "x" * 300, seed, False),
        ("life", "the hour after a red close is the whole job.", seed, True),
    ]
    for lane, text, sd, should_pass in cases:
        bad = voice.check(text, lane, f if lane == "data" else None, sd)
        ok(bool(bad) != should_pass, f"check({text[:34]!r}) {'passes' if should_pass else 'rejects'}", bad)

    ok(voice.check("", "life") == ["empty"], "empty text is rejected")

    # ---- generation degrades without keys -------------------------------------------
    cands, cost = voice.generate("data", 3, fact=f, live=False)
    ok(cost == 0.0, "a dry run spends nothing")
    ok(all("template" in c["source"] for c in cands), "a dry run returns templates, labelled as such")
    cands, cost = voice.generate("life", 2, seed={"id": "x", "text": seed}, fmt="punch", live=True)
    ok(cost == 0.0 and cands and "no OPENROUTER_API_KEY" in cands[0]["source"],
       "live with no key falls back to a labelled template, and spends nothing", cands)

    # ---- format mix ------------------------------------------------------------------
    got = [voice.format_for(i) for i in range(12)]
    ok(got.count("paragraph") == 6 and got.count("punch") == 4 and got.count("confession") == 2,
       "the format cycle holds the half / third / rest proportion", got)

    # ---- the full card flow, in a sandbox --------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="profphet-test-"))
    kdir = tmp / "kontrol_data"
    kdir.mkdir(parents=True)
    import kontrol_adapter as ka

    real = (ka.KDIR, ka.STATE, ka.POSTS, ka.DRAFTS, voice.KDIR, voice.LLM_COSTS, voice.seeds)
    ka.KDIR, ka.STATE, ka.POSTS, ka.DRAFTS = kdir, kdir / "state.json", kdir / "posts.jsonl", kdir / "drafts"
    voice.KDIR, voice.LLM_COSTS = kdir, kdir / "llm-costs.jsonl"
    voice.seeds = lambda used=None: [s for s in [{"id": "n_1", "text": seed, "at": "2026-09-20T00:00:00+00:00"}]
                                     if s["id"] not in (used or set())]
    try:
        # The real emit()/fail() end the call with sys.exit, and decide() relies on that for
        # control flow. A stub that merely records would let execution fall through into the
        # next branch, so it must exit too.
        emitted: list = []

        def _emit(obj, code=0):
            emitted.append((obj, code))
            raise SystemExit(code)

        def _fail(msg):
            emitted.append(({"error": msg}, 1))
            raise SystemExit(1)

        ka.emit, ka.fail = _emit, _fail

        def call(fn, *a, **kw):
            """Invoke an adapter entry point and return what it emitted."""
            try:
                fn(*a, **kw)
            except SystemExit:
                pass
            return emitted[-1][0] if emitted else None

        s = ka.load_state()
        dry = call(ka.run, s, live=False, action=None)
        ok(dry["dry_run"] is True and dry["cost_usd"] == 0.0, "dry run spends nothing", dry.get("cost_usd"))
        ok(not any(q.get("gate") == "pick-post" for q in ka.load_state()["queue"]),
           "a dry run raises no pick card")
        ok(any("would offer" in (a.get("note") or "") for a in dry["actions"]),
           "a dry run says what it would have offered")

        call(ka.run, ka.load_state(), live=True, action=None)
        s = ka.load_state()
        pick = next((q for q in s["queue"] if q.get("gate") == "pick-post"), None)
        ok(pick is not None, "a live run raises the pick card")
        if pick:
            ok(1 <= len(pick["candidates"]) <= ka.SHORTLIST_N, "the shortlist is 1..N options", len(pick["candidates"]))
            ok(len(pick["copy"]) == len(pick["candidates"]), "one copy block per option")
            ok("regenerate" in pick["options"], "the card offers a regenerate option")
            ok(all(not voice.check(c["text"], c["lane"], None,
                                   seed if c["lane"] == "life" else None) or c["lane"] == "data"
                   for c in pick["candidates"]), "no offered life option fails its own validator")
            lanes = {c["lane"] for c in pick["candidates"]}
            ok(lanes <= {"data", "life"}, "every option declares a known lane", lanes)

            n_before = len(pick["candidates"])
            emitted.clear()
            res = call(ka.decide, ka.load_state(), pick["id"], "1", None)
            ok(res.get("run_after") == {"live": True, "action": "draft"}, "picking asks kontrol to draft", res)
            s = ka.load_state()
            ok(len(s["passed"]) == n_before - 1, "the options not chosen are remembered as passed", s["passed"])

            call(ka.run, ka.load_state(), live=True, action="draft")
            s = ka.load_state()
            drafts = [i for i in s["items"].values() if i["state"] == "draft"]
            ok(len(drafts) == 1, "the pick became exactly one draft", len(drafts))
            cards = ka.draft_items(s)
            ok(len(cards) == 1 and cards[0]["gate"] == "approve-post", "the draft shows an approve card")
            ok(cards[0]["copy"][0]["text"] == drafts[0]["text"], "the card's copy block is the post itself")

            # a second run must not offer more while one is in flight
            emitted.clear()
            call(ka.run, ka.load_state(), live=True, action=None)
            ok(not any(q.get("gate") == "pick-post" for q in ka.load_state()["queue"]),
               "no new shortlist while a draft is in flight")

            # edit keeps the card, approve moves it on
            emitted.clear()
            r = call(ka.decide, ka.load_state(), cards[0]["id"], "edit", "you lost 4k. close the laptop.")
            ok(r.get("keep_card") is True, "an edit keeps the card open", r)
            ok(ka.load_state()["items"][cards[0]["item"]]["text"] == "you lost 4k. close the laptop.",
               "the edit is saved verbatim")

            emitted.clear()
            call(ka.decide, ka.load_state(), cards[0]["id"], "edit", "-")
            ok(emitted[-1][1] == 1, "an empty edit is refused", emitted[-1][0])

            s = ka.load_state()
            cards = ka.draft_items(s)
            emitted.clear()
            call(ka.decide, s, cards[0]["id"], "approve", None)
            s = ka.load_state()
            ok(s["items"][cards[0]["item"]]["state"] == "approved", "approve moves the draft on")
            post_cards = ka.draft_items(s)
            ok(post_cards and post_cards[0]["gate"] == "post", "an approved draft shows a post card")
            ok(post_cards[0].get("follows") == cards[0]["id"], "the post card replaces the approve card")
            ok("done" in post_cards[0]["options"], "the post card can be marked done")

            emitted.clear()
            call(ka.decide, ka.load_state(), post_cards[0]["id"], "done", "https://x.com/profphetic/status/1")
            s = ka.load_state()
            ok(s["items"][post_cards[0]["item"]]["state"] == "posted", "done records the post")
            rows = [json.loads(x) for x in (kdir / "posts.jsonl").read_text().splitlines()]
            ok(len(rows) == 1 and rows[0]["url"] == "https://x.com/profphetic/status/1",
               "the post is appended to posts.jsonl with its link", rows)
            ok(rows[0]["lane"] in ("data", "life") and "posted_at" in rows[0], "the row carries lane and time")
            ok(not ka.draft_items(ka.load_state()), "nothing is left in flight once posted")

            # the ratio flips to the other lane
            ok(ka.next_lane(ka.load_state()) != rows[0]["lane"], "the next lane is the one just posted against")

        # a seed is not reused
        s = ka.load_state()
        if s.get("used_seeds"):
            ok(voice.seeds(used=set(s["used_seeds"])) == [], "a used seed is not offered again")

        # status and queue stay contract-shaped
        emitted.clear()
        st = call(ka.status, ka.load_state())
        for k in ("data", "needs_human", "accounts", "cost_30d_usd", "notes",
                  "buffer_days", "last_published_at", "cadence_7d", "on_track"):
            ok(k in st, f"status carries {k}")
        ok(st["accounts"][0]["handle"] == "@profphetic", "status reports the real handle", st["accounts"][0])
        emitted.clear()
        items = call(ka.queue, ka.load_state())
        ok(isinstance(items, list), "queue returns a list")
        for it in items:
            missing = [k for k in ("id", "gate", "kind", "summary", "options", "evidence",
                                   "raised_at", "if_unanswered", "delegable") if k not in it]
            ok(not missing, f"queue item {it.get('id')} has every required key", missing)
    finally:
        (ka.KDIR, ka.STATE, ka.POSTS, ka.DRAFTS, voice.KDIR, voice.LLM_COSTS, voice.seeds) = real
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f_ in FAILS:
            print("  -", f_)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
