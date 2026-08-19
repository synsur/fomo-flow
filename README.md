# fomo-flow

Measuring what actually happens on [fomo.family](https://fomo.family) — the social crypto
trading app — from public on-chain data.

Everything here reads third-party public sources: Hyperliquid's stats bucket and Solana
RPC. **Nothing touches fomo.family's servers.** No API key is required for any of it.

## What it found

Across **452,338 perpetuals fills** (12,328 wallets, 2026-06-05 → 08-17) and **5,882
Solana spot trades**:

- **29% of perp traders finish ahead after fees.** The cohort is down $1.31M, and
  **74% of that drain is fees, not market losses.**
- **98.7% of fills cross the spread.** They almost never post a limit order.
- **44.5% of wallets traded on exactly one day** and never came back.
- **The advertised fee rate is a default, not a price.** It is a per-account setting
  that gets granted *and revoked* by hand — 2 grants and 26 revocations observed on
  perps; every long-history spot account observed repriced. There is no volume
  threshold: accounts lost a discount after $997 of trading and kept it past $5M.
- **Spot pricing is regressive.** Trades under $5 pay a median **82%** in fees, against
  the advertised 0.50%, because of the flat minimum fee.
- **53% of spot volume is discounted**, blending to 0.379% against a 0.500% headline.

Charts for each are in [`content/`](content/).

## Quickstart

Python 3.9+. `pip install -r requirements.txt` (or just `brew install lz4` — the ingest
scripts need nothing else).

```bash
python3 fomo_ingest.py        # backfill all perp fills          (~2 min, ~130 MB)
python3 fomo_outcomes.py      # trader outcomes, retention, skew
python3 fomo_tier.py --roster # is the fee discount earned?

python3 fomo_spot.py --limit 9000   # sample Solana spot trades  (~40 min)
python3 fomo_history.py             # per-wallet rate histories
python3 make_charts.py              # regenerate content/*.png
```

Every script derives its own labels and totals from the database, so re-running on fresh
data updates the charts and findings without editing anything.

## Caveats worth knowing

- Hyperliquid's own venue fees are **modelled** at base rates, not measured; only FOMO's
  markup is exact. Where this matters (the 74% figure) the finding survives the worst
  case — at *zero* venue fees, fees are still 54% of the loss.
- `closed_pnl` excludes open positions, so wallets still holding are undercounted.
- The spot per-account finding rests on the ~87% of traders with stable rates; the
  highest-frequency accounts show wide intra-account ranges that are more likely
  multi-leg attribution error than real pricing.

## Why this works

FOMO routes perps through Hyperliquid using a **builder code**. Hyperliquid publishes
every fill routed through a given builder as a public daily LZ4 CSV — no key, no rate
limit:

```
https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{builder}/{YYYYMMDD}.csv.lz4
```

Columns: `time, user, coin, side, px, sz, crossed, special_trade_type, tif,
is_trigger, counterparty, closed_pnl, twap_id, builder_fee`

`user` is the trader's address and `closed_pnl` is realized PnL, so this is per-user
positioning, performance and cost for FOMO's entire perp book.

### Two builders share the name

From DefiLlama's `factory/hyperliquid.ts`:

| Key | Address | What it is |
|---|---|---|
| `fomo-social-trading-perps` | `0x2a2b6b09…d17` | **fomo.family** — the app. Start `2026-06-05`. |
| `fomo-perps` | `0xb838e4d1…adb` | A different product (onfomo.com). No start date. |

Their daily files differ 20–50× in size. Building against the wrong one gives you a
plausible, useless dataset.

## Usage

Requires Python 3.9+ and LZ4 (`pip install lz4`, or `brew install lz4` for the CLI).

```bash
python3 fomo_ingest.py                  # backfill launch → yesterday into fomo_fomo.db
python3 fomo_ingest.py --since 2026-08-01
python3 fomo_ingest.py --builder other  # the other FOMO, for comparison
python3 fomo_ingest.py --refresh 2026-08-16

python3 fomo_tier.py                    # the fee-tier investigation
python3 fomo_tier.py --roster           # + list every discounted wallet
python3 fomo_outcomes.py                # trader outcomes, retention, skew persistence
```

Ingest is idempotent and resumable — days already stored are skipped, `--refresh`
re-pulls one. Missing days (no trades, or not yet published) are recorded, not retried.

Corpus as of 2026-08-15: **433,811 fills · 11,584 wallets · 71 days · $896M notional**,
about 60 MB of SQLite.

## Finding: the discounted fee tier is granted, not earned

A visible minority of fills bill at ~1 bp instead of the standard 5 bp. Whether that
is reachable decides if routing perps through FOMO can ever be rational. It isn't.

`fomo_tier.py` walks the candidates and computes each verdict from the data:

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Discrete configured tiers | **Supported** | Rates land on round `f` values: 50, 30, 25, 20, 10, 0 tenths of a bp |
| Set per account | **Supported** | 0 of 360,630 fills from ordinary wallets ever fell below 2.5 bps |
| Earned via order type | Ruled out | Ordinary wallets span 0.055 bps across all order types; the global `Gtc` "discount" is composition bias from discounted whales |
| Market or coin promo | Ruled out | Several rates coexist in the same market on the same day |
| Volume threshold | Ruled out | Crossovers occur anywhere from **$29** to **$34.9M** of prior notional — a 1.19-million-fold range |
| Toggled by FOMO | **Supported** | 2 grants, **26 revocations**, 9 accounts flipping repeatedly |

**The cohort:** 73 wallets (0.6%) hold **31.3% of FOMO's perp notional** while paying
3.04 bps against everyone else's 4.94 — about $274K/yr of forgone revenue.

**So:** there is no threshold to trade toward. For an ordinary account the original
advice stands — at 5 bp/side on top of Hyperliquid's own fees, route perps direct.
But FOMO clearly grants this to size, which makes it a negotiation rather than a
ladder, and `--roster` prints exactly who already has it.

## Finding: traders lose mostly to fees, not to the market

`fomo_outcomes.py` recomputes trader outcomes across all 71 days, and models
Hyperliquid's own venue fees on top of FOMO's markup (`closed_pnl` nets out
neither, and HL's fee is the larger of the two).

| | |
|---|---|
| Net-positive after all fees | **29.1%** of 10,492 wallets with realized PnL |
| Cohort net result | **−$1,262,959** over 71 days |
| Share of that drain that is *fees* | **74%** — the rest is market losses |
| Taker fills | 98.7% |
| Traded exactly one day, ever | 44.6% of wallets |
| Retention (wallets old enough to churn) | 19.3% |

**Experience barely helps.** Win rate climbs from 19% (1 active day) to ~41%
(16–30 days), but aggregate return on notional stays negative in *every*
experience bucket. **Size doesn't help either** — win rate is flat at 25–31%
from sub-$1K wallets to $1M+ wallets.

The earlier 37.8% figure came from a 10-day window counting only FOMO's builder
fee. Both changes matter; 29.1% is the defensible number.

### What this rules out

The dossier proposed harvesting funding against persistent retail long skew.
**It doesn't hold.** Month over month, **7 of 12 liquid markets flip the sign of
their skew** — HYPE goes +21.5% (Jul) to −10.1% (Aug), crude −14.1% to +1.8%.
The +61.5% XYZ100 skew that looked compelling in a 10-day window decays to
+30.7% over the full history. Aggregate FOMO positioning is roughly balanced and
unstable, so there is no durable one-sided imbalance to fade at cohort level.

## Phase 3: Solana spot — measured

`fomo_spot.py` reads FOMO's spot trades directly off Solana. Identification was
verified against live transactions:

- every FOMO swap pays a USDC fee to a token account owned by
  `R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX`
- every one routes through program `proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u`
- **gas is sponsored, so the signer is FOMO's relayer, not the trader**

Trader identification is deterministic — no heuristic. In a USDC swap the pools sit
opposite the user, so `USDC down AND some token up` uniquely identifies the buyer even
through multi-hop routes, and the mirror rule catches sellers. (An earlier
frequency-based heuristic dropped 98% of transactions; this parses **75%**.)

**No API key needed.** The public mainnet RPC accepts JSON-RPC batches (~4.4 tx/s, ~60%
hit rate) and `solana-rpc.publicnode.com` serves singles (~3.3 tx/s, ~100% hit rate).
Running both concurrently gives ~4.5 tx/s sustained. Results are parsed and committed
every ~400 transactions, so an interrupted run keeps its progress.

### Result: the advertised rate is a default, not the price

Sample of **5,882 trades · 2,771 traders · $2.6M notional**:

| Trade size | n | Median rate |
|---|---|---|
| $0–5 | 57 | **81.9%** |
| $5–10 | 14 | 8.4% |
| $10–25 | 11 | 5.3% |
| $25–50 | 23 | 3.2% |
| $50–100 | 67 | 0.50% |
| $100–250 | 2,884 | 0.50% |
| $250–1K | 2,509 | 0.50% |
| $1K–10K | 304 | 0.45% |

Blended: **0.387%**. On trades ≥$100: **0.382%** — *below* the advertised 0.50%,
because **45% of them pay less than headline**.

**The rate is a per-account setting, same as the perp builder fee.** Of 206 traders with
≥4 qualifying trades, **86.9% hold a rate stable within 0.02pp** across all their trades.
Distinct tiers appear at 0.50 / 0.45 / 0.33 / 0.31 / 0.29 / 0.27 / 0.24 / 0.22 / 0.20 /
0.15%, each with its own population of accounts. Token type is *not* the driver — majors
and memecoins both sit at 0.500% median in the $100–1K buckets.

### Is the spot discount earned or granted? — granted, same as perps

`fomo_history.py` inverts the query: instead of paging the fee wallet (intractable at
~1 tx/sec), it reads each identified trader's **own wallet history**, then samples
transactions spread across that account's lifetime.

Most FOMO spot wallets turn out to be very new — median observed history 0.1 days, only
5 of 125 usable accounts span a week or more. (Not a signature-cap artifact: just 1 of
281 wallets hit the 1000-signature limit.) So the headline "94% constant" is meaningless,
since it is dominated by accounts observed for hours.

**The accounts with real history are unambiguous.** All four changed rate, in both
directions:

| Account | History | Rate path |
|---|---|---|
| `AYohrfmz…` | Jan 31 → Aug 18 | 0.452% → 0.330%, then flat for ~6.5 months |
| `XEdF7B6c…` | May 24 → Aug 18 | 0.331% held ~3 months, then **repriced to 0.240%** |
| `F5oDUDyg…` | Jun 10 → Aug 18 | **0.052% → 0.503%** — lost a deep discount |
| `AuuTZTnB…` | Aug 11 → Aug 18 | **0.050% → 0.503%** — lost it after 3 days |

Two accounts *lost* a ~0.05% rate and reverted to the 0.503% default. Volume before
losing it: **$997** and **$5,412** — a 5× spread, so no threshold. Rates move up as well
as down, at arbitrary times, unrelated to how much the account traded.

This is the same mechanism the 71-day perp data showed (2 grants, **26 revocations**),
now corroborated independently on spot. The spot n is small — four accounts — so the perp
dataset carries the statistical weight and spot confirms the pattern.

**Combined conclusion: FOMO's fee rate is a per-account setting the company grants and
revokes by hand, on both spot and perps. The advertised rate is a default, not a price.**

### Business impact

In the 24.7-hour window, **53% of notional was discounted**. Blended **0.379%** against a
0.500% headline — FOMO forwent **$3,165 in a day**, roughly **$1.15M/year** on Solana spot
alone.

*Caveat:* the highest-frequency accounts show wide intra-account rate ranges
(0.02%–5.9%), which is more likely multi-leg attribution error in the parser than real
pricing. The per-account finding rests on the 87% with stable rates; exclude the
high-frequency tail before quoting it.

## Next

- **Phase 4** — live poller over `clearinghouseState` for the rolling roster. Lower priority
  now that cohort skew is ruled out; its main output needs rethinking.

## Sources

- [Hyperliquid builder codes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes)
- [DefiLlama dimension-adapters](https://github.com/DefiLlama/dimension-adapters) — builder addresses, Solana fee wallet, dedupe logic
