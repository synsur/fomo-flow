# fohomo — project plan

*Written 2026-09-07. Every number below is from the databases in this repo unless marked
"assumption" or "verify". Re-run the scripts before quoting any of them; they move.*

## 0. Where we are

**Goal:** make money from fomo.family, the social crypto trading app, without trading on it.

**Measured so far** (README has the full findings):

| | |
|---|---|
| Perp traders net-positive after fees | 29.1% |
| Share of the cohort's loss that is fees | 74% |
| Wallets that traded exactly one day, ever | 42–45% |
| Fee discount tier | granted by hand, not earnable |
| Funding-skew harvesting | skew flips sign in 7 of 12 markets month to month |
| "Trending on FOMO → buy" alert | crowd arrives after a +9% run; zero excess return after |

Four ways to make money on the app itself have now been measured and ruled out. What is
left is the thing every measurement pointed at: **the crowd pays about $9M a year in fees,
roughly three quarters of it with no referrer attached** (spot ≈ $3.7M/yr from $10.1K/day
sampled; perps ≈ $5.4M/yr from $447K in the last 30 days).

**Assets in hand:** seven measurement scripts over public data only, plus the chart
generator; five charts (light and dark) in `content/`; a README that is itself the credibility anchor; the profphet X account (`@profphetic`, the bare name was taken), aimed
at crypto twitter and money twitter, with a defined voice; a working spot-trade parser that
can run live.

## 1. Thesis

Distribution, not trading. Two engines feeding one funnel:

1. **A live "what the crowd is buying" feed** (Telegram first) built from the on-chain
   spot flow, in the Profphet voice, with a referral link on every post.
2. **Original-data content** on X, each piece a measurement nobody else has, ending in
   the same link.

The funnel is: attention → referral signups → 25% of their fees, recurring (verify, §7).
The feed and the content sell the same thing, which is not "buy this" but "here is what
the crowd is doing and what usually happens next". That framing is honest, it is what the
data supports, and it is the only version a small account has standing to publish.

## 2. Unit economics — the part that decides everything

FOMO's fee base is a whale distribution, not a crowd:

| perps, per wallet-month | |
|---|---|
| mean FOMO fee | $34.32 |
| median | **$0.56** |
| p90 | $19.85 |
| p99 | $568 |
| top 1% of wallet-months pay | **69%** of all fees |
| top 10% pay | 95% |
| spot, top 10% of traders pay | 59% of the day's fees |

At a 25% referral rate the **median referred wallet is worth $0.14 a month**. A p99 wallet
is worth $142 a month. Mean lifetime perp fees per wallet in the 83-day window are $38.87,
so the mean referral is worth about $10 per wallet, and that mean is almost entirely the
tail.

**Consequences for the plan:**

- Referral income is a whale-acquisition game. Volume of small signups is the *audience*,
  not the revenue. The revenue is the handful of accounts that trade size.
- Content must reach people who already trade size elsewhere (Hyperliquid natives,
  memecoin accounts with real PnL), not tourists. That argues for the comparative pieces
  ("same order book, 5× the price") over the FOMO-only pieces.
- Targets have to be set in *fees referred*, not signups. Working targets (assumptions):
  100 referred wallets and $2K of referred fee volume by week 8; one wallet in the top
  decile by week 12. If the first is met and the second is not, the feed is an audience
  tool and the content is the business.

## 3. Workstreams

### A. Live feed bot — weeks 1–3

Spot only for the live part. The Solana fee wallet is on-chain in real time; the
Hyperliquid builder CSV is published daily, so perps get a daily digest, not alerts.
Never point anything at fomo.family itself.

- **A1 Poller.** Tail `getSignaturesForAddress` on the fee wallet every ~20s, parse with
  `fomo_spot.parse`, append to `fomo_live.db`. This is the README's "Phase 5" — same code,
  new purpose. Keyless RPC pool is enough at FOMO's ~1 tx/s; upgrade to a keyed endpoint
  only if hit rate drops.
- **A2 Burst detector.** Per mint, 15-minute bins, distinct buyers ≥ 5 and ≥ 3× the
  mint's trailing mean — the thresholds `fomo_timing.py` already uses. Dedupe episodes
  with a 1-hour cool-off. Resolve ticker and pool name via GeckoTerminal (cache it; the
  free tier throttles hard).
