#!/usr/bin/env python3
"""
Compare every trading app routing through Hyperliquid.

Hyperliquid publishes a builder-fills CSV for *every* app that routes orders
through it, not just one. Same URL, same schema. So the question "what does
this app cost you and how do its users do" can be asked of all of them at once,
which is the only way any single answer means anything.

Fills are aggregated per wallet as they stream in and never stored raw -- the
top builders publish several MB/day compressed and there is no reason to keep
it on disk.

Usage:
    python3 builders_compare.py --days 14 --top 12
    python3 builders_compare.py --report
"""

import argparse
import csv
import datetime as dt
import io
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

REGISTRY = ("https://raw.githubusercontent.com/DefiLlama/dimension-adapters"
            "/master/factory/hyperliquid.ts")
BASE = "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills"
# Hyperliquid's own venue fees, bps: (taker, is_hip3). HIP-3 markets bill double.
HL_FEES = {(1, 1): 9.0, (1, 0): 4.5, (0, 1): 3.0, (0, 0): 1.5}

DDL = """
CREATE TABLE IF NOT EXISTS agg (
    builder TEXT, address TEXT, wallet TEXT,
    fills INTEGER, notional REAL, pnl REAL, builder_fee REAL, venue_fee REAL,
    taker INTEGER, days INTEGER,
    PRIMARY KEY (builder, wallet)
);
CREATE INDEX IF NOT EXISTS ix_agg_builder ON agg(builder);
"""


def decompress(blob):
    try:
        import lz4.frame
        return lz4.frame.decompress(blob)
    except ImportError:
        pass
    try:
        return subprocess.run(["lz4", "-d", "-c", "-"], input=blob,
                              capture_output=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("need lz4: pip install lz4")
    except subprocess.CalledProcessError:
        return b""


def registry():
    """{name: address} for every builder DefiLlama tracks."""
    src = urllib.request.urlopen(REGISTRY, timeout=60).read().decode()
    out = {}
    for n, a in re.findall(
            r'"([a-z0-9\-]+)":\s*\{[^}]*?addresses:\s*\[\s*"(0x[0-9a-f]{40})"', src, re.S):
        out.setdefault(n, a)
    return out


def head_size(addr, day):
    url = f"{BASE}/{addr}/{day}.csv.lz4"
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "x"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


def fetch(addr, day):
    url = f"{BASE}/{addr}/{day}.csv.lz4"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fomo-oracle/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return decompress(r.read())
    except urllib.error.HTTPError:
        return b""
    except Exception:
        return b""


def accumulate(raw, acc, day):
    """Fold one day's CSV into per-wallet aggregates. Never stores raw fills."""
    if not raw:
        return 0
    n = 0
    for r in csv.DictReader(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")):
        try:
            px = float(r["px"]); sz = float(r["sz"])
            fee = float(r["builder_fee"] or 0)
            pnl = float(r["closed_pnl"] or 0)
        except (TypeError, ValueError, KeyError):
            continue
        no = px * sz
        cr = 1 if r.get("crossed") == "true" else 0
        hip3 = 1 if ":" in r.get("coin", "") else 0
        a = acc[r["user"]]
        a[0] += 1; a[1] += no; a[2] += pnl; a[3] += fee
        a[4] += no * HL_FEES[(cr, hip3)] / 1e4
        a[5] += cr
        a[6].add(day)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--db", default="builders.db")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.executescript(DDL)
    if a.report:
        report(con); return

    end = dt.date.today() - dt.timedelta(days=2)     # last reliably published day
    days = [(end - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(a.days)]
    reg = registry()
    print(f"{len(reg)} builders in registry · probing {days[0]}")

    with ThreadPoolExecutor(max_workers=16) as p:
        sizes = list(p.map(lambda kv: (head_size(kv[1], days[0]), kv[0], kv[1]),
                           reg.items()))
    live = sorted([s for s in sizes if s[0] > 0], reverse=True)[:a.top]
    print(f"{len([s for s in sizes if s[0]>0])} publishing · taking top {len(live)}\n")

    for sz, name, addr in live:
        acc = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0, 0, set()])
        with ThreadPoolExecutor(max_workers=8) as p:
            got = list(p.map(lambda d: (d, fetch(addr, d)), days))
        total = sum(accumulate(raw, acc, d) for d, raw in got)
        rows = [(name, addr, w, v[0], v[1], v[2], v[3], v[4], v[5], len(v[6]))
                for w, v in acc.items()]
        con.execute("DELETE FROM agg WHERE builder = ?", (name,))
        con.executemany("INSERT OR REPLACE INTO agg VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        vol = sum(v[1] for v in acc.values())
        print(f"  {name:<28} {total:>8,} fills  {len(acc):>6,} wallets  ${vol:>13,.0f}")

    print()
    report(con)


def report(con):
    q = con.execute("""SELECT builder, COUNT(*), SUM(fills), SUM(notional), SUM(pnl),
                              SUM(builder_fee), SUM(venue_fee), SUM(taker)
                       FROM agg GROUP BY builder ORDER BY SUM(notional) DESC""").fetchall()
    if not q:
        print("no data"); return

    print("=" * 100)
    print("WHAT EACH APP CHARGES, AND HOW ITS TRADERS DO")
    print("=" * 100)
    print(f"{'app':<26} {'volume':>13} {'wallets':>8} {'fee bps':>8} "
          f"{'taker%':>7} {'profitable':>11} {'fees vs pnl':>12}")
    for b, w, f, no, pnl, bf, vf, tk in q:
        prof = con.execute("""SELECT SUM(CASE WHEN pnl-builder_fee-venue_fee>0 THEN 1 ELSE 0 END),
                                     COUNT(*) FROM agg WHERE builder=? AND pnl!=0""", (b,)).fetchone()
        pr = f"{prof[0]/prof[1]*100:.0f}%" if prof[1] else "-"
        ratio = f"{(bf+vf)/abs(pnl):.1f}x" if pnl else "-"
        print(f"{b:<26} ${no:>12,.0f} {w:>8,} {bf/no*1e4:>8.2f} "
              f"{tk/f*100:>6.1f}% {pr:>11} {ratio:>12}")

    print(f"\n{'app':<26} {'traders made':>14} {'fees paid':>13} {'net':>14}")
    for b, w, f, no, pnl, bf, vf, tk in q:
        print(f"{b:<26} ${pnl:>+13,.0f} ${bf+vf:>12,.0f} ${pnl-bf-vf:>+13,.0f}")
    print("\nfee bps = builder markup only, on top of Hyperliquid's own venue fee.")
    print("profitable = share of wallets that closed a position and finished net "
          "positive after both.")


if __name__ == "__main__":
    main()
