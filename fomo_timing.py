#!/usr/bin/env python3
"""
Phase 4 — does FOMO's crowd lead price, or chase it?

The strategy under test: watch for a burst of FOMO users piling into an asset,
get alerted, and buy. That only pays if the burst precedes the move. If the
burst is the move -- users buying because price already ran -- an alert
follower buys the top, and the on-chain flow is a fade signal, not a follow
signal.

Two datasets, both public:

  * Perps: Hyperliquid builder fills (fomo_fomo.db) at second resolution,
    priced with Hyperliquid's own candles. 15m candles are retained ~42 days,
    1h candles cover the whole corpus, so the 15m test runs on the recent
    window and the 1h test is the full-history robustness check.
  * Spot: the one-day Solana fee-wallet sample (fomo_spot.db), priced with
    GeckoTerminal minute candles for the mints with enough distinct buyers.

A "spike" is a bin where the number of DISTINCT wallets opening a long at
market is at least MIN_WALLETS and at least MULT times the coin's trailing
7-day mean per bin. Consecutive spike bins are one episode; the alert fires at
the close of the FIRST spike bin, and the follower buys at the next bin's open.
Wallet count, not notional, so a single whale is not a "trend".

Usage:
    python3 fomo_timing.py --fetch          # pull candles into fomo_candles.db
    python3 fomo_timing.py                  # run the analysis
    python3 fomo_timing.py --mult 6         # stricter spike definition
"""

import argparse
import datetime as dt
import json
import math
import sqlite3
import statistics
import time
import urllib.request
from collections import defaultdict

HL_INFO = "https://api.hyperliquid.xyz/info"
GT = "https://api.geckoterminal.com/api/v2"
GT_HDR = {"Accept": "application/json;version=20230302", "User-Agent": "Mozilla/5.0"}

WSOL = "So11111111111111111111111111111111111111112"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Per-side cost for an alert follower trading through FOMO: HL taker 4.5bp +
# FOMO builder markup 5bp. HIP-3 (xyz:) markets bill double.
PERP_COST_BPS = {0: 9.5, 1: 14.0}
SPOT_COST_BPS = 50.0     # FOMO's 0.50% per side on spot, before slippage

DDL = """
CREATE TABLE IF NOT EXISTS hl_candles (
    coin TEXT, iv TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, n INTEGER,
    PRIMARY KEY (coin, iv, t));
CREATE TABLE IF NOT EXISTS gt_pools (
    mint TEXT PRIMARY KEY, pool TEXT, name TEXT, liq REAL, fetched TEXT);
CREATE TABLE IF NOT EXISTS gt_candles (
    mint TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (mint, t));
"""


