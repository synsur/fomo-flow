#!/usr/bin/env python3
"""
Phase 3 — FOMO's Solana spot flow, and the take rate nobody publishes.

Perps are ~10% of FOMO and barred to U.S. Persons. Spot is where the ~100K
daily actives are. This samples FOMO's spot trades straight off Solana RPC.

How the trades are identified (verified against live transactions):

  * every FOMO swap pays a USDC fee to a token account owned by
    R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX  (12/12 sampled txs)
  * every one routes through program proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u
  * gas is sponsored, so the SIGNER is FOMO's relayer, not the trader. The
    trader has to be recovered from token-balance deltas.

Trader recovery is two-pass: infrastructure accounts (pools, routers, the fee
wallet) recur across many transactions, real users mostly appear once. Pass 1
counts owner frequency, pass 2 attributes each trade to the non-infrastructure
owner whose balances moved.

Notional is measured on BUYS only, where it is unambiguous: the trader is the
sole non-fee-wallet owner whose USDC balance falls, and
notional = |USDC out| - fee. FOMO charges the same 0.50% on buys and sells, so
the buy-side rate is the rate.

Usage:
    python3 fomo_spot.py --limit 600
    python3 fomo_spot.py --limit 2000 --rpc https://your-endpoint
    python3 fomo_spot.py --report            # re-report from the db
"""

import argparse
import json
import sqlite3
import statistics
import time
import urllib.request
from collections import Counter

FEE_OWNER = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ROUTER = "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
# Keyless pool. mainnet-beta accepts JSON-RPC batches (~4.4 tx/s, ~60% hit rate);
# publicnode rejects batches but serves singles at ~3.3 tx/s with ~100% hit rate.
# Running both concurrently roughly doubles throughput and needs no API key.
POOL = [("https://api.mainnet-beta.solana.com", "batch"),
        ("https://solana-rpc.publicnode.com", "single")]

DDL = """
CREATE TABLE IF NOT EXISTS spot (
    sig       TEXT PRIMARY KEY,
    slot      INTEGER,
    ts        INTEGER,
    trader    TEXT,
    mint      TEXT,
    direction TEXT,     -- buy | sell | ?
    fee_usdc  REAL,
    notional  REAL,     -- USD, buys only
    rate_pct  REAL      -- fee / notional * 100
);
CREATE INDEX IF NOT EXISTS ix_spot_trader ON spot(trader);
CREATE INDEX IF NOT EXISTS ix_spot_mint   ON spot(mint);
"""


