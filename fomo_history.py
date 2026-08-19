#!/usr/bin/env python3
"""
Phase 3b — did any account's spot fee rate ever CHANGE?

The 24.7-hour fee-wallet sample showed rates are per-account and uncorrelated with
volume, but could not distinguish "granted" from "earned long ago" — every account
was observed for one day. Paging the fee wallet deeper is intractable (~1 tx/sec
means 71 days is ~6M transactions).

So invert the query: take the traders already identified and read THEIR wallet
histories directly. Each is a handful of RPC calls, and it reaches back as far as
the account goes.

A rate that changes mid-history reveals a threshold. A rate that is constant across
an account's whole life, at a level uncorrelated with its volume, means the number
was set when the account was created -- i.e. granted.

Usage:
    python3 fomo_history.py --per-wallet 12
"""

import argparse
import json
import sqlite3
import statistics
import threading
import time
import urllib.request
from collections import defaultdict

from fomo_spot import parse, FEE_OWNER, POOL

DDL = """
CREATE TABLE IF NOT EXISTS history (
    sig      TEXT PRIMARY KEY,
    trader   TEXT NOT NULL,
    ts       INTEGER,
    mint     TEXT,
    direction TEXT,
    fee_usdc REAL,
    notional REAL,
    rate_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_hist_trader ON history(trader);
CREATE TABLE IF NOT EXISTS scanned (trader TEXT PRIMARY KEY, sigs INTEGER, oldest INTEGER);
"""
HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


def rpc(url, payload, to=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=HDRS)
    with urllib.request.urlopen(req, timeout=to) as r:
        return json.load(r)


def sigs_for(wallet, limit=1000):
    for url, _ in POOL:
        try:
            out = rpc(url, {"jsonrpc": "2.0", "id": 1,
                            "method": "getSignaturesForAddress",
                            "params": [wallet, {"limit": limit}]}, to=45)
            if "result" in out:
                return [s for s in out["result"] if not s.get("err")]
        except Exception:
            continue
    return []


def spread(items, k):
    """k items spread evenly across the list, always including both ends."""
    if len(items) <= k:
        return items
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot-db", default="fomo_spot.db")
    ap.add_argument("--db", default="fomo_history.db")
    ap.add_argument("--per-wallet", type=int, default=12)
    ap.add_argument("--max-seconds", type=int, default=3000)
    a = ap.parse_args()

    src = sqlite3.connect(a.spot_db)
    traders = [r[0] for r in src.execute(
        """SELECT trader FROM spot WHERE notional >= 150 GROUP BY trader
           HAVING COUNT(*) >= 3 AND (MAX(rate_pct) - MIN(rate_pct)) < 0.02
           ORDER BY SUM(notional) DESC""")]
    win_start = src.execute("SELECT MIN(ts) FROM spot").fetchone()[0]
    src.close()

    con = sqlite3.connect(a.db)
    con.executescript(DDL)
    done = {r[0] for r in con.execute("SELECT trader FROM scanned")}
    todo = [t for t in traders if t not in done]
    print(f"{len(traders):,} rate-stable traders · {len(todo):,} to scan\n")

    # ---- step 1: each wallet's signature list, and how far back it goes
    wanted, t0 = [], time.time()
    for i, w in enumerate(todo, 1):
        ss = sigs_for(w)
        oldest = min((s.get("blockTime") or 0) for s in ss) if ss else 0
        con.execute("INSERT OR REPLACE INTO scanned VALUES (?,?,?)", (w, len(ss), oldest))
        # only worth fetching if the account predates the fee-wallet sample window
        if oldest and oldest < win_start - 3600:
            older = [s["signature"] for s in ss
                     if (s.get("blockTime") or 0) < win_start]
            wanted += [(w, sg) for sg in spread(older, a.per_wallet)]
        if i % 25 == 0:
            con.commit()
            print(f"  scanned {i:,}/{len(todo):,} wallets · "
                  f"{len(wanted):,} historical txs queued · "
                  f"{(time.time()-t0)/60:.1f}m", flush=True)
    con.commit()
    pre = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    have = {r[0] for r in con.execute("SELECT sig FROM history")}
    wanted = [(w, s) for w, s in wanted if s not in have]
    print(f"\n  {len(wanted):,} historical transactions to fetch\n")

    # ---- step 2: fetch them through the same dual-endpoint pool
    lock, pending, got = threading.Lock(), list(wanted), []
    stop = threading.Event()

    def claim(n):
        with lock:
            out = pending[:n]; del pending[:n]
        return out

    def params(sg):
        return [sg, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]

    def w_batch(url, size=25):
        while not stop.is_set():
            chunk = claim(size)
            if not chunk:
                time.sleep(0.4); continue
            body = [{"jsonrpc": "2.0", "id": j, "method": "getTransaction",
                     "params": params(s)} for j, (_, s) in enumerate(chunk)]
            try:
                out = rpc(url, body)
                byid = {o["id"]: o for o in out if isinstance(o, dict) and "id" in o}
                with lock:
                    for j, (w, s) in enumerate(chunk):
                        res = (byid.get(j) or {}).get("result")
                        got.append((w, s, res)) if res else pending.append((w, s))
            except Exception:
                with lock:
                    pending.extend(chunk)
                time.sleep(1.0)

    def w_single(url, threads=8):
        def run():
            while not stop.is_set():
                ch = claim(1)
                if not ch:
                    time.sleep(0.4); continue
                w, s = ch[0]
                try:
                    res = rpc(url, {"jsonrpc": "2.0", "id": 1,
                                    "method": "getTransaction",
                                    "params": params(s)}, to=40).get("result")
                    with lock:
                        got.append((w, s, res)) if res else pending.append((w, s))
                except Exception:
                    with lock:
                        pending.append((w, s))
                    time.sleep(0.4)
        ts = [threading.Thread(target=run, daemon=True) for _ in range(threads)]
        [t.start() for t in ts]; [t.join() for t in ts]

    for url, mode in POOL:
        threading.Thread(target=(w_batch if mode == "batch" else w_single),
                         args=(url,), daemon=True).start()

    def drain():
        with lock:
            batch = list(got); got.clear()
        rows = []
        for w, sg, tx in batch:
            p = parse(tx)
            if p:
                rows.append((sg, w, tx.get("blockTime"), p["mint"], p["direction"],
                             p["fee_usdc"], p["notional"], p["rate_pct"]))
        if rows:
            con.executemany("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?)", rows)
            con.commit()
        return len(batch), len(rows)

    t1, seen, kept = time.time(), 0, 0
    while time.time() - t1 < a.max_seconds:
        with lock:
            left, ready = len(pending), len(got)
        if ready >= 300 or (left == 0 and ready):
            s_, k_ = drain(); seen += s_; kept += k_
            print(f"  {seen:,}/{len(wanted):,} fetched · {kept:,} parsed · "
                  f"{left:,} pending · {(time.time()-t1)/60:.0f}m", flush=True)
        if left == 0 and ready == 0:
            break
        time.sleep(2)
    stop.set(); time.sleep(1)
    s_, k_ = drain(); seen += s_; kept += k_
    print(f"  done: {seen:,} fetched, {kept:,} parsed, "
          f"{con.execute('SELECT COUNT(*) FROM history').fetchone()[0] - pre:,} new rows\n")
    report(con, a)