def rule(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def ep(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def hl_candles(coin, iv, start_ms, end_ms):
    body = json.dumps({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": iv, "startTime": start_ms, "endTime": end_ms}}).encode()
    for i in range(5):
        try:
            req = urllib.request.Request(HL_INFO, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            time.sleep(2 * (i + 1))
            err = e
    raise err


def fetch_hl(con, fills_db, start, end, min_users=150):
    """15m + 1h candles for every coin with enough distinct long-openers."""
    f = sqlite3.connect(fills_db)
    coins = [r[0] for r in f.execute("""
        SELECT coin FROM fills WHERE side='Bid' AND crossed=1 AND COALESCE(closed_pnl,0)=0
        GROUP BY coin HAVING COUNT(DISTINCT user) >= ? ORDER BY COUNT(DISTINCT user) DESC""",
        (min_users,))]
    print(f"hl: {len(coins)} coins with >= {min_users} distinct long-openers")
    s_ms, e_ms = start * 1000, end * 1000
    for coin in coins:
        for iv in ("15m", "1h"):
            got, hi = 0, e_ms
            while True:
                rows = hl_candles(coin, iv, s_ms, hi)
                if not rows:
                    break
                con.executemany("INSERT OR REPLACE INTO hl_candles VALUES (?,?,?,?,?,?,?,?,?)",
                                [(coin, iv, r["t"] // 1000, float(r["o"]), float(r["h"]),
                                  float(r["l"]), float(r["c"]), float(r["v"]), r["n"]) for r in rows])
                got += len(rows)
                if rows[0]["t"] <= s_ms + 3600_000:
                    break
                hi = rows[0]["t"] - 1
            con.commit()
            lo = con.execute("SELECT MIN(t) FROM hl_candles WHERE coin=? AND iv=?", (coin, iv)).fetchone()[0]
            print(f"  {coin:<14} {iv:>3} {got:>5} candles  from {dt.datetime.fromtimestamp(lo, dt.timezone.utc):%Y-%m-%d}")


def gt_get(path, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(GT + path, headers=GT_HDR)
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            time.sleep(4.0)          # the free tier throttles well below its stated 30/min
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (i + 1))
                continue
            if e.code == 404:
                return None
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def fetch_gt(con, spot_db, start, end, min_buyers=15, agg=5):
    """Candles for every spot mint with enough distinct buyers.

    5-minute candles: 1000 per request covers the whole 51h window in one call
    per mint, which matters because GeckoTerminal's free tier 429s hard. The
    analysis forward-fills to a 1-minute grid, so 1m and 5m data mix cleanly."""
    s = sqlite3.connect(spot_db)
    mints = [r[0] for r in s.execute("""
        SELECT mint FROM spot WHERE direction='buy' AND mint NOT IN (?, ?)
        GROUP BY mint HAVING COUNT(DISTINCT trader) >= ? ORDER BY COUNT(DISTINCT trader) DESC""",
        (WSOL, USDT, min_buyers))]
    print(f"gt: {len(mints)} spot mints with >= {min_buyers} distinct buyers")
    for mint in mints:
        row = con.execute("SELECT pool FROM gt_pools WHERE mint=?", (mint,)).fetchone()
        if row:
            pool = row[0]
        else:
            p = gt_get(f"/networks/solana/tokens/{mint}/pools?page=1")
            pools = (p or {}).get("data") or []
            if not pools:
                print(f"  {mint[:10]}  no pool")
                con.execute("INSERT OR REPLACE INTO gt_pools VALUES (?,?,?,?,?)",
                            (mint, None, None, None, dt.datetime.now(dt.timezone.utc).isoformat()))
                con.commit()
                continue
            best = max(pools, key=lambda x: float(x["attributes"].get("reserve_in_usd") or 0))
            a = best["attributes"]
            pool = a["address"]
            con.execute("INSERT OR REPLACE INTO gt_pools VALUES (?,?,?,?,?)",
                        (mint, pool, a.get("name"), float(a.get("reserve_in_usd") or 0),
                         dt.datetime.now(dt.timezone.utc).isoformat()))
            con.commit()
        if pool is None:
            continue
        have = con.execute("SELECT COUNT(*), MIN(t) FROM gt_candles WHERE mint=?", (mint,)).fetchone()
        if have[0] and have[1] is not None and have[1] <= start + 3600:
            print(f"  {mint[:10]}  cached {have[0]} candles")
            continue
        got, before = 0, end
        for _ in range(8):
            o = gt_get(f"/networks/solana/pools/{pool}/ohlcv/minute?aggregate={agg}"
                       f"&before_timestamp={before}&limit=1000&currency=usd")
            L = ((o or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
            if not L:
                break
            con.executemany("INSERT OR REPLACE INTO gt_candles VALUES (?,?,?,?,?,?,?)",
                            [(mint, int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in L])
            con.commit()
            got += len(L)
            oldest = min(int(r[0]) for r in L)
            if oldest <= start:
                break
            before = oldest - 1
        lo = con.execute("SELECT MIN(t) FROM gt_candles WHERE mint=?", (mint,)).fetchone()[0]
        lo_s = dt.datetime.fromtimestamp(lo, dt.timezone.utc).strftime("%m-%d %H:%M") if lo else "-"
        print(f"  {mint[:10]}  {got:>5} {agg}m candles  from {lo_s}", flush=True)


# ---------------------------------------------------------------------------
# analysis helpers
# ---------------------------------------------------------------------------

def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def tstat(xs):
    n = len(xs)
    if n < 3:
        return float("nan")
    m = mean(xs)
    sd = statistics.stdev(xs)
    return m / (sd / math.sqrt(n)) if sd > 0 else float("nan")


def fmt_bps(x):
    return "   n/a" if x != x else f"{x * 1e4:+7.1f}"


def fmt_row(label, rets, horizons, med=False):
    """One row: n, then mean (or median) bps and hit-rate per horizon."""
    cells = []
    for h in horizons:
        xs = [r[h] for r in rets if r.get(h) is not None]
        if not xs:
            cells.append("        n/a       ")
            continue
        hit = sum(1 for x in xs if x > 0) / len(xs) * 100
        if med:
            cells.append(f"{fmt_bps(statistics.median(xs))} {hit:4.0f}%  med   ")
        else:
            cells.append(f"{fmt_bps(mean(xs))} {hit:4.0f}% t{tstat(xs):+5.1f}")
    return f"{label:<26} {len(rets):>5}  " + "  ".join(cells)


def header(horizons):
    return f"{'':<26} {'n':>5}  " + "  ".join(f"{h:^18}" for h in horizons)


# ---------------------------------------------------------------------------
# perps
# ---------------------------------------------------------------------------

def perp_test(con, fills_db, iv, mult, min_wallets, cooloff_bins):
    step = {"15m": 900, "1h": 3600}[iv]
    # horizons in bins
    H = {"15m": [("+15m", 1), ("+1h", 4), ("+4h", 16), ("+24h", 96)],
         "1h": [("+1h", 1), ("+4h", 4), ("+24h", 24)]}[iv]
    PRE = {"15m": [("-24h", 96), ("-4h", 16), ("-1h", 4)],
           "1h": [("-24h", 24), ("-4h", 4), ("-1h", 1)]}[iv]
    hz = [h for h, _ in PRE] + ["bin"] + [h for h, _ in H]

    candles = defaultdict(dict)
    for coin, t, o, c in con.execute("SELECT coin, t, o, c FROM hl_candles WHERE iv=?", (iv,)):
        candles[coin][t] = (o, c)
    coins = sorted(candles)
    if not coins:
        print("no candles -- run --fetch first")
        return

    f = sqlite3.connect(fills_db)
    # distinct long-openers and short-openers per (coin, bin), plus buy VWAP
    opens = defaultdict(lambda: defaultdict(set))    # coin -> bin -> {users}
    shorts = defaultdict(lambda: defaultdict(set))
    vw = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for coin, ts, user, side, px, sz, pnl in f.execute("""
            SELECT coin, ts, user, side, px, sz, closed_pnl FROM fills
            WHERE crossed=1 AND COALESCE(closed_pnl,0)=0"""):
        if coin not in candles:
            continue
        t = int(dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
        b = t // step * step
        if side == "Bid":
            opens[coin][b].add(user)
            vw[coin][b][0] += px * sz
            vw[coin][b][1] += sz
        else:
            shorts[coin][b].add(user)

    def ret(coin, t_entry, t_exit, use="c", entry_px=None):
        """entry: open of bin t_entry (or an explicit price); exit: close (or open) of bin t_exit."""
        cd = candles[coin]
        a = entry_px if entry_px is not None else (cd.get(t_entry) or (None, None))[0]
        bx = cd.get(t_exit)
        if a is None or bx is None:
            return None
        b = bx[1] if use == "c" else bx[0]
        return b / a - 1

    def path(coin, t, r):
        """Fill r with pre (open k bins ago -> spike-bin open), in-bin, and follower post returns."""
        cd = candles[coin]
        for h, k in PRE:
            r[h] = ret(coin, t - k * step, t, use="o")
        r["bin"] = ret(coin, t, t)
        nxt = cd.get(t + step)
        for h, k in H:                            # next bin open -> close k bins later = k*step held
            r[h] = ret(coin, None, t + k * step, entry_px=nxt[0]) if nxt else None

    def episodes(counts, coin):
        """Spike bins -> episode start bins, against a trailing-7d mean that excludes the bin itself."""
        cd = candles[coin]
        ts = sorted(cd)
        lo, hi = ts[0], ts[-1]
        week = 7 * 86400 // step
        series = [(t, len(counts[coin].get(t, ()))) for t in range(lo, hi + step, step)]
        out, last_spike = [], -10 ** 12
        base_sum = 0.0
        for i, (t, u) in enumerate(series):
            if i >= week:
                base = base_sum / week
                spike = u >= min_wallets and u >= mult * max(base, 1e-9)
                if spike:
                    if t - last_spike > cooloff_bins * step:
                        out.append((t, u, base))
                    last_spike = t
                base_sum -= series[i - week][1]
            base_sum += u
        return out

    rows_long, rows_short, rows_base = [], [], []
    per_coin = defaultdict(list)
    for coin in coins:
        cd = candles[coin]
        ts_sorted = sorted(cd)
        for t, u, base in episodes(opens, coin):
            r = {"coin": coin, "t": t, "u": u, "base": base}
            path(coin, t, r)
            crowd = vw[coin][t][0] / vw[coin][t][1] if vw[coin][t][1] else None
            slow = cd.get(t + step)                   # slow follower: buys at next bin CLOSE
            for h, k in H:
                r["crowd" + h] = ret(coin, None, t + k * step, entry_px=crowd) if crowd else None
                r["slow" + h] = ret(coin, None, t + (k + 1) * step, entry_px=slow[1]) if slow else None
            rows_long.append(r)
            per_coin[coin].append(r)
        for t, u, base in episodes(shorts, coin):
            r = {"coin": coin, "t": t, "u": u}
            path(coin, t, r)
            rows_short.append(r)
        # baseline: every bin with a full week behind it, same return construction
        week = 7 * 86400 // step
        for t in ts_sorted[week::4]:              # every 4th bin keeps it cheap
            r = {"coin": coin}
            path(coin, t, r)
            rows_base.append(r)

    lo = min(min(c) for c in candles.values())
    hi = max(max(c) for c in candles.values())
    rule(f"PERPS  {iv} bins  {dt.datetime.fromtimestamp(lo, dt.timezone.utc):%Y-%m-%d} .. "
         f"{dt.datetime.fromtimestamp(hi, dt.timezone.utc):%Y-%m-%d}  ({len(coins)} coins)")
    print(f"spike = >= {min_wallets} distinct wallets opening longs at market in one bin "
          f"AND >= {mult}x the coin's trailing-7d mean per bin; {cooloff_bins}-bin cool-off between episodes")
    print("pre columns: return INTO the spike (open k ago -> spike-bin open). bin: the spike bin's own open->close.")
    print("post columns: alert follower buys at NEXT bin open, holds k. mean bps · hit-rate · t-stat\n")
    print(header(hz))
    print(fmt_row("long-spike episodes", rows_long, hz))
    nb = [r for r in rows_long if r["coin"] != "BTC"]
    print(fmt_row("  excluding BTC", nb, hz))
    print(fmt_row("short-spike episodes", rows_short, hz))
    print(fmt_row("all bins (baseline)", rows_base, hz))

    # crowd's own entry vs follower's
    post = [h for h, _ in H]
    print("\nthe crowd's own fill (spike-bin buy VWAP) vs the alert follower (next open) vs a slow follower (next close):")
    print(header(post))
    print(fmt_row("crowd entry", [{h: r.get("crowd" + h) for h in post} for r in rows_long], post))
    print(fmt_row("follower entry", rows_long, post))
    print(fmt_row(f"slow follower (+1 bin)", [{h: r.get("slow" + h) for h in post} for r in rows_long], post))

    # time-matched control: the same horizons, same moment, in every OTHER coin that did not spike.
    # Strips out "the whole market was moving" and leaves the coin-specific content of the flow.
    spike_at = defaultdict(set)
    for r in rows_long:
        for k in range(-16, 17):
            spike_at[r["coin"]].add(r["t"] + k * step)
    excess = []
    for r in rows_long:
        x = {"coin": r["coin"]}
        for h, k in H:
            others = []
            for c2 in coins:
                if c2 == r["coin"] or r["t"] in spike_at[c2]:
                    continue
                nxt = candles[c2].get(r["t"] + step)
                v = ret(c2, None, r["t"] + k * step, entry_px=nxt[0]) if nxt else None
                if v is not None:
                    others.append(v)
            x[h] = (r[h] - mean(others)) if (r.get(h) is not None and len(others) >= 5) else None
        excess.append(x)
    print("\nexcess over the other coins at the same moment (follower entry; strips market-wide moves):")
    print(header(post))
    print(fmt_row("spike coin minus others", excess, post))

    # day clustering: episodes bunch on the same market-wide days, so n overstates independence
    by_day = defaultdict(list)
    for r in rows_long:
        by_day[dt.datetime.fromtimestamp(r["t"], dt.timezone.utc).date()].append(r)
    busiest = max((len(v) for v in by_day.values()), default=0)
    print(f"\nepisodes fall on {len(by_day)} distinct UTC days; busiest day has {busiest}. "
          f"Day-level (average within day, then across days):")
    print(header(post))
    day_rows = []
    for d, rs in by_day.items():
        x = {}
        for h in post:
            xs = [r[h] for r in rs if r.get(h) is not None]
            x[h] = mean(xs) if xs else None
        day_rows.append(x)
    print(fmt_row("follower, per day", day_rows, post))

    # net of costs for the follower, round trip
    print("\nfollower net of round-trip cost (HL taker + FOMO markup, x2):")
    for h, _ in H:
        xs = []
        for r in rows_long:
            if r.get(h) is None:
                continue
            hip3 = 1 if ":" in r["coin"] else 0
            xs.append(r[h] - 2 * PERP_COST_BPS[hip3] / 1e4)
        if xs:
            print(f"  {h:>5}: mean {mean(xs) * 1e4:+7.1f} bps  hit {sum(x > 0 for x in xs) / len(xs) * 100:4.0f}%  n={len(xs)}")

    # per coin
    print(f"\nper coin (episodes >= 5), follower {H[1][0]} and {H[-1][0]}:")
    for coin, rs in sorted(per_coin.items(), key=lambda kv: -len(kv[1])):
        if len(rs) < 5:
            continue
        a = [r[H[1][0]] for r in rs if r.get(H[1][0]) is not None]
        b = [r[H[-1][0]] for r in rs if r.get(H[-1][0]) is not None]
        p = [r["-1h"] for r in rs if r.get("-1h") is not None]
        print(f"  {coin:<12} n={len(rs):>3}  into(-1h) {fmt_bps(mean(p))}   "
              f"{H[1][0]} {fmt_bps(mean(a))} hit {sum(x > 0 for x in a) / len(a) * 100:3.0f}%   "
              f"{H[-1][0]} {fmt_bps(mean(b))} hit {sum(x > 0 for x in b) / len(b) * 100:3.0f}%")

    # lead/lag: corr(wallets_t, ret_{t+1}) vs corr(wallets_t, ret_{t-1})
    print("\nlead/lag: correlation of long-opener count in bin t with the bin return before vs after")
    print(f"  {'coin':<12} {'corr(prev ret)':>15} {'corr(same ret)':>15} {'corr(next ret)':>15}")
    for coin in coins:
        cd = candles[coin]
        ts_sorted = sorted(cd)
        xs, prev, same, nxt = [], [], [], []
        for i in range(1, len(ts_sorted) - 1):
            t = ts_sorted[i]
            if ts_sorted[i - 1] != t - step or ts_sorted[i + 1] != t + step:
                continue
            xs.append(len(opens[coin].get(t, ())))
            prev.append(cd[t - step][1] / cd[t - step][0] - 1)
            same.append(cd[t][1] / cd[t][0] - 1)
            nxt.append(cd[t + step][1] / cd[t + step][0] - 1)
        if len(xs) < 100 or statistics.pstdev(xs) == 0:
            continue
        c = lambda ys: statistics.correlation(xs, ys)
        if sum(xs) < 500:
            continue
        print(f"  {coin:<12} {c(prev):>+15.3f} {c(same):>+15.3f} {c(nxt):>+15.3f}")


# ---------------------------------------------------------------------------
# spot
# ---------------------------------------------------------------------------

def spot_test(con, spot_db, mult, min_wallets):
    pools = {m: (p, n) for m, p, n in con.execute("SELECT mint, pool, name FROM gt_pools WHERE pool IS NOT NULL")}
    raw = defaultdict(dict)
    for mint, t, o, c in con.execute("SELECT mint, t, o, c FROM gt_candles"):
        raw[mint][t] = (o, c)
    if not raw:
        print("no spot candles -- run --fetch first")
        return
    # dense 1-min close series, forward-filled (GeckoTerminal only emits traded buckets).
    # A candle stamped t with resolution res closes at t+res, so the price is only known
    # from t+res onward -- no look-ahead into the bucket the trade sits in.
    dense = {}
    for mint, cd in raw.items():
        ts = sorted(cd)
        res = min((b - a for a, b in zip(ts, ts[1:])), default=60)
        closes = {t + res: cd[t][1] for t in ts}
        series, last = {}, None
        for t in range(ts[0] + res, ts[-1] + res + 60, 60):
            if t in closes:
                last = closes[t]
            series[t] = last
        dense[mint] = series

    s = sqlite3.connect(spot_db)
    buys = defaultdict(list)
    for mint, ts, trader, notional in s.execute(
            "SELECT mint, ts, trader, notional FROM spot WHERE direction='buy'"):
        if mint in dense:
            buys[mint].append((ts, trader, notional))

    PRE = [("-4h", 240), ("-1h", 60), ("-15m", 15)]
    POST = [("+15m", 15), ("+1h", 60), ("+4h", 240), ("+24h", 1440)]
    hz = [h for h, _ in PRE] + [h for h, _ in POST]

    def px(mint, t):
        return dense[mint].get(t // 60 * 60)

    # per-trade markouts: entry = minute close at the trade's minute
    trades = []
    for mint, L in buys.items():
        for ts, trader, notional in L:
            e = px(mint, ts)
            if not e:
                continue
            r = {"mint": mint, "notional": notional}
            for h, k in PRE:
                p = px(mint, ts - k * 60)
                r[h] = e / p - 1 if p else None
            for h, k in POST:
                p = px(mint, ts + k * 60)
                r[h] = p / e - 1 if p else None
            trades.append(r)

    rule(f"SPOT  one-day fee-wallet sample, {len(dense)} mints priced, {len(trades)} buys with a price")
    print("per-trade markout: pre = return INTO the buy; post = return AFTER the buy.")
    print("Means are dominated by fresh launches (a coin 3h old is up 1000s of % over '4h'); medians are the honest row.\n")
    print(header(hz))
    print(fmt_row("every FOMO spot buy", trades, hz))
    print(fmt_row("  median", trades, hz, med=True))
    big = [r for r in trades if (r["notional"] or 0) >= 500]
    print(fmt_row("  buys >= $500, median", big, hz, med=True))
    print(fmt_row("  buys < $100, median", [r for r in trades if (r["notional"] or 0) < 100], hz, med=True))
    print("\nnet of FOMO's 0.50% each way (1.00% round trip, before slippage):")
    for h, _ in POST:
        xs = [r[h] - 2 * SPOT_COST_BPS / 1e4 for r in trades if r.get(h) is not None]
        if xs:
            print(f"  {h:>5}: mean {mean(xs) * 1e4:+7.1f} bps  hit {sum(x > 0 for x in xs) / len(xs) * 100:4.0f}%  n={len(xs)}")

    # spike bins: 15-min, distinct buyers >= min_wallets and >= mult x mint mean per bin over the sample
    step = 900
    spikes = []
    for mint, L in buys.items():
        bins = defaultdict(set)
        for ts, trader, _ in L:
            bins[ts // step * step].add(trader)
        t0, t1 = min(bins), max(bins)
        nbins = (t1 - t0) // step + 1
        base = sum(len(v) for v in bins.values()) / nbins
        last = -10 ** 12
        for b in sorted(bins):
            u = len(bins[b])
            if u >= min_wallets and u >= mult * base:
                if b - last > 4 * step:
                    e = px(mint, b + step)           # follower buys at next bin start
                    if e:
                        r = {"mint": mint, "u": u}
                        for h, k in PRE:
                            p = px(mint, b - k * 60)
                            r[h] = (px(mint, b) / p - 1) if (p and px(mint, b)) else None
                        for h, k in POST:
                            p = px(mint, b + step + k * 60)
                            r[h] = p / e - 1 if p else None
                        spikes.append(r)
                last = b
    # unconditional baseline: every 15-min bin of every priced mint inside the buy window,
    # same return construction. Separates "FOMO buys mark tops" from "these coins decayed all day".
    base = []
    t_lo = min(ts for L in buys.values() for ts, _, _ in L) // step * step
    t_hi = max(ts for L in buys.values() for ts, _, _ in L) // step * step
    for mint in buys:
        for b in range(t_lo, t_hi + step, step):
            e = px(mint, b + step)
            if not e:
                continue
            r = {"mint": mint}
            for h, k in PRE:
                p = px(mint, b - k * 60)
                r[h] = (px(mint, b) / p - 1) if (p and px(mint, b)) else None
            for h, k in POST:
                p = px(mint, b + step + k * 60)
                r[h] = p / e - 1 if p else None
            base.append(r)

    print(f"\nspike bins (15m, >= {min_wallets} distinct buyers and >= {mult}x the mint's mean per bin), follower at next bin:")
    print(header(hz))
    print(fmt_row("spot spike episodes", spikes, hz))
    print(fmt_row("  median", spikes, hz, med=True))
    print(fmt_row("all bins, same mints", base, hz))
    print(fmt_row("  median", base, hz, med=True))
    print("\nmints priced:")
    for mint in sorted(dense, key=lambda m: -len(buys[m])):
        print(f"  {mint[:12]}  {pools[mint][1] or '?':<22} buys={len(buys[mint]):>4}  candles={len(raw[mint]):>5}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download candles, then exit")
    ap.add_argument("--fetch-spot", action="store_true", help="download only the spot candles, then exit")
    ap.add_argument("--db", default="fomo_candles.db")
    ap.add_argument("--fills", default="fomo_fomo.db")
    ap.add_argument("--spot", default="fomo_spot.db")
    ap.add_argument("--mult", type=float, default=4.0, help="spike = this x trailing mean")
    ap.add_argument("--min-wallets", type=int, default=8)
    ap.add_argument("--cooloff", type=int, default=4, help="bins between episodes")
    ap.add_argument("--no-spot", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    con.executescript(DDL)

    if a.fetch or a.fetch_spot:
        if not a.fetch_spot:
            fetch_hl(con, a.fills, ep("2026-06-04 00:00"), ep("2026-08-29 00:00"))
        fetch_gt(con, a.spot, ep("2026-08-17 20:00"), ep("2026-08-19 23:00"))
        return

    perp_test(con, a.fills, "15m", a.mult, a.min_wallets, a.cooloff)
    perp_test(con, a.fills, "1h", a.mult, a.min_wallets, 1)
    if not a.no_spot:
        spot_test(con, a.spot, max(a.mult * 0.75, 3.0), 5)


if __name__ == "__main__":
    main()