class Rpc:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, params, tries=5):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        for i in range(tries):
            try:
                req = urllib.request.Request(
                    self.url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    out = json.load(r)
                if "result" in out:
                    return out["result"]
            except Exception:
                pass
            time.sleep(1.2 * (i + 1))
        return None


def balances(meta):
    """{(accountIndex, mint): (owner, delta)} across pre/post token balances."""
    pre, post, owner = {}, {}, {}
    for tag, dest in (("preTokenBalances", pre), ("postTokenBalances", post)):
        for b in meta.get(tag) or []:
            k = (b["accountIndex"], b["mint"])
            dest[k] = float(b["uiTokenAmount"]["uiAmountString"] or 0)
            owner[k] = b.get("owner")
    return {k: (owner[k], post.get(k, 0) - pre.get(k, 0))
            for k in set(pre) | set(post)}


def parse(tx, infra=None):
    """-> dict or None.

    Trader identification is deterministic, no heuristic needed. In a USDC swap
    the pools always sit on the opposite side of the user:

      BUY   user: USDC down, token up   |  pool: USDC up, token down
      SELL  user: USDC up,  token down  |  pool: USDC down, token up

    so "USDC down AND some token up" identifies the buyer uniquely, even through
    a multi-hop route (intermediate pools each gain on one leg and lose on the
    other). The fee wallet only ever gains USDC, and is excluded.

    SOL-denominated swaps are not captured -- this reads the USDC leg only.
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    deltas = balances(meta)

    fee = 0.0
    moved = {}
    for (idx, mint), (own, d) in deltas.items():
        if not own or abs(d) < 1e-12:
            continue
        if own == FEE_OWNER:
            if mint == USDC and d > 0:
                fee += d
            continue
        moved.setdefault(own, {})
        moved[own][mint] = moved[own].get(mint, 0) + d
    if fee <= 0:
        return None

    def legs(o):
        return {m: v for m, v in moved[o].items() if m != USDC and abs(v) > 1e-12}

    buyers = [o for o in moved if moved[o].get(USDC, 0) < 0
              and any(v > 0 for v in legs(o).values())]
    sellers = [o for o in moved if moved[o].get(USDC, 0) > 0
               and any(v < 0 for v in legs(o).values())]

    if len(buyers) == 1:
        t = buyers[0]
        notional = -moved[t][USDC] - fee
        gained = [m for m, v in legs(t).items() if v > 0]
        if notional > 0 and len(gained) == 1:
            return dict(trader=t, mint=gained[0], direction="buy", fee_usdc=fee,
                        notional=notional, rate_pct=fee / notional * 100)
    if len(sellers) == 1:
        t = sellers[0]
        notional = moved[t][USDC] + fee      # proceeds are net of the fee
        sold = [m for m, v in legs(t).items() if v < 0]
        if notional > 0 and len(sold) == 1:
            return dict(trader=t, mint=sold[0], direction="sell", fee_usdc=fee,
                        notional=notional, rate_pct=fee / notional * 100)
    return None


def report(con):
    q = lambda s, *p: con.execute(s, p).fetchall()
    one = lambda s, *p: con.execute(s, p).fetchone()

    n, traders, fees = one("SELECT COUNT(*), COUNT(DISTINCT trader), SUM(fee_usdc) FROM spot")
    nb, = one("SELECT COUNT(*) FROM spot WHERE notional IS NOT NULL")
    print(f"\nsample: {n:,} FOMO spot trades · {traders:,} traders · "
          f"${fees:,.2f} in fees · {nb:,} with measurable notional")
    if not nb:
        return

    print("\n" + "=" * 74)
    print("THE REAL TAKE RATE, BY TRADE SIZE")
    print("=" * 74)
    print(f"  {'trade size':<16} {'trades':>7} {'median fee':>11} {'median rate':>12} {'effective':>10}")
    for lo, hi, lbl in [(0,25,"< $25"),(25,50,"$25-50"),(50,100,"$50-100"),
                        (100,250,"$100-250"),(250,1e3,"$250-1K"),
                        (1e3,1e4,"$1K-10K"),(1e4,1e9,"> $10K")]:
        rows = q("""SELECT fee_usdc, notional, rate_pct FROM spot
                    WHERE notional >= ? AND notional < ?""", lo, hi)
        if not rows:
            continue
        agg = sum(r[0] for r in rows) / sum(r[1] for r in rows) * 100
        print(f"  {lbl:<16} {len(rows):>7,} "
              f"${statistics.median(r[0] for r in rows):>10.2f} "
              f"{statistics.median(r[2] for r in rows):>11.2f}% {agg:>9.2f}%")
    allr = one("SELECT SUM(fee_usdc), SUM(notional) FROM spot WHERE notional IS NOT NULL")
    print(f"\n  blended across the sample: {allr[0]/allr[1]*100:.3f}%  "
          f"(${allr[0]:,.2f} on ${allr[1]:,.0f})")

    sizes = [r[0] for r in q("SELECT notional FROM spot WHERE notional IS NOT NULL ORDER BY notional")]
    print(f"\n  trade size    median ${statistics.median(sizes):,.0f}"
          f"   mean ${statistics.fmean(sizes):,.0f}"
          f"   p90 ${sizes[int(len(sizes)*.9)]:,.0f}")
    small = sum(1 for s in sizes if s < 100)
    print(f"  {small:,} of {len(sizes):,} trades ({small/len(sizes)*100:.1f}%) are under $100 "
          f"-- the flat-minimum zone")

    print("\n" + "=" * 74)
    print("WHAT THEY TRADE")
    print("=" * 74)
    print(f"  {'mint':<46} {'trades':>7} {'fees':>10}")
    for m, c, f in q("""SELECT mint, COUNT(*), SUM(fee_usdc) FROM spot
                        GROUP BY mint ORDER BY 2 DESC LIMIT 12"""):
        print(f"  {m:<46} {c:>7,} ${f:>9,.2f}")

    print("\n" + "=" * 74)
    print("CONTENT-READY")
    print("=" * 74)
    u100 = one("SELECT SUM(fee_usdc), SUM(notional) FROM spot WHERE notional < 100")
    o100 = one("SELECT SUM(fee_usdc), SUM(notional) FROM spot WHERE notional >= 100")
    print(f"\n  On {nb:,} FOMO spot trades parsed straight off Solana:\n")
    if o100 and o100[1]:
        print(f"  · Measured take rate on trades >= $100: {o100[0]/o100[1]*100:.3f}%")
    if u100 and u100[1]:
        print(f"  · Trades under $100 pay an effective {u100[0]/u100[1]*100:.2f}% -- "
              f"{(u100[0]/u100[1])/(o100[0]/o100[1]):.1f}x more, purely from the flat minimum.")
        print(f"  · {small/len(sizes)*100:.0f}% of trades fall in that penalty zone.")
    else:
        print("  · NO sub-$100 trades in this sample. Either the buy-only/single-owner")
        print("    filter is biased toward larger clean trades, or small orders route")
        print("    differently. Do not publish a size distribution off this sample --")
        print("    scale the ingest with a real RPC key first.")
    print(f"  · Median trade size ${statistics.median(sizes):,.0f} (sample of {len(sizes):,}).")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=600, help="signatures to sample")
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--db", default="fomo_spot.db")
    ap.add_argument("--report", action="store_true", help="report only")
    ap.add_argument("--max-seconds", type=int, default=5400, dest="max_seconds")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.executescript(DDL)
    if a.report:
        report(con)
        return

    rpc = Rpc(a.rpc)
    print(f"rpc {a.rpc}\nfee wallet {FEE_OWNER}\nrouter {ROUTER}\n")

    sigs, before = [], None
    while len(sigs) < a.limit:
        opts = {"limit": min(1000, a.limit - len(sigs))}
        if before:
            opts["before"] = before
        batch = rpc("getSignaturesForAddress", [FEE_OWNER, opts]) or []
        if not batch:
            break
        sigs += [s["signature"] for s in batch if not s.get("err")]
        before = batch[-1]["signature"]
        print(f"  signatures: {len(sigs):,}")
    have = {s for (s,) in con.execute("SELECT sig FROM spot")}
    sigs = [s for s in sigs if s not in have]
    print(f"  {len(sigs):,} new to fetch\n")

    import threading
    lock = threading.Lock()
    got, pending = {}, list(sigs)
    HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

    def claim(n):
        with lock:
            out = pending[:n]
            del pending[:n]
        return out

    def give(sg, res):
        with lock:
            if res:
                got[sg] = res
            else:
                pending.append(sg)

    def post(url, payload, to=60):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=HDRS)
        with urllib.request.urlopen(req, timeout=to) as r:
            return json.load(r)

    def params(sg):
        return [sg, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]

    stop = threading.Event()

    def worker_batch(url, size=25):
        while not stop.is_set():
            chunk = claim(size)
            if not chunk:
                time.sleep(0.5)
                continue
            body = [{"jsonrpc": "2.0", "id": j, "method": "getTransaction",
                     "params": params(sg)} for j, sg in enumerate(chunk)]
            try:
                out = post(url, body)
                byid = {o["id"]: o for o in out if isinstance(o, dict) and "id" in o}
                for j, sg in enumerate(chunk):
                    give(sg, (byid.get(j) or {}).get("result"))
            except Exception:
                for sg in chunk:
                    give(sg, None)
                time.sleep(1.0)

    def worker_single(url, threads=8):
        def run():
            while not stop.is_set():
                chunk = claim(1)
                if not chunk:
                    time.sleep(0.5)
                    continue
                sg = chunk[0]
                try:
                    give(sg, post(url, {"jsonrpc": "2.0", "id": 1,
                                        "method": "getTransaction",
                                        "params": params(sg)}, to=40).get("result"))
                except Exception:
                    give(sg, None)
                    time.sleep(0.5)
        ts = [threading.Thread(target=run, daemon=True) for _ in range(threads)]
        [t.start() for t in ts]
        [t.join() for t in ts]

    threads = []
    for url, mode in POOL:
        fn = worker_batch if mode == "batch" else worker_single
        t = threading.Thread(target=fn, args=(url,), daemon=True)
        t.start(); threads.append(t)

    def drain():
        """Parse whatever has arrived and commit it. Safe to call repeatedly."""
        with lock:
            batch = list(got.items())
            got.clear()
        rows = []
        for sg, tx in batch:
            p = parse(tx)
            if p:
                rows.append((sg, tx.get("slot"), tx.get("blockTime"), p["trader"],
                             p["mint"], p["direction"], p["fee_usdc"],
                             p["notional"], p["rate_pct"]))
        if rows:
            con.executemany("INSERT OR REPLACE INTO spot VALUES (?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        return len(batch), len(rows)

    target, t0 = len(sigs), time.time()
    seen = kept = 0
    while time.time() - t0 < a.max_seconds:
        with lock:
            left = len(pending)
            ready = len(got)
        if ready >= 400 or (left == 0 and ready):
            s_, k_ = drain()
            seen += s_; kept += k_
            el = time.time() - t0
            print(f"  {seen:,}/{target:,} fetched · {kept:,} parsed · {left:,} pending "
                  f"· {seen/max(el,1):.1f} tx/s · {el/60:.0f}m", flush=True)
        if left == 0 and ready == 0:
            break
        time.sleep(2)
    stop.set()
    time.sleep(1)
    s_, k_ = drain()
    seen += s_; kept += k_
    print(f"  done: {seen:,} fetched, {kept:,} parsed in "
          f"{(time.time()-t0)/60:.1f} min", flush=True)


    report(con)
    con.close()


if __name__ == "__main__":
    main()