def report(con, a):
    q = lambda s, *p: con.execute(s, p).fetchall()
    src = sqlite3.connect(a.spot_db)
    now_rate = {t: r for t, r in src.execute(
        """SELECT trader, AVG(rate_pct) FROM spot WHERE notional>=150 GROUP BY trader""")}
    src.close()

    hist = defaultdict(list)
    for t, ts, r, no in q("""SELECT trader, ts, rate_pct, notional FROM history
                             WHERE notional >= 150 ORDER BY ts"""):
        hist[t].append((ts, r, no))

    usable = {t: v for t, v in hist.items() if len(v) >= 3 and t in now_rate}
    print("=" * 74)
    print("DID ANY ACCOUNT'S RATE CHANGE?")
    print("=" * 74)
    print(f"  accounts with >=3 historical trades over $150: {len(usable):,}")

    def halves(v, now):
        """Median rate of the older half vs the newer half (+ today's rate).

        Median, not max-min: a single mis-parsed multi-leg trade must not
        masquerade as a repricing.
        """
        rs = [r for _, r, _ in v]
        mid = len(rs) // 2
        old_h, new_h = rs[:max(1, mid)], rs[mid:] + [now]
        return statistics.median(old_h), statistics.median(new_h)

    changed, const = [], []
    for t, v in usable.items():
        o, n = halves(v, now_rate[t])
        (changed if abs(n - o) >= 0.02 else const).append((t, v, (o, n)))
    print(f"    constant across their whole history : {len(const):,} "
          f"({len(const)/max(1,len(usable))*100:.0f}%)")
    print(f"    rate CHANGED                        : {len(changed):,} "
          f"({len(changed)/max(1,len(usable))*100:.0f}%)")

    ages = sorted((v[-1][0] - v[0][0]) / 86400 for v in usable.values())
    print(f"\n  Observed history per account: median {statistics.median(ages):.1f} days, "
          f"p90 {ages[int(len(ages)*.9)]:.1f}, max {max(ages):.1f}")
    longlived = [a for a in ages if a >= 7]
    print(f"  {len(longlived):,} accounts ({len(longlived)/len(ages)*100:.0f}%) "
          f"observed over a week or more")

    if changed:
        print(f"\n  {'trader':<16} {'span':>7} {'oldest':>8} {'now':>8} {'notional before':>16}")
        for t, v, (o, n) in sorted(changed, key=lambda x: -len(x[1]))[:15]:
            days = (v[-1][0] - v[0][0]) / 86400
            before = sum(x[2] for x in v[:max(1, len(v)//2)])
            print(f"  {t[:14]}… {days:>6.1f}d {o:>7.3f}% {n:>7.3f}% "
                  f"${before:>15,.0f}")

    if const:
        spans = [(v[-1][0] - v[0][0]) / 86400 for _, v, _ in const]
        print(f"\n  Constant-rate accounts were observed over a median of "
              f"{statistics.median(spans):.0f} days (max {max(spans):.0f}).")

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    if not usable:
        print("\n  Not enough history recovered to decide.\n")
    elif len(changed) / len(usable) < 0.05:
        print(f"""
  {len(const):,} of {len(usable):,} accounts hold ONE rate across their entire observed
  history (median {statistics.median([(v[-1][0]-v[0][0])/86400 for _,v,_ in const]):.0f} days), at levels uncorrelated with their volume.

  Nobody is crossing a threshold. The rate is set at the account and does not
  move -- the same conclusion the 71-day perp data reached, now independently
  on spot: FOMO's advertised rate is a default, not the price.
""")
    else:
        print(f"""
  {len(changed):,} of {len(usable):,} accounts ({len(changed)/len(usable)*100:.0f}%) changed rate mid-history.
  Inspect the table above -- if the volume traded before each change clusters,
  that is a threshold and the discount is earned.
""")
    con.close()


if __name__ == "__main__":
    main()