- **A3 Poster.** Telegram Bot API, one channel, one post per episode. Template, in the
  Profphet voice: how many wallets, how much, what price did in the last hour, and the
  measured line about what usually happens next. Referral link and a one-word disclosure
  on every post. Example shape, not final copy:

  > 41 wallets just piled into $BOLLOCKS on fomo. $63k in 15 min, +38% into it.
  > median outcome from here is −4.8% by dinner and −22% by tomorrow. anyway [link]

- **A4 Daily digest.** From the builder CSV each morning: most-piled-into perps, fee burn,
  biggest winner and loser of the day (wallet truncated), share of wallets that made
  money. Post to Telegram and X. This is the recurring content format; it writes itself.
- **A5 Ops.** Run on a $5 VPS or this Mac under launchd; log every post; hard rate
  limits; a dead-man alert if the poller stops. Dependency-free Python like the rest of
  the repo.

Done when: the channel has posted unattended for 7 days without a duplicate or a stall.

### B. Content (profphet, `@profphetic`) — from week 1, ongoing

profphet (`@profphetic`) is a crypto-twitter / money-twitter account, not a FOMO account. FOMO is its
first data series. The account's promise is the voice memory in one line: a trader who
checks things, with receipts from public on-chain data. Everything FOMO-specific is
published as an instance of a format that travels — what an app costs you, who actually
wins, what the crowd does next — so the audience compounds even if FOMO does not.

**What the audience changes.** CT and money twitter care about two questions: am I
getting robbed, and who is actually winning. Both are universal, so the universal pieces
lead and the FOMO-only pieces follow. The same-order-book comparison already covers every
Hyperliquid front-end, and `builders_compare.py` makes "what [app] costs you" a repeatable
series across venues — how the account grows past one app while the referral funnel stays
where the referral pays.

Cadence: one original measurement a week, one derived post most days from the digest (A4).
Every data piece ends on a stealable line and carries the link. Voice rules are in memory; the
short version is lowercase, a trader who checks things, never analyst register, never
pre-hedge.

**Mix.** Data posts alone read as a bot, so the account also posts about life, money and
what success looks like from inside a trading screen — same person, same voice. The
reference is `@josbjohnson` (read 2026-09-20: 59.7K followers on ~1.3 text-only posts a
day). What transfers is the *mechanics*, not the subject:

- Three formats, in this proportion: the paragraph (3–6 sentences, second person, ends on
  a 2–5 word directive) about half; the punch (1–2 lines) a third; the first-person
  confession ("the moment it flipped for me was…") the rest — the confession is what makes
  it a person.
- One idea per post. Lowercase, periods as beats, fragments. Zero links, hashtags, emojis,
  threads or engagement questions in a life post; the sell lives in the bio and on the
  data posts only. **Life posts never carry the referral link.**
- Subjects that landed on the reference, translated: forgiving yourself after a loss (his
  best post); a coined physical phrase the reader can steal; "it's supposed to be hard";
  "it's supposed to be fun". Profphet's versions are about money, screens, variance and
  red days — the data account's life posts are about what the data feels like.
- What died on the reference and stays out: bullet lists of virtues, routine tips, and
  abstract poetry with no image.
- No grindset boilerplate and never a returns flex the on-chain data would contradict.
- Ratio: start at one life post for every data post (the reference grows on life posts
  alone), reviewed against impressions at the week-8 gate.

**Account mechanics, week 1:**

- Bio carries the referral link with a one-word disclosure. Pinned post is the toll piece
  with its chart, because it travels furthest.
- Record the baseline in the scorecard: followers, average impressions on the last ten
  posts. Growth gets measured from a number, not a feeling. (Assumption until recorded:
  cold start. The gates in §4 don't depend on follower count; the X-automation decision
  does.)
- Quote-post real traders' lines with the data. Crypto X quotes lines, not findings; this
  is where a small account gets seen.
- Charts are the hook. Regenerate from the current database before every post.

Backlog, in publish order — universal first, FOMO-only last:

1. **Same order book, up to 5× the price** — `builders_compare.py` and chart 09 are done;
   ship first. Reaches Hyperliquid natives, who trade size, and travels across CT.
2. **You are not losing to the market, you are losing to the toll** — chart 01. The pinned
   piece.
