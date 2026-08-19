#!/usr/bin/env python3
"""
Render the three @profphet charts as PNGs, light and dark.

Colors come from the validated data-viz palette; every set was run through
scripts/validate_palette.js before use:
  categorical slots 1-3  (chart 1)  -- all-pairs PASS both modes
  diverging blue<->red   (chart 2)  -- all-pairs PASS both modes
Light-mode aqua sits under 3:1 on the light surface, so the relief rule applies:
chart 1 ships visible direct labels on every segment.

Usage:  python3 make_charts.py   ->  content/*.png
"""

import os
import sqlite3
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

DB = "fomo_fomo.db"
DB_SPOT = "fomo_spot.db"
OUT = "content"
HL_FEES = {(1, 1): 9.0, (1, 0): 4.5, (0, 1): 3.0, (0, 0): 1.5}   # (taker, hip3) bps

THEME = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", ink3="#8a8a85",
                  grid="#e7e6e2", zero="#b4b3ae",
                  cat=["#2a78d6", "#eb6834", "#1baf7a"],
                  pos="#2a78d6", neg="#e34948"),
    "dark":  dict(surface="#1a1a19", ink="#ffffff", ink2="#c3c2b7", ink3="#8a8a80",
                  grid="#2e2e2c", zero="#4a4a46",
                  cat=["#3987e5", "#d95926", "#199e70"],
                  pos="#3987e5", neg="#e66767"),
}


def base(fig, ax, t, title, subtitle):
    fig.patch.set_facecolor(t["surface"])
    for a in (ax if isinstance(ax, (list, tuple)) else [ax]):
        a.set_facecolor(t["surface"])
        for s in a.spines.values():
            s.set_visible(False)
        a.tick_params(colors=t["ink2"], labelsize=11, length=0)
    fig.text(0.055, 0.955, title, color=t["ink"], fontsize=19, fontweight="600", va="top")
    fig.text(0.055, 0.876, subtitle, color=t["ink2"], fontsize=12.5, va="top")


def footer(fig, t, note):
    fig.text(0.055, 0.035, note, color=t["ink3"], fontsize=9.5, va="bottom")
    fig.text(0.945, 0.035, "@profphet", color=t["ink3"], fontsize=9.5,
             va="bottom", ha="right")


def rounded(ax, x, y, w, h, color, r=0.018):
    """Bar with softly rounded ends; r in axes-x units."""
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
        linewidth=0, facecolor=color, mutation_aspect=h / max(r, 1e-9) / 40))


# --------------------------------------------------------------- chart 1
def chart_loss(con, mode):
    t = THEME[mode]
    rows = con.execute("""SELECT closed_pnl, builder_fee, notional, crossed, coin
                          FROM fills""").fetchall()
    pnl = sum(r[0] or 0 for r in rows)
    bf = sum(r[1] or 0 for r in rows)
    hl = sum((r[2] or 0) * HL_FEES[(r[3], 1 if ":" in r[4] else 0)] / 1e4 for r in rows)
    n, users, days, d0, d1 = con.execute(
        """SELECT COUNT(*), COUNT(DISTINCT user), COUNT(DISTINCT day),
                  MIN(day), MAX(day) FROM fills""").fetchone()
    fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
    segs = [("Market losses", -pnl, t["cat"][0]),
            ("FOMO's fee", bf, t["cat"][1]),
            ("Hyperliquid's fee", hl, t["cat"][2])]
    total = sum(s[1] for s in segs)

    fig, ax = plt.subplots(figsize=(11, 4.9), dpi=190)
    fig.subplots_adjust(left=.055, right=.945, top=.70, bottom=.17)
    base(fig, ax, t,
         f"FOMO perp traders lost ${total/1e6:.2f}M in {days} days",
         f"Only {(-pnl)/total*100:.0f}% of it was the market. The rest was fees.")

    gap = total * 0.004          # 2px surface gap between fills
    left = 0.0
    for name, val, c in segs:
        rounded(ax, left, 0.34, val - gap, 0.40, c)
        ax.text(left + (val - gap) / 2, 0.54, f"${val:,.0f}", color="#ffffff",
                fontsize=13, fontweight="600", ha="center", va="center")
        ax.text(left + (val - gap) / 2, 0.24, name, color=t["ink2"],
                fontsize=11.5, ha="center", va="top")
        ax.text(left + (val - gap) / 2, 0.13, f"{val/total*100:.0f}%", color=t["ink3"],
                fontsize=10.5, ha="center", va="top")
        left += val

    fee_start = segs[0][1]
    ax.plot([fee_start, total], [0.84, 0.84], color=t["ink3"], lw=1.2)
    for x in (fee_start, total):
        ax.plot([x, x], [0.80, 0.84], color=t["ink3"], lw=1.2)
    ax.text((fee_start + total) / 2, 0.90,
            f"{(bf+hl)/total*100:.0f}% of the loss is fees",
            color=t["ink"], fontsize=13, fontweight="600", ha="center")

    ax.set_xlim(0, total); ax.set_ylim(0, 1)
    ax.set_xticks([]); ax.set_yticks([])
    footer(fig, t, f"{n:,} fills · {users:,} wallets · {fmt(d0)} to {fmt(d1)} · "
                   "Hyperliquid builder-fills data. Venue fees modelled at base rates.")
    fig.savefig(f"{OUT}/01-losses-{mode}.png", facecolor=t["surface"])
    plt.close(fig)


