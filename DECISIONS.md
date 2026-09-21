# @profphet / fohomo — Decisions

Append-only. Supersede by adding, never by editing. The plan's own decisions (2026-09-07, 09-12) live in `PLAN.md`.

## D-001 · Onboarding without an intake (CONTRACT §15A, provisional)
**Date:** 2026-09-13 · **Status:** provisional, confirm-intake item raised · **Review by:** 2026-10-13

The operator did not answer the five intake questions for this project. From `PLAN.md`: nothing is paused, nothing is running, there are no trackers to freeze, the X account is a cold start with no baseline recorded, and the delegation intent is full automation of the feed and daily derived posts with a human on original pieces. The first run raises a `confirm-intake` item; if corrected, this entry is superseded.

## D-002 · Prerequisites are queue items, not memory
**Date:** 2026-09-13 · **Status:** decided · **Review by:** 2026-12-13

The plan's "verify this week" list (referral terms, Telegram channel, reference accounts) becomes gates the operator answers. Nothing in the feed is built until the referral terms are verified, because the feed's every post carries the link.

## D-003 · Voice rules leave agent memory
**Date:** 2026-09-13 · **Status:** decided · **Review by:** 2027-03-01

`VOICE.md` holds the voice that previously existed only in agent memory (contract §12).

## D-004 · Facts come from SQL, voice comes from the model, and the two never swap jobs

**2026-09-20.** profphet needed to start posting, in a voice modelled on `@josbjohnson` and
fused with money twitter. The obvious build - hand an LLM the README and ask for tweets -
fails on this account specifically, because the account's entire promise is "a trader who
checks things". A hallucinated or stale figure is not a typo here; it is the product failing.

It would also have failed immediately. On the day this was built the committed README
described a corpus of 766,931 fills to 2026-08-27, while the live database on the PC held
1,297,231 fills to 2026-09-18: 24 days and 69% behind. A generator reading the prose would
have published that gap as fact on its first post.

So the split is enforced rather than encouraged. `profphet_voice.py` computes each fact from
the current database, carrying its SQL, its date and the exact list of figures it licenses.
The model is handed that block and writes only the sentence around it. `check()` then extracts
every numeric token from the candidate and drops it if any figure was not in the block. The
same function enforces the rest of VOICE.md - no emoji, hashtag, thread, engagement question,
returns claim, grindset boilerplate, or link on a life post.

A fact also carries a staleness allowance and is refused past it, so "never quote a stale
number" is a property of the code. On the Mac, where the databases are local artifacts, four
of five facts correctly refuse to be posted.

**Life posts are never generated from nothing.** A life post is shaped from a seed the
operator filed with `#profphet-life`, and the seed's own numbers are the only figures it
licenses. With no seeds the generator returns nothing and kontrol raises a card asking for
one. An invented confession is the exact slop the josbjohnson modelling exists to avoid, and
it is the one failure the audience cannot forgive; the system is built so it cannot happen
rather than instructed not to.

**Rejected:** generating from the README (wrong by 69% on day one); letting the model produce
figures and validating them against the database afterwards (a validator that has to
re-derive intent is weaker than one that checks set membership); a fixed editorial calendar
(the operator chooses from a shortlist, which is what debate-receipts settled on after
auto-pick produced clips worth discarding).

**Not done:** posting to X. `kontrol.yaml` declares `accounts.x.posting: manual`, there is no
X credential, and the `x-posting-method` research question is still open and shared with
debate-receipts. The flow therefore ends on a manual-step card carrying the text and the
chart. Building an untested path that writes to a live account was the larger risk.