3. **The crowd chases** — the Phase 4 finding. Pairs with the feed launch; universal degen
   behaviour.
4. **Winners and losers are the same person** — the behavioural identity finding.
   Money-twitter bait: it is variance.
5. **Trades under $5 pay 82% in fees** — the regressive spot pricing. Travels to the fee
   discourse on money twitter.
6. **The fee discount is a favour, not a tier** — chart 08. FOMO-specific; niche; last.
7. **Weekly FOMO report** — a fixed format from A4 numbers, every Monday.
8. **Next series** — the same measurements for the next app, straight from
   `builders_compare.py`.

Rule: no piece ships without its chart regenerated from the current database.

### C. Referral and measurement — week 1, then weekly

- Get the referral link and read the actual terms (§7 has what to verify).
- One short link per channel (feed, X, README) so attribution is visible.
- Weekly scorecard, one file in the repo: channel subscribers, X followers and impressions
  on data posts, link clicks per channel, referred signups, referred fee volume, payout
  received. Fee volume is the number that matters.

### D. Data platform hygiene — week 1, then monthly

- Commit the in-flight work: `builders_compare.py`, the chart-09 additions to
  `make_charts.py`, `fomo_timing.py`, README.
- Daily `fomo_ingest.py` on a schedule; monthly re-run of every finding and chart. The
  README already warns that numbers moved 60% in nine days; a stale claim is the fastest
  way to lose the credibility the whole plan rests on.
- Keep the two-builder name collision documented; it will bite again.

## 4. Timeline and gates

| weeks | deliver | gate |
|---|---|---|
| 1–2 | referral link live; A1–A3 posting; builders piece shipped; work committed | channel exists, link attributed |
| 3–4 | A4 digest; weekly report format; pieces 2–3 shipped | 7 unattended days on the bot |
| 5–8 | pieces 4–6; scorecard reviewed weekly; tune burst thresholds on live data | **week 8 gate** |
| 9–12 | double down on whatever the scorecard says | week 12 gate |

**Week 8 gate.** Under ~50 referred wallets and under $500 of referred fees: keep the
content, cut the bot to the daily digest only. Over 100 wallets or $2K of fees: invest in
X automation and a second feed (Discord). Between: continue as is, revisit at week 12.

**Week 12 gate.** No referred wallet in the top decile of fee payers by now means the
audience is tourists; shift content entirely to the comparative, Hyperliquid-native
pieces, which is where the whales are.

## 5. Risks

- **The referral program changes or ends.** Single point of failure. Mitigation: the
  content and the audience are portable to any venue; the comparative pieces already
  cover every Hyperliquid front-end.
- **FOMO rotates the fee wallet or builder code.** The DefiLlama adapter is the canary;
  check it monthly.
- **Perps are barred to U.S. persons.** Never post anything that reads as a solicitation
  to trade perps; the digest reports, it does not recommend.
- **X API cost.** Auto-posting to X costs money; start manual, automate when the digest
  proves out.
- **The voice slips into analyst register.** The measured, hedged tone of the README is
  right for the README and wrong for every post. Keep them separate.
- **Rate limits.** GeckoTerminal throttles far below its stated limit; Solana public RPC
  drops batches. Both are handled in code already; don't add a dependency on either
  being reliable.

## 6. Ruled out — do not revisit without new data

- Trading on the app for edge. 29.1% net-positive; fees are 74% of the loss.
- Grinding toward the discounted fee tier. It is granted by hand.
- Harvesting funding against cohort skew. 7 of 12 markets flip monthly.
- Buying what is trending on FOMO, from on-chain flow or from social media. The flow is a
  lagging indicator on both venues; the biggest bursts are followed by negative excess
  returns; on spot the median buy is −22% a day later.

## 7. Verify this week

- The referral rate, what it applies to (spot, perps, both), whether it is recurring or
  time-boxed, how attribution works (link, code, first-touch), payout asset and cadence.
  The 25% figure is from earlier research, not from the app's terms.
- Whether FOMO's terms restrict promoting the app with a referral link, and whether they
  say anything about publishing on-chain data about its users.
- Telegram channel name availability and whether the bot needs admin rights to post.
- Whether the fee wallet still matches the DefiLlama adapter.
