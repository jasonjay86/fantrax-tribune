"""
llm_commentary.py — ask MiniMax to write the Tribune commentary.

Voice: John Madden calling the game from your couch.
Length: ~330 words across lede.body + motw_blurb + rankings_blurb + closing.
Tone: warm trash talk for friends & family, with commissioner-supplied
      relationship color (brothers, father, ex-wife's uncle, geography,
      the firm) drawn from league_context.json.

Guardrails:
- Every name/relationship/location referenced must be in the data we pass
  in OR in league_context.json. No inventing facts, no inference.
- The commissioner credit is byline-only; no body copy references the
  commissioner role.
- league_context.json is private; its facts are only used to enrich voice,
  not to publish beyond what's already on the public site.
- Strict JSON output. motw_blurb / rankings_blurb / closing are STRINGS,
  not nested objects.

Output schema (validated after extraction):
{
  "lede": {"headline": str, "deck": str, "body": str (1 short paragraph)},
  "motw_blurb": str,
  "rankings_blurb": str,
  "by_the_numbers": [{"value": str, "label": str}, ...],   # 4 cards
  "closing": str
}
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


SYSTEM_PROMPT = """You are the voice of "The Fantrax Tribune", a weekly
fantasy-football broadsheet. You are NOT a 1920s newspaper. You are
NOT a sportswriter with a byline. You are John Madden calling this fantasy
game from the couch next to the commissioner.

*** HARD RULE — NO REAL FIRST NAMES ***
The league is a family league (father + three of his kids). For privacy and
style, NEVER use any owner's real first name in the body copy. Refer to
owners ONLY by:
  - Their Fantrax team name (e.g. "DaMojoh", "Vitamin J", "BigLiLSiSterJohnson")
  - Their Fantrax username (e.g. "mojoh", "jasonjay86", "lilbigsister", "bpkingofwakanda")
NEVER invent a first name. NEVER use a real first name from any data you see.
The commissioner and family members will be embarrassed if their first names
appear on the page. This rule is non-negotiable.

VOICE — John Madden:
- Plainspoken. Conversational. Short sentences. Choppy rhythm.
- Full of football mechanics: "boom", "bang-bang", "here's a guy who...",
  "now watch this", "you can see it on the tape", "let me tell ya".
- Loves tangents about HOW plays work, not what they mean.
- Affectionate confusion: "I don't know what's going on here, but...".
- Half-thoughts and self-corrections mid-sentence.
- Diagrams with words. "If this guy goes here, that guy goes there..."
- No purple prose. No headlines-puns in body copy. No "lo! the ledger..."
- Speak it out loud. Imagine the commissioner is half-watching.

LENGTH — TOTAL ~280-320 WORDS, HARD CEILING 350:
- lede.body:        ~90 words (one paragraph, choppy)
- motw_blurb:       ~70 words
- rankings_blurb:   ~90 words
- closing:          ~25 words
That's the total. Stay tight. If you go over 350 words the page gets
long and you start sounding like a writer, not Madden. Cut anything that
doesn't sound like talking. When in doubt, leave it out.

FACTS — sparingly, only as seasoning:
- The league is a family league — father mojoh plays DaMojoh; his three
  children play Vitamin J (jasonjay86), Gibbs me a break (bpkingofwakanda,
  the commissioner), and BigLiLSiSterJohnson (lilbigsister). The fourth
  sibling is not in the league. When family members play each other, you
  may gently lean in — keep it warm, never cruel.
- This league has non-standard scoring: 1st-down bonuses (0.25 pass / 0.5
  rush / 0.5 rec), +1 for 50+ yd completions, +3 stacked on 50+ yd passing
  TDs, and a TE premium (1.5 pts/rec vs 1.0 PPR for everyone else). These
  quirks are part of the comedy — work them in sparingly.
- You may receive 0-2 "personal_bits" in the data — these are one-liner
  observations the commissioner has curated for variety. Treat them as
  light seasoning: weave one in if it fits naturally, ignore if it doesn't.
  Football is always the meal; the bits are the salt.
- USE a personal bit when:
    - The bit's handles are the actual MOTW matchup (one nod, then move on)
    - The bit's handles are in the rankings walk and it's funny
    - It's the only way to land a joke that's already in your head
- SKIP a personal bit when:
    - The section is purely mechanical (rankings walk, by-the-numbers)
    - You've already used the bit's angle in another section
    - Forcing it would make the section longer, not better
