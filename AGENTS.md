# @profphet / fohomo — operating rules

On the kontrol contract (`/Users/cynful/kontrol/CONTRACT.md`): shapes `feed` and `social`. Manifest `kontrol.yaml`, adapter `kontrol_adapter.py`, records under `kontrol_data/`. The plan is `PLAN.md` (2026-09-07); the findings are `README.md`. Voice: `VOICE.md`. Decisions since onboarding: `DECISIONS.md`. No state here: `kontrol status profphet`.

## Rules

- Everything here reads public sources only: Hyperliquid's stats bucket and Solana RPC. Nothing touches fomo.family's servers.
- No piece ships without its chart regenerated from the current database. Numbers moved 60% in nine days once.
- Never post anything that reads as a solicitation to trade perps. The digest reports; it does not recommend.
- Every post carries the referral disclosure and link once the programme is verified.
- Secrets in `.env` only (`TELEGRAM_BOT_TOKEN`, `REFERRAL_LINK`); names in `.env.example`.
- The analysis scripts are dependency-free Python. Keep them that way.

## Commands

```bash
python3 fomo_ingest.py          # refresh the perps fills database
python3 fomo_spot.py            # spot trade parser
python3 fomo_outcomes.py        # what happens after the crowd buys
python3 builders_compare.py     # every Hyperliquid front-end's markup
python3 make_charts.py          # regenerate content/ charts
```
