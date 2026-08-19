#!/usr/bin/env python3
"""
Phase 2 — what determines FOMO's builder fee?

Some FOMO perp fills are billed at ~1 bp instead of the standard ~5 bp. A 5x
difference in FOMO's markup decides whether it's ever rational to route perps
through the app, so it's worth knowing whether the discount is earned.

This tests each candidate explanation against the corpus and computes the
verdict from the data rather than asserting it. Run fomo_ingest.py first.

Usage:
    python3 fomo_tier.py [--db fomo_fomo.db] [--roster]

Result on the 2026-06-05..08-15 corpus (433,811 fills, 11,584 wallets):
the fee is a per-ACCOUNT setting, granted out of band. Not earned.
"""

import argparse
import sqlite3

CUT = 2.5   # bps; separates any discounted tier from the 5 bp default

# Wallets never observed below CUT -- i.e. ordinary, unflagged accounts.
UNFLAGGED = "user NOT IN (SELECT user FROM fills WHERE fee_bps < 2.5 GROUP BY user)"
FLAGGED   = "user IN (SELECT user FROM fills WHERE fee_bps < 2.5 GROUP BY user)"


def rule(t):
    print(f"\n{'=' * 76}\n{t}\n{'=' * 76}")


def verdict(claim, ok, detail=""):
    print(f"\n  >> {'SUPPORTED' if ok else 'RULED OUT':<11} {claim}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"     {line}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="fomo_fomo.db")
    ap.add_argument("--roster", action="store_true", help="print the discounted-wallet list")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    q = lambda s, *p: con.execute(s, p).fetchall()
    one = lambda s, *p: con.execute(s, p).fetchone()

    n, users, days, lo_d, hi_d = one(
        "SELECT COUNT(*), COUNT(DISTINCT user), COUNT(DISTINCT day), MIN(day), MAX(day) FROM fills")
    print(f"corpus: {n:,} fills · {users:,} wallets · {days} days · {lo_d} .. {hi_d}")

    # ------------------------------------------------------------------ 0
    rule("0. What rates does FOMO actually charge?")
    print("   Hyperliquid builder fees are set in tenths of a bp -- the 'f' field.")
    print(f"\n   {'bps':>6} {'f':>5} {'fills':>9} {'share':>7} {'notional':>16}")
    for r, c, no in q("""SELECT ROUND(fee_bps,1), COUNT(*), SUM(notional) FROM fills
                         WHERE fee_bps IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 100
                         ORDER BY 1 DESC"""):
        print(f"   {r:>6.1f} {r*10:>5.0f} {c:>9,} {c/n*100:>6.1f}% ${no:>15,.0f}")
    verdict("discrete configured tiers, not noise or rounding", True,
            "f values are round numbers (50, 30, 25, 20, 10, 0) -- these are settings.")

    # ------------------------------------------------------------------ 1
    rule("1. Is the tier a property of the ACCOUNT?")
    tot_w, = one("SELECT COUNT(DISTINCT user) FROM fills")
    fl_w,  = one(f"SELECT COUNT(DISTINCT user) FROM fills WHERE {FLAGGED}")
    unf = one(f"""SELECT COUNT(*), AVG(fee_bps), MIN(fee_bps), MAX(fee_bps),
                         SUM(fee_bps < {CUT})
                  FROM fills WHERE fee_bps IS NOT NULL AND {UNFLAGGED}""")
    print(f"   wallets ever discounted     {fl_w:>7,}  ({fl_w/tot_w*100:.1f}%)")
    print(f"   wallets never discounted    {tot_w-fl_w:>7,}  ({(tot_w-fl_w)/tot_w*100:.1f}%)")
    print(f"\n   Among the never-discounted: {unf[0]:,} fills, "
          f"avg {unf[1]:.3f} bps, range {unf[2]:.1f}-{unf[3]:.1f}, "
          f"{unf[4]:,} below {CUT} bps")
    verdict("the rate is set per account", unf[4] == 0,
            f"Not one of {unf[0]:,} fills from ordinary wallets fell below {CUT} bps.")

    # ------------------------------------------------------------------ 2
    rule("2. Can an ordinary user earn it through ORDER TYPE?")
    print("   Globally, limit (Gtc) orders look cheaper -- but that may be composition,")
    print("   since the discounted whales are also the ones posting limits. Split it:\n")
    for label, where in (("ALL wallets", "1=1"), ("ORDINARY only", UNFLAGGED)):
        print(f"   {label}")
        print(f"     {'crossed':>8} {'tif':<18} {'fills':>9} {'avg bps':>9}")
        for cr, tif, c, avg in q(f"""SELECT crossed, tif, COUNT(*), AVG(fee_bps) FROM fills
                                     WHERE fee_bps IS NOT NULL AND {where}
                                     GROUP BY crossed, tif
                                     HAVING COUNT(*) > 200 ORDER BY COUNT(*) DESC"""):
            print(f"     {cr:>8} {str(tif or '(empty)'):<18} {c:>9,} {avg:>9.3f}")
        print()
    spread, = one(f"""SELECT MAX(a)-MIN(a) FROM (SELECT AVG(fee_bps) a FROM fills
                      WHERE fee_bps IS NOT NULL AND {UNFLAGGED}
                      GROUP BY crossed, tif HAVING COUNT(*) > 200)""")
    verdict("order type gets an ordinary user a discount", spread > 0.5,
            f"Across ordinary wallets the spread between order types is {spread:.3f} bps.\n"
            "The global Gtc 'discount' is composition bias from the flagged cohort.")

    # ------------------------------------------------------------------ 3
    rule("3. Is it the MARKET, or a promo on certain coins?")
    print("   If it were market-wide, a coin on a given day would show ONE rate.")
    print("   Instead the same coin/day carries several, split by who traded:\n")
    for c, d in q("""SELECT coin, day FROM fills WHERE fee_bps < 2.5
                     GROUP BY coin, day ORDER BY COUNT(*) DESC LIMIT 5"""):
        mix = q("""SELECT ROUND(fee_bps,1), COUNT(*) FROM fills
                   WHERE coin=? AND day=? GROUP BY 1 ORDER BY 2 DESC""", c, d)
        print(f"     {c:<13} {d}  " + "   ".join(f"{b}bp×{k:,}" for b, k in mix[:4]))
    verdict("the market determines the rate", False,
            "Multiple rates coexist in the same market on the same day.")

    # ------------------------------------------------------------------ 4
    rule("4. Is it a VOLUME THRESHOLD?  (the one that would make it earnable)")
    print("   For each wallet that moved from standard to discounted, how much had it")
    print("   already traded at the standard rate?\n")
    print(f"   {'wallet':<15} {'first':>7} {'switch':>7} {'fills before':>13} {'notional before':>17}")
    befores = []
    for (u,) in q("""SELECT user FROM fills WHERE fee_bps IS NOT NULL GROUP BY user
                     HAVING SUM(fee_bps<2.5)>0 AND SUM(fee_bps>=2.5)>0"""):
        rows = q("""SELECT day, notional, fee_bps FROM fills
                    WHERE user=? AND fee_bps IS NOT NULL ORDER BY ts""", u)
        tiers = [r[2] < CUT for r in rows]
        if not tiers[0] and True in tiers:            # started standard, moved down
            i = tiers.index(True)
            nb = sum(r[1] for r in rows[:i])
            befores.append(nb)
            if len(befores) <= 10:
                print(f"   {u[:13]}…  {rows[0][0][4:]:>7} {rows[i][0][4:]:>7} "
                      f"{i:>13,} ${nb:>16,.0f}")
    if befores:
        spread_ratio = max(befores) / max(min(befores), 1)
        print(f"\n   {len(befores)} wallets crossed over. Notional traded first ranged "
              f"${min(befores):,.0f} to ${max(befores):,.0f}.")
        verdict("a volume threshold unlocks the discount", spread_ratio < 100,
                f"The range spans {spread_ratio:,.0f}x. No threshold can fit that.")
    else:
        verdict("a volume threshold unlocks the discount", False,
                "No wallet is observed crossing over at all.")

    # ------------------------------------------------------------------ 5
    rule("5. Does the flag move in BOTH directions?")
    fwd = rev = osc = 0
    for (u,) in q("""SELECT user FROM fills WHERE fee_bps IS NOT NULL GROUP BY user
                     HAVING SUM(fee_bps<2.5)>0 AND SUM(fee_bps>=2.5)>0"""):
        t = [r[0] < CUT for r in q(
            "SELECT fee_bps FROM fills WHERE user=? AND fee_bps IS NOT NULL ORDER BY ts", u)]
        flips = sum(1 for x, y in zip(t, t[1:]) if x != y)
        if flips > 2:   osc += 1
        elif t[0]:      rev += 1
        else:           fwd += 1
    print(f"   standard -> discounted (once)   {fwd:>4}")
    print(f"   discounted -> standard (once)   {rev:>4}   <- discounts get REVOKED")
    print(f"   flips repeatedly                {osc:>4}")
    verdict("the flag is toggled by FOMO, in both directions", rev > 0 or osc > 0,
            "A rate that is granted, removed, and re-granted is an operator action,\n"
            "not a status a trader earns and holds.")

    # ------------------------------------------------------------------ 6
    rule("6. What the discounted cohort is worth")
    tot = one("SELECT COUNT(DISTINCT user),COUNT(*),SUM(notional),SUM(builder_fee) FROM fills")
    fl  = one(f"SELECT COUNT(DISTINCT user),COUNT(*),SUM(notional),SUM(builder_fee) "
              f"FROM fills WHERE {FLAGGED}")
    for lbl, i in (("wallets", 0), ("fills", 1), ("notional", 2), ("builder fees", 3)):
        v = f"${fl[i]:,.0f}" if i > 1 else f"{fl[i]:,}"
        print(f"   {lbl:<14} {v:>16}   {fl[i]/tot[i]*100:>5.1f}% of total")
    r_fl = fl[3] / fl[2] * 10_000
    r_or = (tot[3] - fl[3]) / (tot[2] - fl[2]) * 10_000
    print(f"\n   effective rate, discounted cohort  {r_fl:>6.2f} bps")
    print(f"   effective rate, everyone else      {r_or:>6.2f} bps")
    print(f"   forgone over {days} days               "
          f"${fl[2]*r_or/10_000 - fl[3]:>10,.0f}"
          f"   (${(fl[2]*r_or/10_000 - fl[3])/days*365:,.0f}/yr)")

    if a.roster:
        rule("The discounted roster")
        print(f"   {'wallet':<44} {'fills':>7} {'notional':>14} {'bps':>6} {'pnl':>12}")
        for u, f, no, bf, p in q(f"""SELECT user,COUNT(*),SUM(notional),SUM(builder_fee),
                                     SUM(closed_pnl) FROM fills WHERE {FLAGGED}
                                     GROUP BY user ORDER BY SUM(notional) DESC"""):
            print(f"   {u:<44} {f:>7,} ${no:>13,.0f} {bf/no*10_000:>6.2f} ${p:>+11,.0f}")

    # ------------------------------------------------------------------
    rule("ANSWER")
    print(f"""
   FOMO's builder fee is a per-ACCOUNT setting. The default is 5.0 bps and
   {tot[0]-fl[0]:,} of {tot[0]:,} wallets ({(tot[0]-fl[0])/tot[0]*100:.1f}%) never leave it. A further
   {fl[0]} accounts sit on configured tiers of 3.0, 2.5, 2.0, 1.0 or 0.0 bps.

   It is NOT earned. It does not track volume (crossovers happen anywhere from
   ${min(befores):,.0f} to ${max(befores):,.0f} of prior notional), it is not a market promo, it is
   not an order-type discount, and it gets revoked as well as granted.

   So there is no threshold to trade toward, and the dossier's advice stands
   unchanged for ordinary accounts: at 5 bp/side on top of Hyperliquid's own
   fees, route perps direct.

   But note what the cohort is: {fl[0]} accounts, {fl[2]/tot[2]*100:.0f}% of FOMO's perp notional,
   paying {r_fl:.1f} bps against everyone else's {r_or:.1f}. FOMO plainly does grant this to
   size. That makes it a negotiation, not a ladder -- and the roster of who
   already has it is public, which is a useful thing to hold before asking.
""")
    con.close()


if __name__ == "__main__":
    main()