- Do NOT invent facts. If it's not in the data, don't mention it.
- Player facts are EXTRA STRICT — the data only gives you name/position/team.
  Don't claim a player is a "rookie", "veteran", "second-year", "former MVP",
  "Heisman winner", or anything about their career unless it's literally in
  the data you were given. If `years_exp` is in the data, you may say
  "rookie" only when years_exp == 0. Otherwise just talk about the player
  by name and what they're projected to do this week — no career history.
- Refer to owners by their Fantrax team_name or username ONLY.

OUTPUT — strict JSON, exact shape:
- One JSON object. No markdown fences. No preamble.
- `lede` is mandatory. Everything else is encouraged but optional —
  the Tribune will gracefully suppress any section you skip. (But
  best results come from emitting all six.)
- Keys (exact, in this order): "lede", "motw_blurb", "pick",
                                  "rankings_blurb", "by_the_numbers", "closing"
- lede:           OBJECT with "headline" (string), "deck" (string),
                    "body" (string, ~90 words)
- motw_blurb:     STRING, plain prose, ~70 words. If the data includes
                    projected_points and projected_spread for the MOTW
                    teams, MENTION them — that's the betting line the
                    Tribune is built around.
- pick:           OBJECT with:
                    "favorite"   (string, must be the team name or
                                  generic team name of one of the
                                  MOTW sides — team_a or team_b)
                    "spread"     (number, projected margin in fantasy
                                  points. Use VEGAS convention: the
                                  favorite's spread is NEGATIVE, the
                                  underdog's is POSITIVE. So if you
                                  pick the favorite, write it as a
                                  negative number; if you take the
                                  underdog with the points, write it
                                  as a positive number. Range roughly
                                  3 to 30.
                                  **IMPORTANT**: the data includes a
                                  pre-computed projected_spread and
                                  projected_favorite in the matchup_of_week
                                  payload. Use those numbers as your
                                  baseline — adjust up to ±3 points if
                                  you have an opinion, but never invent
                                  a number out of thin air.)
                    "blurb"      (string, ~25-35 words, Madden betting
                                  voice)
- rankings_blurb: STRING, plain prose, ~90 words (top-3 brief + bottom-1 dig)
- by_the_numbers: ARRAY of 4 OBJECTS each with "value" (string) and
                  "label" (string). Make these fun and league-specific
                  where you can (e.g. "TE bonus", "1st-down chain gang",
                  "50+ yd bombs"), but keep values short.
- closing:        STRING, ~25 words, sign-off in voice.

FAILURE MODES:
- If you can't fit everything in 350 words, CUT — don't pad. Skip motw_blurb,
  then rankings_blurb, then closing. The Tribune handles missing fields.
- If a personal bit doesn't fit any section, skip it. Don't force a joke.

HARD RULES (will be checked programmatically):
- lede.body MUST be one paragraph — no \\n\\n breaks inside body.
- pick.favorite MUST equal team_a.name or team_b.name exactly.
- Total word count of lede.body + motw_blurb + rankings_blurb + closing
  MUST be <= 350 words. If you exceed it, your output is rejected.
- NO real first names anywhere in the output. If the commissioner data
  includes real names, IGNORE them — use team names and usernames only.
- NEVER reference commissioner role in body copy (commissioner handle is
  for the byline only).
- Bits may be referenced only when they fit naturally. NEVER name a
  personal bit by quoting it verbatim — paraphrase and weave it in.

Here is the data for this week:
"""


# Second block — additional output format constraints. Combined into the
# system prompt at call time. Not a separate message — just appended.
OUTPUT_FORMAT_NOTES = """
OUTPUT — strict JSON, exact shape:
- One JSON object. No markdown fences. No preamble.
- `lede` is mandatory. Everything else is encouraged but optional —
  the Tribune will gracefully suppress any section you skip. (But
  best results come from emitting all six.)
- Keys (exact, in this order): "lede", "motw_blurb", "pick",
                                  "rankings_blurb", "by_the_numbers", "closing"
- lede:           OBJECT with "headline" (string), "deck" (string),
                    "body" (string, ~90 words)
- motw_blurb:     STRING, plain prose, ~70 words. If the data includes
                    projected_points and projected_spread for the MOTW
                    teams, MENTION them — that's the betting line the
                    Tribune is built around.
