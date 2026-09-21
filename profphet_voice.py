#!/usr/bin/env python3
"""profphet's tweet generator: facts from SQL, voice from VOICE.md, nothing invented.

Two lanes, and they fail differently on purpose.

A **data post** is built around one number that this module computes from the current
database at generation time. The model never sees the README and never produces a figure:
it is handed a fact block and writes the sentence around it, and `check()` then rejects any
candidate containing a digit that was not in that block. That is what makes VOICE.md's
"never quote a stale number" a property of the code rather than a hope - on 2026-09-20 the
committed README was 24 days and 69% behind the corpus it describes.

A **life post** is shaped from a seed the operator wrote (`#profphet-life ...` from the
phone, stored in kontrol's notebooks). With no seeds this module returns nothing. It does
not invent a life: an invented confession is the one failure the audience cannot forgive,
and it is the exact slop the josbjohnson modelling exists to avoid.

Dependency-free except for the LLM call, like the rest of the repo. Import-safe with no
database, no keys and no network - every entry point degrades to a deterministic template so
the whole flow can be exercised dry.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICE = ROOT / "VOICE.md"
KDIR = ROOT / "kontrol_data"
LLM_COSTS = KDIR / "llm-costs.jsonl"
SEED_TOPIC = "profphet-life"
DEFAULT_MODEL = "openai/gpt-5.6-luna"
MAX_TWEET = 280


# ---- environment ---------------------------------------------------------------------

def dotenv_vals(path: Path | None = None) -> dict:
    """.env as a dict. The operator copies .env onto each machine; it is never in git."""
    out: dict[str, str] = {}
    f = path or (ROOT / ".env")
    try:
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or dotenv_vals().get(name) or default


# ---- the voice spec ------------------------------------------------------------------

SPEC_RE = re.compile(r"^```spec:([a-z]+)\n(.*?)^```", re.M | re.S)


def load_spec() -> dict:
    """The fenced ```spec:name blocks in VOICE.md, verbatim. Editing VOICE.md changes the
    prompt; that is the point of keeping them fenced rather than duplicating them here."""
    try:
        return {m.group(1): m.group(2).strip() for m in SPEC_RE.finditer(VOICE.read_text())}
    except OSError:
        return {}


def system_prompt(lane: str) -> str:
    spec = load_spec()
    parts = ["You write posts for profphet (@profphetic) on X: a trader who checks things, "
             "with receipts from public on-chain data.",
             spec.get("register", ""), spec.get("banned", ""), spec.get(lane, "")]
    return "\n\n".join(p for p in parts if p)


# ---- facts ---------------------------------------------------------------------------

@dataclass
class Fact:
    id: str
    title: str
    headline: str            # the one number, formatted as it should appear
    body: str                # everything the model may draw on
    numbers: list[str]       # every figure the model is allowed to use
    sql: str                 # kept as evidence; a post's number is reproducible
    as_of: date              # newest row the fact depends on
    db: str
    chart: str | None = None
    unavailable: str | None = None   # set when the fact could not be computed, and why

    def stale_by(self, max_age_days: int, today: date | None = None) -> int:
        return ((today or date.today()) - self.as_of).days - max_age_days

    def block(self) -> str:
        return (f"FACT: {self.title}\n"
                f"Computed {self.as_of.isoformat()} from {self.db}.\n\n{self.body}\n\n"
                f"Figures you may use, and no others: {', '.join(self.numbers)}")


def _conn(db: str) -> sqlite3.Connection | None:
    p = ROOT / db
    if not p.is_file():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _day(s) -> date:
    s = str(s)
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _n(x, places=0, unit="") -> str:
    """One formatting path, so the string in `numbers` is byte-identical to the one in `body`."""
    if places == 0:
        return f"{round(x):,}{unit}"
    return f"{x:,.{places}f}{unit}"


# Each builder returns a Fact, or a Fact with `unavailable` set. Never a half-true number.

def fact_corpus(max_age_days: int = 7) -> Fact:
    sql = "select count(*), count(distinct user), count(distinct day), sum(px*sz), max(day) from fills"
    c = _conn("fomo_fomo.db")
    if c is None:
        return Fact("corpus", "Size of the measured corpus", "", "", [], sql, date.today(),
                    "fomo_fomo.db", unavailable="fomo_fomo.db is not on this machine")
    fills, wallets, days, notional, mx = c.execute(sql).fetchone()
    as_of = _day(mx)
    f, w, d = _n(fills), _n(wallets), _n(days)
    b = _n(notional / 1e9, 2)
    return Fact(
        "corpus", "Size of the measured corpus",
        headline=f"{f} fills",
        body=(f"Every perp fill FOMO has routed through Hyperliquid, from public builder CSVs: "
              f"{f} fills by {w} distinct wallets over {d} days, ${b}B of notional. "
              f"Newest day in the corpus is {as_of.isoformat()}."),
        numbers=[f, w, d, f"${b}B"], sql=sql, as_of=as_of, db="fomo_fomo.db")


def fact_taker_share(max_age_days: int = 7) -> Fact:
    sql = ("select sum(case when crossed then 1 else 0 end)*1.0/count(*), count(*), max(day) "
           "from fills where day >= ?")
    c = _conn("fomo_fomo.db")
    if c is None:
        return Fact("taker-share", "How much of the flow pays the taker fee", "", "", [], sql,
                    date.today(), "fomo_fomo.db", unavailable="fomo_fomo.db is not on this machine")
    cutoff = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
    share, n, mx = c.execute(sql, (cutoff,)).fetchone()
    if not n:
        return Fact("taker-share", "How much of the flow pays the taker fee", "", "", [], sql,
                    date.today(), "fomo_fomo.db", unavailable="no fills in the last 30 days")
    pct = _n(share * 100, 1, "%")
    return Fact(
        "taker-share", "How much of the flow pays the taker fee",
        headline=pct,
        body=(f"Of the last {_n(n)} fills FOMO routed, {pct} crossed the spread - they paid the "
              f"taker fee rather than earning the maker rebate. Taker flow is the expensive side "
              f"of every order book, and this crowd is almost entirely on it."),
        numbers=[pct, _n(n)], sql=sql, as_of=_day(mx), db="fomo_fomo.db")


def fact_one_day_wallets(max_age_days: int = 7) -> Fact:
    sql = ("select sum(case when d = 1 then 1 else 0 end)*1.0/count(*), count(*), max(mx) from "
           "(select user, count(distinct day) d, max(day) mx from fills group by user)")
    c = _conn("fomo_fomo.db")
    if c is None:
        return Fact("one-day-wallets", "Wallets that trade once and never come back", "", "", [],
                    sql, date.today(), "fomo_fomo.db", unavailable="fomo_fomo.db is not on this machine")
    share, n, mx = c.execute(sql).fetchone()
    pct = _n(share * 100, 0, "%")
    return Fact(
        "one-day-wallets", "Wallets that trade once and never come back",
        headline=pct,
        body=(f"{pct} of the {_n(n)} wallets that have ever traded through FOMO were active on "
              f"exactly one day. They arrived, traded, and never came back. Retention is the "
              f"number an app never puts in its own dashboard."),
        numbers=[pct, _n(n)], sql=sql, as_of=_day(mx), db="fomo_fomo.db")


def fact_crowded_coin(max_age_days: int = 3) -> Fact:
    sql = ("select coin, count(distinct user) u, sum(px*sz) v, max(day) mx from fills "
           "where day >= ? group by coin order by u desc limit 1")
    c = _conn("fomo_fomo.db")
    if c is None:
        return Fact("crowded-coin", "What the crowd piled into this week", "", "", [], sql,
                    date.today(), "fomo_fomo.db", unavailable="fomo_fomo.db is not on this machine")
    cutoff = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
    row = c.execute(sql, (cutoff,)).fetchone()
    if row is None:
        return Fact("crowded-coin", "What the crowd piled into this week", "", "", [], sql,
                    date.today(), "fomo_fomo.db", unavailable="no fills in the last 7 days")
    coin, users, vol, mx = row["coin"], _n(row["u"]), _n(row["v"] / 1e6, 1), _day(row["mx"])
    return Fact(
        "crowded-coin", "What the crowd piled into this week",
        headline=f"{users} wallets",
        body=(f"In the seven days to {mx.isoformat()}, more FOMO wallets touched {coin} than any "
              f"other market: {users} of them, ${vol}M of notional. This is a crowding "
              f"observation, not a call - say nothing about where the price goes."),
        numbers=[users, f"${vol}M"], sql=sql, as_of=mx, db="fomo_fomo.db")


def _ord(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _app(builder: str) -> str:
    """'fomo-social-trading-perps' -> 'fomo'. The registered name is plumbing; the app is the noun."""
    return re.sub(r"-(?:social-trading-)?(?:perps|app|xyz)$", "", str(builder)).replace("-", " ")


def fact_builder_spread(max_age_days: int = 21) -> Fact:
    """The #1 backlog piece: the same order book at very different prices."""
    # `closed` is wallets with a realised pnl. builders_compare.py's own "profitable" column is
    # `WHERE pnl != 0`, so the denominator here must match it or this fact would publish a number
    # that contradicts the README's table for the same fortnight (34% against its 39%).
    sql = ("select builder, sum(notional) v, count(distinct wallet) w, "
           "sum(case when pnl != 0 then 1 else 0 end) closed, "
           "sum(builder_fee)/sum(notional)*10000 bp, "
           "sum(case when pnl - builder_fee - venue_fee > 0 then 1 else 0 end)*1.0 / "
           "  nullif(sum(case when pnl != 0 then 1 else 0 end), 0) net "
           "from agg group by builder having closed >= 200 and v >= 50e6 order by bp")
    c = _conn("builders.db")
    if c is None:
        return Fact("builder-spread", "The same order book, at very different prices", "", "", [],
                    sql, date.today(), "builders.db",
                    unavailable="builders.db is not on this machine (run builders_compare.py)")
    rows = [dict(r) for r in c.execute(sql)]
    if len(rows) < 3:
        return Fact("builder-spread", "The same order book, at very different prices", "", "", [],
                    sql, date.today(), "builders.db", unavailable="too few builders clear the floor")
    lo, hi = rows[0], rows[-1]
    # Builders register as 'fomo-social-trading-perps', 'metamask-perps', 'based-app'. Matching
    # the bare name exactly found nothing and silently dropped FOMO's own row - the part of this
    # fact worth posting - while still returning a valid-looking fact.
    fomo = next((r for r in rows if r["builder"].lower().startswith("fomo")), None)
    mult = _n(hi["bp"] / lo["bp"], 1) + "x"
    lo_bp, hi_bp = _n(lo["bp"], 2, " bp"), _n(hi["bp"], 2, " bp")
    nums = [mult, lo_bp, hi_bp, _n(len(rows))]
    extra = ""
    if fomo:
        f_bp, f_net = _n(fomo["bp"], 2, " bp"), _n(fomo["net"] * 100, 0, "%")
        rank = sorted(rows, key=lambda r: r["net"]).index(fomo) + 1
        nums += [f_bp, f_net, _n(rank)]
        extra = (f" FOMO charges {f_bp}, mid-table. But only {f_net} of its wallets that closed a "
                 f"position finished ahead of both fees, which is {_ord(rank)} lowest of the "
                 f"{_n(len(rows))}. The outcome column does not follow the price column.")
    mtime = datetime.fromtimestamp((ROOT / "builders.db").stat().st_mtime, UTC).date()
    return Fact(
        "builder-spread", "The same order book, at very different prices",
        headline=mult,
        body=(f"{_n(len(rows))} front-ends route orders into the same Hyperliquid book and charge "
              f"their own markup on top of the identical venue fee: from {lo_bp} ({_app(lo['builder'])}) "
              f"to {hi_bp} ({_app(hi['builder'])}), a spread of {mult}.{extra}"),
        numbers=nums, sql=sql, as_of=mtime, db="builders.db",
        chart=str(ROOT / "content" / "09-builders-light.png"))


FACTS = [fact_builder_spread, fact_corpus, fact_taker_share, fact_one_day_wallets, fact_crowded_coin]
MAX_AGE = {"builder-spread": 21, "corpus": 7, "taker-share": 7, "one-day-wallets": 7, "crowded-coin": 3}


def available_facts(exclude: set | None = None, today: date | None = None) -> tuple[list, list]:
    """(usable, rejected). Rejected carries a reason, so a card can say why a lane is empty
    instead of silently offering nothing."""
    ok, bad = [], []
    for fn in FACTS:
        f = fn()
        if f.unavailable:
            bad.append((f, f.unavailable))
        elif exclude and f.id in exclude:
            bad.append((f, "already posted"))
        elif (over := f.stale_by(MAX_AGE.get(f.id, 7), today)) > 0:
            bad.append((f, f"{over} day(s) staler than this fact is allowed to be "
                           f"(computed {f.as_of.isoformat()})"))
        else:
            ok.append(f)
    return ok, bad


# ---- seeds ---------------------------------------------------------------------------

def _notes_dirs() -> list[Path]:
    for p in (Path.home() / "projects" / "kontrol" / "notes", Path("/Users/cynful/kontrol/notes")):
        if p.is_dir():
            return [p]
    return []


def seeds(used: set | None = None) -> list[dict]:
    """Life-post raw material: what the operator filed with `#profphet-life ...`.

    Read across every host's notebook file, because the operator files these from the phone
    (which lands on the PC) and the generator may run anywhere."""
    used = used or set()
    out, seen = [], set()
    for d in _notes_dirs():
        for f in sorted(d.glob("*.jsonl")):
            try:
                lines = f.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    n = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if n.get("topic") != SEED_TOPIC or n.get("id") in used or n.get("id") in seen:
                    continue
                seen.add(n.get("id"))
                out.append({"id": n.get("id"), "text": (n.get("text") or "").strip(), "at": n.get("at")})
    return [s for s in out if s["text"]]


# Roughly half paragraphs, a third punches, the rest confessions (VOICE.md). Cycling a fixed
# pattern keeps the mix right across a shortlist without a random draw that can clump.
FORMAT_CYCLE = ("paragraph", "punch", "paragraph", "confession", "punch", "paragraph")


def format_for(n: int) -> str:
    return FORMAT_CYCLE[n % len(FORMAT_CYCLE)]


# ---- validation ----------------------------------------------------------------------

URL_RE = re.compile(r"https?://|\bwww\.|\b[a-z0-9-]+\.(com|io|xyz|fi|family|app|co)\b", re.I)
HASHTAG_RE = re.compile(r"(?:^|\s)#\w")
NUM_RE = re.compile(r"\d[\d,]*\.?\d*\s*(?:%|bp|x|b|m|k)?", re.I)
CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")
THREAD_RE = re.compile(r"^\s*1\s*[/.]|\b(?:a thread|thread below|🧵)\b", re.I)
QUESTION_RE = re.compile(r"\b(thoughts|am i wrong|who else|agree|right\?)\s*\?|\?\s*$", re.I)

SLOP = ("the grind", "most people won't", "most people wont", "let that sink in",
        "here's the thing", "heres the thing", "nobody talks about", "unpopular opinion",
        "the hard truth", "stay humble", "wagmi", "we're all gonna make it", "few understand",
        "i used to think", "trust the process", "built different", "rent free", "game changer",
        "at the end of the day", "the reality is", "truth is", "food for thought")

# Deliberately no bare `\d+x` here: "a 5.0x spread" is the headline of the first data post, and
# the number-grounding check below already refuses any figure that is not in the fact block.
RETURNS_RE = re.compile(r"\b(up \d+%|made \$[\d,]+|\d+% (?:gain|return|profit)|"
                        r"(?:turned|flipped) [\d$]|my portfolio)\b", re.I)
ADVICE_RE = re.compile(r"\b(buy|sell|long|short|ape|send it)\s+(?:the\s+)?[A-Z]{2,}\b")

# Words allowed to appear as digits in any post without being in a fact block.
SAFE_NUMS = {"1", "2", "3", "24", "2026"}


def _norm_num(tok: str) -> str:
    t = tok.strip().lower().replace(",", "").replace(" ", "").rstrip(".")
    try:
        v = float(re.sub(r"[^\d.]", "", t) or "0")
    except ValueError:
        return t
    suffix = re.sub(r"[\d.]", "", t)
    return f"{v:g}{suffix}"


def _allowed_nums(fact: Fact | None, seed_text: str | None = None) -> set:
    allowed = {_norm_num(x) for x in SAFE_NUMS}
    for x in (fact.numbers if fact else []):
        allowed.add(_norm_num(x))
        # "$2.78B" must also license the bare "2.78" a sentence may use.
        allowed.add(_norm_num(re.sub(r"[^\d.]", "", x)))
    # A life post's specificity is usually a number the operator wrote - three hours, 4am,
    # the size of the loss. VOICE.md tells the model to keep exactly those, so the seed
    # licenses its own figures and nothing else.
    for m in NUM_RE.finditer(seed_text or ""):
        allowed.add(_norm_num(m.group(0)))
        allowed.add(_norm_num(re.sub(r"[^\d.]", "", m.group(0))))
    return allowed


def check(text: str, lane: str, fact: Fact | None = None, seed_text: str | None = None) -> list[str]:
    """Every reason this candidate must not be offered. Empty list means it may be."""
    bad: list[str] = []
    t = (text or "").strip()
    if not t:
        return ["empty"]
    if len(t) > MAX_TWEET:
        bad.append(f"{len(t)} characters, over the {MAX_TWEET} limit")
    if any(unicodedata.category(ch) == "So" or ord(ch) > 0x2500 for ch in t):
        bad.append("contains an emoji")
    if HASHTAG_RE.search(t):
        bad.append("contains a hashtag")
    if CAPS_RE.search(t):
        bad.append("shouts in capitals")
    if THREAD_RE.search(t):
        bad.append("opens a thread")
    if QUESTION_RE.search(t):
        bad.append("ends on an engagement question")
    if "—" in t or ";" in t:
        bad.append("uses an em dash or semicolon")
    low = t.lower()
    for p in SLOP:
        if p in low:
            bad.append(f"boilerplate phrase: {p!r}")
    if RETURNS_RE.search(t):
        bad.append("claims or implies a return")
    if ADVICE_RE.search(t):
        bad.append("reads as trade advice")
    if lane == "life" and URL_RE.search(t):
        bad.append("a life post carries a link")
    allowed = _allowed_nums(fact, seed_text)
    for m in NUM_RE.finditer(t):
        tok = _norm_num(m.group(0))
        if tok and tok not in allowed and _norm_num(re.sub(r"[^\d.]", "", m.group(0))) not in allowed:
            bad.append(f"figure {m.group(0).strip()!r} is not in the fact block")
    return bad


# ---- generation ----------------------------------------------------------------------

def _record_cost(task: str, model: str, usage, gen_id: str | None = None) -> float:
    cost = float(getattr(usage, "cost", 0) or 0)
    KDIR.mkdir(parents=True, exist_ok=True)
    with LLM_COSTS.open("a") as fh:
        fh.write(json.dumps({
            "at": datetime.now(UTC).isoformat(timespec="seconds"), "task": task, "model": model,
            "tokens_in": getattr(usage, "prompt_tokens", 0), "tokens_out": getattr(usage, "completion_tokens", 0),
            "cost_usd": cost, "id": gen_id}) + "\n")
    return cost


def spent_30d() -> float:
    cut = datetime.now(UTC) - timedelta(days=30)
    total = 0.0
    try:
        for line in LLM_COSTS.read_text().splitlines():
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r["at"]) >= cut:
                    total += float(r.get("cost_usd") or 0)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except OSError:
        pass
    return total


class BudgetReached(RuntimeError):
    pass


def check_budget(add: float = 0.0) -> None:
    cap = env("KONTROL_BUDGET_USD")
    if cap and spent_30d() + add > float(cap):
        raise BudgetReached(f"30-day spend {spent_30d():.2f} would exceed the {cap} budget")


def _template(lane: str, fact: Fact | None, seed: dict | None, fmt: str) -> str:
    """What a dry run offers. Deterministic, honest, and never cached as real copy."""
    if lane == "data" and fact:
        return f"{fact.headline}. {fact.title.lower()}."
    if seed:
        return seed["text"][:MAX_TWEET]
    return ""


def generate(lane: str, n: int, fact: Fact | None = None, seed: dict | None = None,
             fmt: str = "paragraph", live: bool = False, model: str | None = None) -> tuple[list, float]:
    """n candidate posts for one lane. Returns (candidates, cost). Never raises on a model
    failure: a lane that cannot be written falls back to the template, which `check()` then
    judges on the same terms as anything else."""
    model = model or env("PROFPHET_MODEL") or DEFAULT_MODEL
    if not live:
        t = _template(lane, fact, seed, fmt)
        return ([{"text": t, "source": "template (dry run)", "format": fmt}] if t else []), 0.0
    key = env("OPENROUTER_API_KEY")
    if not key:
        t = _template(lane, fact, seed, fmt)
        return ([{"text": t, "source": "template (no OPENROUTER_API_KEY)", "format": fmt}] if t else []), 0.0
    try:
        check_budget(0.01)
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
                        default_headers={"X-Title": "profphet"})
        if lane == "data" and fact:
            user = (f"{fact.block()}\n\nWrite {n} different posts about this fact. Vary the angle, "
                    f"not the number. Return JSON: {{\"posts\": [\"...\", ...]}}")
        else:
            user = (f"SEED (something that actually happened):\n{seed['text']}\n\n"
                    f"Write {n} different posts in the '{fmt}' format from this seed. Keep its "
                    f"specific details. Return JSON: {{\"posts\": [\"...\", ...]}}")
        r = client.chat.completions.create(
            model=model, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system_prompt(lane)}, {"role": "user", "content": user}],
            max_tokens=1200, extra_body={"usage": {"include": True}})
        cost = _record_cost(f"copy-{lane}", model, r.usage, getattr(r, "id", None))
        posts = json.loads(r.choices[0].message.content or "{}").get("posts") or []
        return [{"text": str(p).strip(), "source": f"OpenRouter ({model})", "format": fmt}
                for p in posts[:n] if str(p).strip()], cost
    except BudgetReached:
        raise
    except Exception as e:                                    # noqa: BLE001 - a model failure is not fatal
        t = _template(lane, fact, seed, fmt)
        return ([{"text": t, "source": f"template ({type(e).__name__})", "format": fmt}] if t else []), 0.0
