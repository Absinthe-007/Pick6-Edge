# Pick6 Edge

Live board for **DraftKings Pick6** player props — NFL, College Football and MLB.

For every Pick6 line it shows:

- **P(MORE) / P(LESS)** – model probability the player goes over / under the Pick6 line
- **Value** – edge vs. the Pick6 break-even per pick (≈55% for a 3-pick 6x / 8-pick entry, 57.7% for 2-pick 3x), multiplier-aware
- **Where to play** – Pick6 vs. each sportsbook (BetRivers, Bovada, + DraftKings/FanDuel/BetMGM/Caesars etc. if you add an Odds API key), ranked by expected profit per $1
- **Matchup stats** – last 5 games vs. the line, season average, opponent defense rank, game moneyline/spread/total, latest news
- **Alt ladder** – Pick6's alternate lines/multipliers with their hit probability
- **Slip builder** – tap `+` on picks to see the chance every leg hits and the slip's EV using Pick6's real payout table

Site: `https://absinthe-007.github.io/Pick6-Edge/`

## How it updates
`.github/workflows/update.yml` runs `scraper.py` every 2 hours (and on every push / the **Run workflow** button), then publishes `index.html` + `data/props.json` to GitHub Pages. If a source is blocked, the others still run and the last good data is kept.

## Model
1. **Market**: de-vig each sportsbook's over/under, then move it to the Pick6 line (Normal model for yards/pitches, Poisson for counts like receptions, TDs, hits, strikeouts). One-winner markets (first TD scorer) are de-vigged across the whole field.
2. **Stats**: 55% season average + 45% last-5 average from DraftKings' own player card, with variance from the game log.
3. **Blend**: 75% market / 25% stats (90/10 for TD longshots). Stats-only props are shrunk harder toward 50%.

## Optional: more sportsbooks
Get a free key at the-odds-api.com, then in the repo: **Settings → Secrets and variables → Actions → New repository secret** → name `ODDS_API_KEY`. (Player props cost credits; `ODDS_API_MAX_EVENTS` limits usage.)

## Run locally
```
pip install -r requirements.txt
python scraper.py
python -m http.server   # open http://localhost:8000
```

Estimates only — not betting advice. 21+. Problem gambling help: 1-800-GAMBLER.