# --------------------------------------------------------------- chart 2
def chart_skew(con, mode):
    t = THEME[mode]
    coins = [c for (c,) in con.execute(
        """SELECT coin FROM fills GROUP BY coin
           HAVING SUM(notional) > 5e6 ORDER BY SUM(notional) DESC LIMIT 6""")]
    months = ["202606", "202607", "202608"]
    data = {}
    for c in coins:
        for m in months:
            r = con.execute("""SELECT SUM(notional),
                    SUM(CASE WHEN side='Bid' THEN notional ELSE -notional END)
                    FROM fills WHERE coin=? AND day LIKE ?||'%'""", (c, m)).fetchone()
            data[(c, m)] = (r[1] / r[0] * 100) if r[0] else None

    fig, axes = plt.subplots(2, 3, figsize=(11, 6.4), dpi=190)
    fig.subplots_adjust(left=.055, right=.965, top=.685, bottom=.115, hspace=.52, wspace=.16)
    flat = [a for row in axes for a in row]
    allc = [c for (c,) in con.execute(
        """SELECT coin FROM fills GROUP BY coin HAVING SUM(notional) > 5e6""")]
    nflip = 0
    for c in allc:
        sg = set()
        for m in months:
            r = con.execute("""SELECT SUM(notional),
                    SUM(CASE WHEN side='Bid' THEN notional ELSE -notional END)
                    FROM fills WHERE coin=? AND day LIKE ?||'%'""", (c, m)).fetchone()
            if r[0]:
                sg.add(r[1] > 0)
        nflip += len(sg) > 1
    nfills, = con.execute("SELECT COUNT(*) FROM fills").fetchone()
    base(fig, flat, t, "There is no side to fade",
         f"Net positioning flips sign month to month in {nflip} of {len(allc)} "
         f"liquid markets.")

    vals = [v for v in data.values() if v is not None]
    lim = max(abs(min(vals)), abs(max(vals))) * 1.42
    labels = ["Jun", "Jul", "Aug"]
    for ax, c in zip(flat, coins):
        series = [data[(c, m)] for m in months]
        for j, v in enumerate(series):
            if v is None:
                continue
            ax.bar(j, v, width=0.5, color=t["pos"] if v > 0 else t["neg"], zorder=3)
            off = lim * 0.09
            ax.text(j, v + (off if v > 0 else -off), f"{v:+.1f}",
                    color=t["ink"], fontsize=10.5, fontweight="600", ha="center",
                    va="bottom" if v > 0 else "top")
        flips = len({v > 0 for v in series if v is not None}) > 1
        ax.set_title(c, color=t["ink"], fontsize=13, loc="left", pad=10)
        if flips:
            ax.text(1.0, 1.055, "flips", transform=ax.transAxes, ha="right",
                    va="bottom", fontsize=9.5, color=t["neg"], fontweight="600")
        ax.axhline(0, color=t["zero"], lw=1.4, zorder=2)
        ax.set_ylim(-lim, lim)
        ax.set_xlim(-0.6, 2.6)
        ax.set_xticks(range(3)); ax.set_xticklabels(labels, fontsize=10.5)
        ax.set_yticks([])
    fig.text(0.055, 0.788, "net long above the line", color=t["pos"],
             fontsize=11, fontweight="600")
    fig.text(0.055 + 0.148, 0.788, "·", color=t["ink3"], fontsize=11)
    fig.text(0.055 + 0.163, 0.788, "net short below", color=t["neg"],
             fontsize=11, fontweight="600")
    footer(fig, t, "Net side skew by month, markets over $5M notional · "
                   f"{nfills:,} fills")
    fig.savefig(f"{OUT}/03-skew-{mode}.png", facecolor=t["surface"])
    plt.close(fig)