- pick:           OBJECT with:
                    "favorite"   (string, must be the team name or
                                  generic team name of one of the
                                  MOTW sides — team_a or team_b)
                    "spread"     (number, projected margin in fantasy
                                  points. Use VEGAS convention: the
                                  favorite's spread is NEGATIVE, the
                                  underdog's is POSITIVE. So if you
                                  pick the favorite, write it as a
                                  negative number; if you take the
                                  underdog with the points, write it
                                  as a positive number. Range roughly
                                  3 to 30.
                                  **IMPORTANT**: the data includes a
                                  pre-computed projected_spread and
                                  projected_favorite in the matchup_of_week
                                  payload. Use those numbers as your
                                  baseline — adjust up to ±3 points if
                                  you have an opinion, but never invent
                                  a number out of thin air.)
                    "blurb"      (string, ~25-35 words, Madden betting
                                  voice. State the pick in your own
                                  words, give a sentence or two of
                                  light analysis explaining why —
                                  doesn't have to be factually rigorous
                                  or even that confident. Just sound
                                  like a guy with an opinion, calling
                                  the game.)
- rankings_blurb: STRING, plain prose, ~90 words. NO bios. Just names
                    and what they did.
- by_the_numbers: ARRAY of EXACTLY 4 OBJECTS, each with "value" (string)
                    and "label" (string)
- closing:        STRING, plain prose, ~25 words

