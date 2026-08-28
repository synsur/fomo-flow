#!/usr/bin/env python3
"""
Phase 2b — trader outcomes across the full corpus.

The 10-day sample said 37.8% of FOMO perp wallets were net-positive after fees.
That number is the flagship claim of any content built on this data, so it has
to survive recomputation on the whole history rather than one volatile window.

This also asks what only the full corpus can answer: does experience help, do
cohorts survive, does size help.

Two methodology notes that matter:

  * `closed_pnl` is realized PnL on position close. Open positions are not in
    it, so a wallet still holding is undercounted in both directions.
  * `closed_pnl` does NOT net out Hyperliquid's own trading fees -- only FOMO's
    builder markup is in `builder_fee`, and HL's fee is the larger of the two.
    So "net of builder fee" is a CEILING on true user outcomes. The modelled
    all-in figure below is the more honest one.

Usage:
    python3 fomo_outcomes.py [--db fomo_fomo.db]
"""

import argparse
import sqlite3
from collections import defaultdict

# Hyperliquid base fees, bps. HIP-3 (xyz:) markets bill at double core rates;
# the deployer keeps half. These are pre-discount, so high-volume wallets pay
# less -- treat the modelled figure as an upper bound on venue cost.
HL_FEES = {(1, 1): 9.0, (1, 0): 4.5, (0, 1): 3.0, (0, 0): 1.5}   # (taker, hip3)