# --------------------------------------------------------------- chart 3
def chart_experience(con, mode):
    t = THEME[mode]
    pnl, bfee, hlfee, notl, days = (defaultdict(float) for _ in range(5))
    for u, d, no, p, bf, cr, coin in con.execute(
            "SELECT user,day,notional,closed_pnl,builder_fee,crossed,coin FROM fills"):
        pnl[u] += p or 0; bfee[u] += bf or 0; notl[u] += no or 0
        hlfee[u] += (no or 0) * HL_FEES[(cr, 1 if ":" in coin else 0)] / 1e4
    for u, d in con.execute("SELECT user,COUNT(DISTINCT day) FROM fills GROUP BY user"):
        days[u] = d
    traded = [u for u in pnl if pnl[u] != 0]
    net = lambda u: pnl[u] - bfee[u] - hlfee[u]

    buckets = [(1, 1, "1"), (2, 3, "2–3"), (4, 7, "4–7"),
               (8, 15, "8–15"), (16, 30, "16–30")]
    labs, wins, rets = [], [], []
    for lo, hi, lab in buckets:
        c = [u for u in traded if lo <= days[u] <= hi]
        if not c:
            continue
        labs.append(lab)
        wins.append(sum(1 for u in c if net(u) > 0) / len(c) * 100)
        rets.append(sum(net(u) for u in c) / sum(notl[u] for u in c) * 100)

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6.6), dpi=190,
                                 gridspec_kw=dict(height_ratios=[1, 1], hspace=0.40))
    fig.subplots_adjust(left=.085, right=.945, top=.72, bottom=.155)
    base(fig, [a1, a2], t, "Experience doesn't make them profitable",
         "Win rate climbs with time on the app. Returns never leave the red.")

    x = range(len(labs))
    a1.bar(x, wins, width=0.40, color=t["cat"][0], zorder=3)
    for i, v in enumerate(wins):
        a1.text(i, v + 1.4, f"{v:.0f}%", color=t["ink"], fontsize=11,
                fontweight="600", ha="center")
    a1.set_ylim(0, max(wins) * 1.32); a1.set_yticks([])
    a1.set_xticks(list(x)); a1.set_xticklabels([])
    a1.set_title("Share of wallets net-positive after fees", color=t["ink2"],
                 fontsize=12, loc="left", pad=8)

    a2.bar(x, rets, width=0.40, color=t["neg"], zorder=3)
    for i, v in enumerate(rets):
        a2.text(i, v - abs(min(rets)) * 0.13, f"{v:.2f}%", color=t["ink"],
                fontsize=11, fontweight="600", ha="center", va="top")
    a2.axhline(0, color=t["zero"], lw=1.4)
    a2.set_ylim(min(rets) * 1.72, abs(min(rets)) * 0.30); a2.set_yticks([])
    a2.set_xticks(list(x)); a2.set_xticklabels(labs, fontsize=11)
    a2.set_title("Return on notional traded", color=t["ink2"],
                 fontsize=12, loc="left", pad=8)
    fig.text(0.5, 0.093, "days active on FOMO", color=t["ink3"],
             fontsize=11, ha="center")
    footer(fig, t, f"{len(traded):,} wallets with realized PnL · fees include FOMO's "
                   "markup and modelled Hyperliquid venue fees")
    fig.savefig(f"{OUT}/05-experience-{mode}.png", facecolor=t["surface"])
    plt.close(fig)