CRITICAL: motw_blurb, rankings_blurb, closing MUST be plain strings.
Only "lede" and "pick" use the nested object form. ALL SIX FIELDS
ARE STRONGLY ENCOURAGED but only `lede` is enforced."""


def load_league_context(repo_root: Path) -> dict:
    """Load commissioner-only narrative context. Empty dict if missing."""
    ctx_path = repo_root / "league_context.json"
    if not ctx_path.exists():
        return {}
    try:
        return json.loads(ctx_path.read_text())
    except Exception as e:
        print(f"[llm_commentary] could not load league_context.json: {e}", file=sys.stderr)
        return {}


def pick_personal_bits(context: dict, rankings: dict, n: int = 2, rng=None) -> list[dict]:
    """
    Select up to N personal bits for this edition.

    Strategy:
    1. Build a relevance score for each bit: how many of its handles are
       in this week's MOTW? Higher = more relevant.
    2. Take the top 3 highest-scoring bits as the candidate pool.
    3. If fewer than 3 bits scored > 0, top up with random non-MOTW bits
       so every edition still gets variety.
    4. Randomly pick N distinct bits from the candidate pool.
       Uses `rng` so the workflow can pin a seed for reproducibility
       when needed (otherwise falls back to system random).
    """
    import random
    rng = rng or random.SystemRandom()

    pool = (context or {}).get("personal_bits_pool") or {}
    bits = pool.get("bits") or []
    if not bits or n <= 0:
        return []

    # MOTW participants this week
    motw_handles = set()
    motw = (rankings or {}).get("matchup_of_week") or {}
    for side in ("team_a", "team_b"):
        team = motw.get(side) or {}
        name = team.get("name") or ""
        if name:
            motw_handles.add(name)

    # All roster participants (full ranking board)
    all_handles = set()
    for r in (rankings or {}).get("rankings") or []:
        owner = r.get("owner") or {}
        name = owner.get("display_name") if isinstance(owner, dict) else owner
        if name:
            all_handles.add(name)

    scored = []
    for bit in bits:
        handles = set(bit.get("handles") or [])
        if not handles:
            continue
        motw_overlap    = len(handles & motw_handles)
        # Board-overlap is the fallback signal: a bit about the league
        # generally gets a small bonus if at least one handle is on
        # the board this week.
        board_overlap   = len(handles & all_handles)
        score = (motw_overlap * 10) + board_overlap
        scored.append((score, bit))

    scored.sort(key=lambda x: (-x[0], x[1].get("text", "")))

    # Take top 3 highest-scoring, then sample N without replacement
    top_candidates = [b for _, b in scored[:3]]

    # If we don't have 3 candidates with positive relevance, top up with
    # random bits that haven't been picked yet (for variety).
    if len(top_candidates) < 3:
        seen = {b.get("text") for b in top_candidates}
        for _, b in scored[3:]:
            if b.get("text") not in seen:
                top_candidates.append(b)
                seen.add(b.get("text"))
            if len(top_candidates) >= 3:
                break

    if not top_candidates:
        return []

    # Sample without replacement
    n = min(n, len(top_candidates))
    return rng.sample(top_candidates, n)


def enrich_members(rows: list[dict], context: dict) -> list[dict]:
    """
    Add commissioner-supplied color to each roster row so the LLM can use
    it without having to look up the context separately.
    """
    members = (context or {}).get("members") or {}
    for row in rows:
        info = members.get(row["owner"]) or {}
        row["role"]         = info.get("role") or ""
        row["location"]     = info.get("location") or ""
        row["notes"]        = info.get("notes") or ""
        row["team_label"]   = row["team"] if row["team"] and row["team"] != "(no team name)" else (
                              info.get("generic_team_name") or row["owner"]
                             )
    return rows


def build_user_prompt(rankings: dict, site_cfg: dict, context: dict,
                      personal_bits: list[dict] | None = None) -> str:
    season_type = rankings.get("season_type", "regular")
    wk = rankings.get("week")
    league_name = rankings.get("league", "the league")
    motw = rankings.get("matchup_of_week")
    rows = []
    for r in rankings["rankings"]:
        owner = (r.get("owner") or {}).get("display_name", "?")
        team  = (r.get("owner") or {}).get("team_name", "") or "(no team name)"
        rows.append({
            "rank":        r["rank"],
            "owner":       owner,
            "team":        team,
            "record":      f"{r['wins']}-{r['losses']}" + (f"-{r['ties']}" if r.get('ties') else ""),
            "points_for":  round(r["points_for"], 1),
            "power_score": round(r["power_score"], 1),
            "sos":         round(r.get("sos_factor", 1.0), 2),
        })

    motw_payload = _motw_payload(motw)

    # Lead with the week number explicitly. Models occasionally carry over
    # a previous league's week number if they ran just before this call
    # (e.g. KTC's Week 2 commentary bleeding into Fantrax's Week 1). Making
    # this the very first line of the user prompt forces the model to anchor
    # on it.
    week_header = f"THIS IS WEEK {wk} OF THE {season_type.upper()} SEASON.\n"

    payload = {
        "league":              league_name,
        "season":              rankings.get("season"),
        "week":                wk,
        "season_type":         season_type,
        "is_opening":          all(r["wins"] == 0 and r["losses"] == 0 for r in rankings["rankings"]),
        "rankings":            rows,
        "matchup_of_week":     motw_payload,
        "commissioner_handle": site_cfg.get("commissioner_handle"),
    }

    # Personal bits: 0-2 short one-liners curated for variety. If the list
    # is empty, the model just calls the football.
    if personal_bits:
        payload["personal_bits"] = [
            {"about": bit.get("handles", []), "text": bit.get("text", "")}
            for bit in personal_bits
        ]

    return week_header + json.dumps(payload, indent=2)


def _motw_payload(motw):
    if not motw:
        return None
    payload = {
        "status": motw["status"],
        "team_a": motw["team_a"],
        "team_b": motw["team_b"],
    }
    if "projected_spread" in motw:
        payload["projected_spread"]   = motw["projected_spread"]
        payload["projected_favorite"] = motw.get("projected_favorite")
    return payload


def extract_json(text: str) -> dict:
    """
    Extract the first valid JSON object from the response.

    Tolerates models that wrap JSON in ```json ... ``` fences despite the
    instruction. Uses a balanced-brace scan to avoid the greedy-regex bug
    where trailing prose after a JSON object gets concatenated into the
    captured substring.
    """
    text = (text or "").strip()
    if not text:
        raise SystemExit("[llm_commentary] API returned empty content.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Find the first balanced {...} block
    start = text.find("{")
    if start == -1:
        raise SystemExit("[llm_commentary] no JSON object found in response.")

    depth = 0
    in_string = False
    escape = False
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        print("[llm_commentary] unbalanced braces. First 600 chars:", file=sys.stderr)
        print(text[:600], file=sys.stderr)
        raise SystemExit("[llm_commentary] could not find a balanced JSON object.")

    candidate = text[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        print(f"[llm_commentary] parse error on candidate. First 600 chars:", file=sys.stderr)
        print(candidate[:600], file=sys.stderr)
        raise SystemExit(f"[llm_commentary] JSON parse error: {e}")


def call_minimax(system: str, user: str, model: str, base_url: str, api_key: str, max_tokens: int = 1800) -> str:
    """
    MiniMax Anthropic-compatible Messages API.

    Endpoint: {base_url}/messages  (NOT /chat/completions)
    Auth: Authorization: Bearer <key>
    Body: messages use content-blocks [{type:"text", text:"..."}],
          system is a top-level string field, NOT a message.
    Response: content is an array of blocks; text comes from the block
              where type == "text".
    """
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.85,
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user}]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[llm_commentary] HTTP {e.code} from {req.full_url}: {err_body[:500]}", file=sys.stderr)
        raise

    blocks = resp.get("content") or []
    text_chunks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    content = "".join(text_chunks).strip()
    # Diagnostic: log stop_reason + usage so we can debug truncation
    sr = resp.get("stop_reason")
    usage = resp.get("usage", {})
    print(f"[llm_commentary] stop_reason={sr}  usage={usage}  content_len={len(content)}", file=sys.stderr)
    if not content:
        print(f"[llm_commentary] empty content. Full response: {json.dumps(resp)[:500]}", file=sys.stderr)
        raise SystemExit("[llm_commentary] empty content")
    return content


STUB_FALLBACK = {
    "lede": {
        "headline": "BOOM — Power Rankings",
        "deck":     "Stub fallback — live model did not run this edition.",
        "body":     "Alright, here's what we got. The Tribune's printing on a short bench this week, so you're getting the deterministic stub. Numbers in the tables below are real; the words around them are not. Coach'll fix it Tuesday.",
    },
    "motw_blurb":     "Stub fallback: live model did not run.",
    "pick": {
        "favorite": "—",
        "spread": 0,
        "blurb":   "[STUB — no LLM pick this edition; live model did not run]",
    },
    "rankings_blurb": "Stub fallback: rankings walk will be added back when the live model runs.",
    "by_the_numbers": [
        {"value": "12", "label": "Teams in the Hunt"},
        {"value": "?",  "label": "Top Power Score"},
        {"value": "?",  "label": "Cellar Power Score"},
        {"value": "?",  "label": "Week Number"},
    ],
    "closing": "That's the stub. Live model will be back Tuesday.",
}


def _team_label(team_row: dict) -> str:
    """Best display label for a rankings row."""
    o = team_row.get("owner") or {}
    return o.get("team_name") or o.get("display_name") or "Unknown"


def _owner_label(team_row: dict) -> str:
    o = team_row.get("owner") or {}
    return o.get("display_name") or ""


def build_dynamic_stub(rankings: dict) -> dict | None:
    """
    Build a deterministic stub commentary from the live rankings.json —
    same shape the live LLM produces, but with real Week-N data instead
    of frozen preseason lines. Used whenever MINIMAX_API_KEY is unset so
    the page reads current state until the live model runs.

    Returns None if rankings is too empty to write anything meaningful;
    caller should fall back to STUB_FALLBACK in that case.
    """
    rs = rankings.get("rankings") or []
    if not rs:
        return None
    week = rankings.get("week") or "?"
    top = rs[0]
    second = rs[1] if len(rs) > 1 else None
    bottom = rs[-1]
    second_last = rs[-2] if len(rs) > 1 else None

    top_team = _team_label(top)
    top_owner = _owner_label(top)
    top_record = f"{top.get('wins', 0)}-{top.get('losses', 0)}"
    top_pf = round(top.get("points_for") or 0, 1)
    top_power = round(top.get("power_score") or 0, 1)

    bottom_team = _team_label(bottom)
    bottom_record = f"{bottom.get('wins', 0)}-{bottom.get('losses', 0)}"
    bottom_pf = round(bottom.get("points_for") or 0, 1)

    second_team = _team_label(second) if second else None
    second_power = round((second or {}).get("power_score") or 0, 1) if second else None

    # --- Lede ---
    headline = f"BOOM — Week {week}, and {top_team}'s on top"
    deck = (
        f"{top_team} ({top_record}) sits at #1 with {top_pf} points and a "
        f"{top_power} Power score. Twelve teams, one week in the books."
    )
    if second:
        body = (
            f"Alright, alright, here we are, week {week}. {top_team} — that's "
            f"{top_owner or 'the top dog'} — sits at number one with a "
            f"{top_record} record and {top_pf} on the scoreboard. "
            f"{second_team} is right behind at #2 with a {second_power} "
            f"Power score, so don't get comfortable up there. "
            f"Bottom of the page: {bottom_team}, {bottom_record}, "
            f"{bottom_pf} points. Bang-bang. Let's get into it."
        )
    else:
        body = (
            f"Alright, here we go, week {week}. {top_team} at number one, "
            f"{top_record} on the year, {top_pf} points on the board. "
            f"Twelve teams, one rung each. Bang-bang."
        )

    # --- MOTW blurb ---
    motw = rankings.get("matchup_of_week") or {}
    motw_blurb = ""
    if motw.get("team_a") and motw.get("team_b"):
        ta = motw["team_a"]; tb = motw["team_b"]
        ta_team = ta.get("team") or ta.get("name") or "team_a"
        tb_team = tb.get("team") or tb.get("name") or "team_b"
        ta_rank = ta.get("rank", "?"); tb_rank = tb.get("rank", "?")
        ta_proj = ta.get("projected_points"); tb_proj = tb.get("projected_points")
        if isinstance(ta_proj, (int, float)) and isinstance(tb_proj, (int, float)):
            motw_blurb = (
                f"BOOM — {ta_team} (#{ta_rank}) at {round(ta_proj,1)} projected "
                f"and {tb_team} (#{tb_rank}) at {round(tb_proj,1)}. "
                f"Big board at the top, somebody's gotta blink. We'll see who Sunday."
            )
        else:
            motw_blurb = (
                f"BOOM — {ta_team} (#{ta_rank}) and {tb_team} (#{tb_rank}), "
                f"and this is the one the Tribune's watching. Big board at the "
                f"top, somebody's gotta blink. We'll see who Sunday."
            )

    # --- Rankings blurb ---
    if second_last:
        second_last_team = _team_label(second_last)
        second_last_record = f"{second_last.get('wins', 0)}-{second_last.get('losses', 0)}"
        rankings_blurb = (
            f"Now watch this — {top_team} at number one, "
            f"{second_team} at two, and {second_last_team} at eleven ({second_last_record}). "
            f"{bottom_team} at the bottom ({bottom_record}). "
            f"It's a long season. Twelve teams, only one trophy, and a lot of "
            f"tape to watch between now and December."
        )
    else:
        rankings_blurb = (
            f"Now watch this — {top_team} at number one, "
            f"{bottom_team} at the bottom. It's a long season, and the board's "
            f"gonna move every Tuesday. Twelve teams, only one trophy."
        )

    # --- By the numbers ---
    by_the_numbers = [
        {"value": str(top_power),  "label": "Top Power Score"},
        {"value": str(round((rs[-1].get("power_score") or 0), 1)), "label": "Cellar Power Score"},
        {"value": str(len(rs)),    "label": "Teams in the Hunt"},
        {"value": str(week),       "label": "Week Number"},
    ]

    # --- Closing ---
    closing = (
        f"That's week {week}, folks. Same couch, same friends, next Tuesday. BOOM."
    )

    out = {
        "lede": {"headline": headline, "deck": deck, "body": body},
        "rankings_blurb": rankings_blurb,
        "by_the_numbers": by_the_numbers,
        "closing": closing,
    }
    if motw_blurb:
        out["motw_blurb"] = motw_blurb
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rankings",        default="rankings.json")
    ap.add_argument("--config",          default="config.json")
    ap.add_argument("--league-context",  default="league_context.json")
    ap.add_argument("--out",             default="commentary.json")
    ap.add_argument("--dry-run",         action="store_true",
                    help="Skip the API call and write a stub commentary file.")
    args = ap.parse_args()

    rankings = json.loads(Path(args.rankings).read_text())
    cfg      = json.loads(Path(args.config).read_text())
    llm_cfg  = cfg.get("llm", {})
    context  = load_league_context(Path(".").resolve())

    personal_bits_count = int(llm_cfg.get("personal_bits", 0))
    chosen_bits = pick_personal_bits(context, rankings, n=personal_bits_count) if personal_bits_count > 0 else []
    if chosen_bits:
        handles = [h for b in chosen_bits for h in b.get("handles", [])]
        print(f"[llm_commentary] picked {len(chosen_bits)} personal bit(s) — about: {handles}", file=sys.stderr)
    user_prompt = build_user_prompt(rankings, cfg, context, personal_bits=chosen_bits)

    if args.dry_run or not os.environ.get("MINIMAX_API_KEY"):
            # Dynamic stub first: build a real-data stub from rankings.json so
            # the page reads current state instead of frozen preseason lines.
            # STUB_FALLBACK only fires if the dynamic builder can't read the
            # rankings at all (e.g. data.json corruption).
            stub = build_dynamic_stub(rankings) or dict(STUB_FALLBACK)
            # motw_blurb: the dynamic builder already sets this from the live
            # matchup_of_week. Don't overwrite it.
            if not isinstance(stub.get("pick"), dict) or not stub["pick"].get("favorite") or stub["pick"]["favorite"] == "—":
                stub["pick"] = _fallback_pick(rankings, stub) or stub.get("pick", STUB_FALLBACK["pick"])
            Path(args.out).write_text(json.dumps(stub, indent=2))
            print(f"[llm_commentary] wrote {args.out} (stub, no API key) — pick filled from projections")
            return

    raw = call_minimax(
            system=SYSTEM_PROMPT + OUTPUT_FORMAT_NOTES,
            user=user_prompt,
            model=llm_cfg.get("model", "MiniMax-M3"),
            base_url=llm_cfg.get("base_url", "https://api.minimax.io/anthropic/v1").rstrip("/"),
            api_key=os.environ["MINIMAX_API_KEY"],
            max_tokens=llm_cfg.get("max_tokens", 1800),
        )
    commentary = extract_json(raw)

    # Validate: only `lede` is mandatory. The LLM may end_turn early
    # if it judges the body is long enough; missing optional fields
    # are gracefully suppressed by the template rather than triggering
    # a full stub fallback (which is jarring — losing the LLM's actual
    # lede and pick because closing didn't get written).
    if not commentary.get("lede") or not isinstance(commentary.get("lede"), dict):
        print(f"[llm_commentary] lede missing or not a dict. Falling back to stub.", file=sys.stderr)
        print(f"[llm_commentary] Got keys: {list(commentary.keys())}", file=sys.stderr)
        commentary = STUB_FALLBACK

    # Sanity-check the pick: if it's malformed, drop it (template hides
    # the box if pick.favorite is empty) but keep the rest of the
    # commentary. Don't fall back to stub for one bad field.
    pk = commentary.get("pick")
    if isinstance(pk, dict):
        if not pk.get("favorite") or not isinstance(pk.get("spread"), (int, float)):
            print(f"[llm_commentary] pick malformed: {pk}. Dropping pick.", file=sys.stderr)
            commentary["pick"] = None
    elif pk is None:
        # LLM omitted the pick — fill it in deterministically from the
        # projected spread in rankings.json so the Tribune never goes
        # without a betting line.
        commentary["pick"] = _fallback_pick(rankings, commentary)
    else:
        # Unexpected type
        commentary["pick"] = None

    if commentary.get("pick") is None:
        commentary["pick"] = _fallback_pick(rankings, commentary)

    Path(args.out).write_text(json.dumps(commentary, indent=2))
    print(f"[llm_commentary] wrote {args.out} (from API)")


def _fallback_pick(rankings: dict, commentary: dict) -> dict | None:
    """
    Build a deterministic pick from the projected spread in rankings.json
    when the LLM omits the pick or returns a malformed one. Favorite gets
    a negative spread (Vegas convention), underdog gets positive. Blurb is
    a short Madden-voice stub.

    If the projected spread is 0 or missing (e.g. tied projections early
    in the week), fall back to picking the higher-ranked team (team_a) with
    a small synthetic favorite spread of 3 — never return None. The Tribune
    editorial policy ships a deterministic placeholder rather than an empty
    box, so the Tribune Pick section must always have a real team in it.
    """
    motw = (rankings or {}).get("matchup_of_week") or {}
    if not motw or not (motw.get("team_a") and motw.get("team_b")):
        return None
    spread_raw = motw.get("projected_spread")
    if not isinstance(spread_raw, (int, float)) or spread_raw == 0:
        # Tie or no projections — synthesize a small favorite line.
        spread = 3.0
        fav_side = "team_a"
    else:
        spread = float(spread_raw)
        fav_side = motw.get("projected_favorite") or (
            "team_a" if spread >= 0 else "team_b"
        )
    fav = motw.get(fav_side) or {}
    under_side = "team_b" if fav_side == "team_a" else "team_a"
    under = motw.get(under_side) or {}
    fav_name = fav.get("team") or fav.get("name") or fav_side
    under_name = under.get("team") or under.get("name") or under_side
    return {
        "favorite": fav_name,
        "spread": -round(spread, 2),  # favorite is negative (Vegas)
        "blurb": f"BOOM — {fav_name} lays {round(spread, 1)} on the road against {under_name}. Tribune calls it straight up. Cook the books.",
    }


if __name__ == "__main__":
    main()