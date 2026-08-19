#!/usr/bin/env python3
"""
Phase 1 — backfill FOMO's Hyperliquid perp order flow into SQLite.

Hyperliquid publishes every fill routed through a builder code as a public
daily LZ4-compressed CSV. No key, no rate limit:

    https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{builder}/{YYYYMMDD}.csv.lz4

Two builders carry the "fomo" name in DefiLlama's registry. We want the social
trading app (start 2026-06-05), NOT onfomo.com. See BUILDERS below.

Usage:
    python3 fomo_ingest.py                     # backfill from launch to yesterday
    python3 fomo_ingest.py --since 2026-08-01
    python3 fomo_ingest.py --builder other     # the other FOMO, for comparison
    python3 fomo_ingest.py --refresh 2026-08-16  # re-pull one day
"""

import argparse
import csv
import datetime as dt
import io
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# DefiLlama dimension-adapters, factory/hyperliquid.ts
BUILDERS = {
    # "fomo-social-trading-perps", start 2026-06-05 -- fomo.family, the app
    "fomo": "0x2a2b6b093a9813fbd8cddae800c3d17d46460d17",
    # "fomo-perps", no start date -- a DIFFERENT product (onfomo.com)
    "other": "0xb838e4d1c8bcf71fa8e63299d5aa3258c83d6adb",
}
LAUNCH = "2026-06-05"
BASE = "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills"

DDL = """
CREATE TABLE IF NOT EXISTS fills (
    ts                 TEXT NOT NULL,
    day                TEXT NOT NULL,
    user               TEXT NOT NULL,
    coin               TEXT NOT NULL,
    side               TEXT,
    px                 REAL,
    sz                 REAL,
    crossed            INTEGER,   -- 1 = taker
    special_trade_type TEXT,
    tif                TEXT,
    is_trigger         INTEGER,
    counterparty       TEXT,
    closed_pnl         REAL,
    twap_id            TEXT,
    builder_fee        REAL,
    notional           REAL,      -- px * sz
    fee_bps            REAL       -- builder_fee / notional * 10000
);
CREATE INDEX IF NOT EXISTS ix_fills_user ON fills(user);
CREATE INDEX IF NOT EXISTS ix_fills_day  ON fills(day);
CREATE INDEX IF NOT EXISTS ix_fills_coin ON fills(coin);

CREATE TABLE IF NOT EXISTS ingested_days (
    day     TEXT PRIMARY KEY,
    rows    INTEGER,
    bytes   INTEGER,
    fetched TEXT
);
"""


def decompress(blob: bytes) -> bytes:
    """LZ4 frame decode. Prefers the python lib, falls back to the CLI."""
    try:
        import lz4.frame  # type: ignore
        return lz4.frame.decompress(blob)
    except ImportError:
        pass
    try:
        return subprocess.run(
            ["lz4", "-d", "-c", "-"], input=blob,
            capture_output=True, check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("Need LZ4: `pip install lz4` or `brew install lz4`")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"lz4 failed: {e.stderr[:200]!r}") from e


def fetch_day(builder: str, day: str):
    """Returns (day, raw_csv_bytes, compressed_size) or (day, None, 0) if absent."""
    url = f"{BASE}/{builder}/{day}.csv.lz4"
    req = urllib.request.Request(url, headers={"User-Agent": "fomo-oracle/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            blob = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return day, None, 0          # day not published (no trades / pre-launch)
        raise
    return day, decompress(blob), len(blob)


def parse_rows(day: str, raw: bytes):
    """CSV bytes -> tuples ready for executemany. Derives notional and fee_bps."""
    out = []
    for r in csv.DictReader(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")):
        try:
            px = float(r["px"]); sz = float(r["sz"])
            fee = float(r["builder_fee"] or 0)
        except (TypeError, ValueError):
            continue
        notional = px * sz
        out.append((
            r["time"], day, r["user"], r["coin"], r.get("side"),
            px, sz,
            1 if r.get("crossed") == "true" else 0,
            r.get("special_trade_type"), r.get("tif"),
            1 if r.get("is_trigger") == "true" else 0,
            r.get("counterparty"),
            float(r["closed_pnl"] or 0), r.get("twap_id"), fee,
            notional,
            (fee / notional * 10_000) if notional > 0 else None,
        ))
    return out


def daterange(start: str, end: str):
    d = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    while d <= last:
        yield d.strftime("%Y%m%d")
        d += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser(description="Backfill FOMO perp fills into SQLite.")
    ap.add_argument("--builder", default="fomo", choices=sorted(BUILDERS))
    ap.add_argument("--since", default=LAUNCH, help="ISO date, default launch")
    ap.add_argument("--until", default=None, help="ISO date, default yesterday UTC")
    ap.add_argument("--db", default=None, help="default fomo_<builder>.db")
    ap.add_argument("--refresh", action="append", default=[],
                    help="ISO date to re-pull even if present (repeatable)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    builder = BUILDERS[args.builder]
    db_path = args.db or f"fomo_{args.builder}.db"
    until = args.until or (dt.datetime.now(dt.timezone.utc).date()
                           - dt.timedelta(days=1)).isoformat()

    con = sqlite3.connect(db_path)
    con.executescript(DDL)

    # Only days that actually yielded rows are considered done. A day recorded
    # with 0 rows may simply not have been published yet, so retry it.
    have = {d for (d,) in con.execute("SELECT day FROM ingested_days WHERE rows > 0")}
    force = {dt.date.fromisoformat(d).strftime("%Y%m%d") for d in args.refresh}
    want = [d for d in daterange(args.since, until) if d not in have or d in force]

    print(f"builder  {args.builder}  {builder}")
    print(f"db       {db_path}")
    print(f"range    {args.since} .. {until}   ({len(want)} day(s) to fetch, "
          f"{len(have)} already stored)")
    if not want:
        print("nothing to do")
        return

    total_rows = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for day, raw, nbytes in pool.map(lambda d: fetch_day(builder, d), want):
            if raw is None:
                con.execute(
                    "INSERT OR REPLACE INTO ingested_days VALUES (?,?,?,?)",
                    (day, 0, 0, dt.datetime.now(dt.timezone.utc).isoformat()))
                print(f"  {day}  -- no file")
                continue
            rows = parse_rows(day, raw)
            con.execute("DELETE FROM fills WHERE day = ?", (day,))   # idempotent
            con.executemany(
                "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.execute("INSERT OR REPLACE INTO ingested_days VALUES (?,?,?,?)",
                        (day, len(rows), nbytes,
                         dt.datetime.now(dt.timezone.utc).isoformat()))
            con.commit()
            total_rows += len(rows)
            print(f"  {day}  {len(rows):>7,} fills  {nbytes/1024:>7.0f} KB")

    con.commit()
    n, users, notional, fee = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT user), SUM(notional), SUM(builder_fee) "
        "FROM fills").fetchone()
    days = con.execute("SELECT COUNT(*) FROM ingested_days WHERE rows > 0").fetchone()[0]
    print(f"\nadded {total_rows:,} fills this run")
    print(f"corpus: {n:,} fills · {users:,} wallets · {days} active days")
    print(f"        ${notional:,.0f} notional · ${fee:,.0f} builder fees "
          f"({fee/notional*10_000:.2f} bps blended)")
    con.close()


if __name__ == "__main__":
    main()