# --------------------------------------------------------------- chart 4
def chart_feecurve(mode):
    """Spot fee rate by trade size. Log y -- the range spans two orders."""
    import statistics
    t = THEME[mode]
    con = sqlite3.connect(DB_SPOT)
    buckets = [(0, 5, "$0–5"), (5, 10, "$5–10"), (10, 25, "$10–25"),
               (25, 50, "$25–50"), (50, 100, "$50–100"), (100, 250, "$100–250"),
               (250, 1e3, "$250–1K"), (1e3, 1e4, "$1K–10K")]
    labs, meds, ns = [], [], []
    for lo, hi, lab in buckets:
        r = [x[0] for x in con.execute(
            "SELECT rate_pct FROM spot WHERE notional>=? AND notional<?", (lo, hi))]
        if len(r) < 8:
            continue
        labs.append(lab); meds.append(statistics.median(r)); ns.append(len(r))
    total = con.execute("SELECT COUNT(*) FROM spot").fetchone()[0]
    con.close()

    fig, ax = plt.subplots(figsize=(11, 6.2), dpi=190)
    fig.subplots_adjust(left=.075, right=.955, top=.665, bottom=.175)
    base(fig, ax, t, f"FOMO says 0.50%. Small trades pay {max(meds):.0f}%.",
         "Median fee as a share of trade size. The flat minimum fee does this.")

    pen = [m > 0.6 for m in meds]
    for i, (m, p) in enumerate(zip(meds, pen)):
        ax.bar(i, m, width=0.46, color=t["neg"] if p else t["cat"][0], zorder=3)
        ax.text(i, m * 1.16, f"{m:.2f}%" if m < 10 else f"{m:.0f}%",
                color=t["ink"], fontsize=11.5, fontweight="600", ha="center")


    ax.axhline(0.50, color=t["ink3"], lw=1.3, ls=(0, (5, 4)), zorder=2)
    ax.text(-0.44, 0.56, "advertised 0.50%", color=t["ink2"],
            fontsize=10.5, ha="left", va="bottom")
    ax.set_yscale("log")
    ax.set_ylim(0.012, 260)
    ax.set_yticks([0.1, 1, 10, 100])
    ax.set_yticklabels(["0.1%", "1%", "10%", "100%"], fontsize=10.5)
    ax.set_xticks(range(len(labs)))
    ax.set_xticklabels([f"{l}\nn={n:,}" for l, n in zip(labs, ns)], fontsize=11)
    ax.yaxis.grid(True, color=t["grid"], lw=1, zorder=0); ax.set_axisbelow(True)
    ax.set_xlabel("trade size", color=t["ink3"], fontsize=11, labelpad=12)

    for x, c, lab in ((0.055, t["neg"], "pays more than advertised"),
                      (0.335, t["cat"][0], "pays 0.50% or less")):
        fig.patches.append(plt.Rectangle((x, 0.766), 0.016, 0.019, color=c,
                                         transform=fig.transFigure, zorder=5))
        fig.text(x + 0.024, 0.7655, lab, color=t["ink2"], fontsize=11)

    footer(fig, t, f"{total:,} FOMO spot trades parsed from Solana · "
                   "median rate per bucket · log scale")
    fig.savefig(f"{OUT}/08-feecurve-{mode}.png", facecolor=t["surface"])
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(DB)
    for mode in ("light", "dark"):
        chart_loss(con, mode)
        chart_skew(con, mode)
        chart_experience(con, mode)
        chart_feecurve(mode)
        print(f"  {mode}: 3 charts")
    con.close()
    print(f"\nwrote {len(os.listdir(OUT))} files to {OUT}/")
