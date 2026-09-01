# The Fantrax Tribune

A weekly fantasy-football broadsheet for a Fantrax public league ("Ballers", league ID `agjsij71msd7ufmw`).
Renders a vintage-newspaper Power Ranking and publishes it to GitHub Pages.

**Live site:** `https://jjfro.github.io/fantrax-tribune/`

## How it works

```
Fantrax public API  ──►  fetch_fantrax.py  ──►  power_rankings.py  ──►  llm_commentary.py  ──►  render.py  ──►  index.html  ──►  GitHub Pages
                                                                       (MiniMax)
```

1. **fetch_fantrax.py** — pulls league info, rosters, matchups, standings, and the NFL player index from the public Fantrax API (`fantrax.com/fxea/general`). **No auth required** because the league is set to public.
2. **power_rankings.py** — computes a Composite Power Score per team (35% win % + 20% PF/game + 20% SoS + 15% all-play, **multiplied by a TE-premium factor** to reflect the league's 1.5/rec TE bonus). Picks the Matchup of the Week from the current week's matchup list.
3. **llm_commentary.py** — sends a JSON bundle of the rankings to MiniMax (OpenAI-compatible endpoint) with a strict system prompt. Outputs a JSON object with lede, matchup blurb, rankings blurb, by-the-numbers cards, and a closing line. **Hard rule: no real first names** — owners are referenced only by Fantrax team name or username.
4. **render.py** — feeds everything into a Jinja2 template (`templates/tribune.html.j2`) styled by `static/style.css`. Writes `index.html` (latest) and `editions/week-NN.html` (archive copy).

The whole thing is wired together by `.github/workflows/weekly.yml`, which runs every Tuesday at 9 AM ET and on manual trigger.

## League-specific quirks (worked into the ranking formula)

- **First downs are worth points**: 0.25/pass, 0.5/rush, 0.5/rec
- **Long-pass bonus**: +1 for 50+ yd completions, +3 stacked on 50+ yd passing TDs
- **TE premium**: tight ends get 1.5 per reception (vs 1.0 PPR for everyone else)

The `power_rankings.py` formula multiplies the raw composite by a TE-premium factor so teams stacked at TE get a small but real bump.

## Local development

```bash
python -m pip install -r requirements.txt

# Pull live data and compute rankings
python scripts/fetch_fantrax.py
python scripts/power_rankings.py

# Render with stub commentary (no API key required)
python scripts/llm_commentary.py --dry-run
python scripts/render.py

# Render with real MiniMax commentary
export MINIMAX_API_KEY=sk-...
python scripts/llm_commentary.py
python scripts/render.py
```

Open `index.html` in a browser. The page uses Google Fonts — first load may take a second.

## Configuration

- `config.json` — league ID, site title, palette, ranking weights, LLM endpoint.
- `league_context.json` — **commissioner-only** notes the LLM uses for relationship color and warm-trash-talk: family dynamics, scoring quirks, rivalries, voice directives. Edit this file to teach the Tribune about your league without rewriting prompts.

## Deployment setup

Two things you do *once*:

1. **GitHub Pages.** Repo Settings → Pages → Source: "Deploy from a branch" → Branch: `main` → `/ (root)`. The site goes live at `https://jjfro.github.io/fantrax-tribune/` after the first push.
2. **MiniMax API key.** Repo Settings → Secrets and variables → Actions → New repository secret. Name: `MINIMAX_API_KEY`. Value: your MiniMax API key.

That's it. The scheduled run on the next Tuesday will publish the first edition.

## Manual run

Actions tab → "Weekly Tribune" → "Run workflow". Optional checkbox to dry-run with stub commentary (no API cost).

## Costs

- GitHub Pages: $0 (free for public repos)
- GitHub Actions: $0 (well under 2,000 min/month free tier — this workflow runs ~30 seconds)
- MiniMax API: ~$0.001–0.01 per weekly run depending on commentary length

Total monthly cost at one run per week: **pennies to a dime**.

## Edge cases handled

- **Pre-season / opening week:** zeroed rankings table, "preview" tone, schedule preview as Matchup of the Week.
- **Fantrax API errors:** the fetcher detects `{"error": {...}}` envelopes and retries with exponential backoff.
- **DST/team-offense IDs:** Fantrax keys DSTs as `<id>#1090`; the fetcher resolves these automatically.
- **Missing API key:** the workflow uses stub commentary and still publishes a valid page.

## License

MIT.