def rule(t):
    print(f"\n{'=' * 76}\n{t}\n{'=' * 76}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="fomo_fomo.db")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    q = lambda s, *p: con.execute(s, p).fetchall()
    one = lambda s, *p: con.execute(s, p).fetchone()

    n, users, days, d0, d1 = one("""SELECT COUNT(*), COUNT(DISTINCT user),
        COUNT(DISTINCT day), MIN(day), MAX(day) FROM fills""")
    print(f"corpus: {n:,} fills · {users:,} wallets · {days} days · {d0} .. {d1}")

    # ---- per-wallet roll-up, with modelled venue fees -------------------
    w_pnl, w_bfee, w_hlfee, w_not, w_days, w_fills = (defaultdict(float) for _ in range(6))
    w_first, w_last = {}, {}
    for u, day, no, pnl, bf, crossed, coin in q(
            "SELECT user, day, notional, closed_pnl, builder_fee, crossed, coin FROM fills"):
        hip3 = 1 if ":" in coin else 0
        w_pnl[u] += pnl or 0
        w_bfee[u] += bf or 0
        w_hlfee[u] += (no or 0) * HL_FEES[(crossed, hip3)] / 10_000
        w_not[u] += no or 0
        w_fills[u] += 1
        w_first[u] = min(w_first.get(u, day), day)
        w_last[u] = max(w_last.get(u, day), day)
    for u, d in q("SELECT user, COUNT(DISTINCT day) FROM fills GROUP BY user"):
        w_days[u] = d

    traded = [u for u in w_pnl if w_pnl[u] != 0]          # has realized PnL
    net_b = lambda u: w_pnl[u] - w_bfee[u]                 # ceiling
    net_a = lambda u: w_pnl[u] - w_bfee[u] - w_hlfee[u]    # modelled all-in

    # ------------------------------------------------------------------ 1
    rule("1. Headline: how do FOMO perp traders actually do?")
    wb = sum(1 for u in traded if net_b(u) > 0)
    wa = sum(1 for u in traded if net_a(u) > 0)
    print(f"   wallets with realized PnL          {len(traded):>9,}")
    print(f"   net-positive after builder fee     {wb:>9,}  ({wb/len(traded)*100:.1f}%)   <- ceiling")
    print(f"   net-positive after ALL fees        {wa:>9,}  ({wa/len(traded)*100:.1f}%)   <- modelled")
    tot_pnl = sum(w_pnl.values())
    tot_bf = sum(w_bfee.values())
    tot_hl = sum(w_hlfee.values())
    print(f"\n   aggregate realized PnL         ${tot_pnl:>14,.0f}")
    print(f"   builder fees paid to FOMO      ${tot_bf:>14,.0f}")
    print(f"   modelled Hyperliquid fees      ${tot_hl:>14,.0f}")
    print(f"   {'-'*46}")
    print(f"   net to the cohort              ${tot_pnl - tot_bf - tot_hl:>14,.0f}   over {days} days")
    fees = tot_bf + tot_hl
    if tot_pnl < 0:
        print(f"\n   Of the total drain, fees are {fees/abs(tot_pnl-fees)*100:.0f}%"
              f" -- the rest is market losses.")
    else:
        print(f"\n   Traders made ${tot_pnl:,.0f} TRADING and paid ${fees:,.0f} in fees."
              f"\n   Fees are {fees/tot_pnl:.1f}x their trading profit. They are not losing"
              f"\n   to the market -- they are losing to the toll.")

    # ------------------------------------------------------------------ 2
    rule("2. Does EXPERIENCE help?  (the question the 10-day window can't ask)")
    print(f"   {'active days':<14} {'wallets':>8} {'win rate':>9} {'median net':>12} {'ret/notional':>13}")
    for lo, hi, lbl in [(1,1,"1"),(2,3,"2-3"),(4,7,"4-7"),(8,15,"8-15"),
                        (16,30,"16-30"),(31,999,"31+")]:
        c = [u for u in traded if lo <= w_days[u] <= hi]
        if not c:
            continue
        wins = sum(1 for u in c if net_a(u) > 0)
        nets = sorted(net_a(u) for u in c)
        agg_no = sum(w_not[u] for u in c)
        agg_net = sum(net_a(u) for u in c)
        print(f"   {lbl:<14} {len(c):>8,} {wins/len(c)*100:>8.1f}% "
              f"${nets[len(nets)//2]:>+11,.0f} {agg_net/agg_no*100:>+12.3f}%")

    # ------------------------------------------------------------------ 3
    rule("3. Do cohorts SURVIVE?")
    all_days = sorted({d for (d,) in q("SELECT DISTINCT day FROM fills")})
    cutoff = all_days[-14]                    # start of final 14 days
    eligible = [u for u in w_first if w_first[u] < cutoff]   # had a chance to churn
    alive = sum(1 for u in eligible if w_last[u] >= cutoff)
    print(f"   Retention is only meaningful for wallets that had time to leave, so")
    print(f"   wallets first seen inside the final 14 days are excluded.\n")
    print(f"   eligible wallets (first seen < {cutoff})   {len(eligible):>7,}")
    print(f"   still trading in the final 14 days         {alive:>7,}")
    print(f"   retained                                   {alive/len(eligible)*100:>6.1f}%")
    print(f"\n   {'first seen':<12} {'wallets':>8} {'still active':>13} {'retained':>9} {'median days':>12}")
    for mo in sorted({d[:6] for d in w_first.values()}):
        c = [u for u in eligible if w_first[u][:6] == mo]
        if not c:
            continue
        av = sum(1 for u in c if w_last[u] >= cutoff)
        md = sorted(w_days[u] for u in c)[len(c)//2]
        print(f"   {mo:<12} {len(c):>8,} {av:>13,} {av/len(c)*100:>8.1f}% {md:>12}")
    one_day = sum(1 for u in w_days if w_days[u] == 1)
    print(f"\n   {one_day:,} of {users:,} wallets ({one_day/users*100:.1f}%) traded on exactly one day, ever.")

    # ------------------------------------------------------------------ 4
    rule("4. Does SIZE help?")
    print(f"   {'notional':<16} {'wallets':>8} {'win rate':>9} {'ret/notional':>13}")
    for lo, hi, lbl in [(0,1e3,"< $1K"),(1e3,1e4,"$1K-10K"),(1e4,1e5,"$10K-100K"),
                        (1e5,1e6,"$100K-1M"),(1e6,1e12,"> $1M")]:
        c = [u for u in traded if lo <= w_not[u] < hi]
        if not c:
            continue
        wins = sum(1 for u in c if net_a(u) > 0)
        agg_no = sum(w_not[u] for u in c)
        print(f"   {lbl:<16} {len(c):>8,} {wins/len(c)*100:>8.1f}% "
              f"{sum(net_a(u) for u in c)/agg_no*100:>+12.3f}%")

    # ------------------------------------------------------------------ 5
    rule("5. Execution and positioning, full corpus")
    tk, tot_f = one("SELECT SUM(crossed), COUNT(*) FROM fills")
    print(f"   taker fills                  {tk/tot_f*100:>6.1f}%  ({tk:,} of {tot_f:,})")
    for tif, c in q("""SELECT tif, COUNT(*) FROM fills GROUP BY tif
                       ORDER BY 2 DESC LIMIT 4"""):
        print(f"   tif {str(tif or '(empty)'):<24} {c/tot_f*100:>5.1f}%")
    hip3_no, all_no = one("""SELECT SUM(CASE WHEN coin LIKE '%:%' THEN notional END),
                                    SUM(notional) FROM fills""")
    print(f"\n   HIP-3 / RWA share of notional {hip3_no/all_no*100:>5.1f}%")
    print("\n   Net side skew, by month -- a tradeable imbalance has to PERSIST.")
    months = sorted({d[:6] for (d,) in q("SELECT DISTINCT day FROM fills")})
    print(f"   {'market':<14}" + "".join(f"{m:>9}" for m in months) + f"{'full':>10}")
    flips = tested = 0
    for (c,) in q("""SELECT coin FROM fills GROUP BY coin
                     HAVING SUM(notional) > 5e6 ORDER BY SUM(notional) DESC LIMIT 12"""):
        cells, signs = [], []
        for m in months:
            r = one("""SELECT SUM(notional),
                       SUM(CASE WHEN side='Bid' THEN notional ELSE -notional END)
                       FROM fills WHERE coin=? AND day LIKE ?||'%'""", c, m)
            if r[0]:
                sk = r[1]/r[0]*100
                cells.append(f"{sk:>+8.1f}%")
                signs.append(sk > 0)
            else:
                cells.append(f"{'-':>9}")
        f = one("""SELECT SUM(notional),
                   SUM(CASE WHEN side='Bid' THEN notional ELSE -notional END)
                   FROM fills WHERE coin=?""", c)
        print(f"   {c:<14}" + "".join(cells) + f"{f[1]/f[0]*100:>+9.1f}%")
        if len(signs) > 1:
            tested += 1
            flips += len(set(signs)) > 1
    print(f"\n   {flips} of {tested} liquid markets FLIP the sign of their skew month to month.")
    print("   >> " + ("RULED OUT  a persistent one-sided imbalance to fade"
                      if flips > tested/2 else
                      "SUPPORTED  skew persists and is tradeable"))

    # ------------------------------------------------------------------
    rule("CONTENT-READY LINES")
    print(f"""
   Across {n:,} perpetual fills by {users:,} wallets over {days} days
   ({d0[:4]}-{d0[4:6]}-{d0[6:]} to {d1[:4]}-{d1[4:6]}-{d1[6:]}), on public Hyperliquid data:

   · {wa/len(traded)*100:.1f}% of FOMO perp traders are net-positive after fees.
   · They made ${tot_pnl:,.0f} from trading and paid ${tot_bf+tot_hl:,.0f} in fees,
     so the cohort is {"down" if tot_pnl-tot_bf-tot_hl < 0 else "up"} ${abs(tot_pnl-tot_bf-tot_hl):,.0f} over {days} days.
     Fees are {(tot_bf+tot_hl)/abs(tot_pnl):.1f}x their trading {"profit" if tot_pnl>0 else "loss"}.
   · {tk/tot_f*100:.1f}% of fills cross the spread. They almost never post a limit order.
   · {one_day/users*100:.1f}% of wallets traded on exactly one day and never returned;
     {alive/len(eligible)*100:.1f}% of wallets old enough to churn were still trading at the end.
   · Win rate climbs from {sum(1 for u in traded if w_days[u]==1 and net_a(u)>0)/max(1,sum(1 for u in traded if w_days[u]==1))*100:.0f}% (1 day active) to
     {sum(1 for u in traded if 16<=w_days[u]<=30 and net_a(u)>0)/max(1,sum(1 for u in traded if 16<=w_days[u]<=30))*100:.0f}% (16-30 days). Return on notional is negative in
     {sum(1 for lo,hi in [(1,1),(2,3),(4,7),(8,15),(16,30),(31,999)] if (lambda c: c and sum(net_a(u) for u in c)/sum(w_not[u] for u in c) < 0)([u for u in traded if lo<=w_days[u]<=hi]))} of 6 experience buckets.
   · FOMO collected ${tot_bf:,.0f} in builder fees on ${all_no:,.0f} of notional.
""")
    con.close()


if __name__ == "__main__":
    main()
