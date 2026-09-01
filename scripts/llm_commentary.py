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
- motw_blurb:     STRING, plain prose, ~70 words
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
                                  3 to 30.)
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
- motw_blurb:     STRING, plain prose, ~70 words
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
                                  3 to 30.)
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

    return json.dumps(payload, indent=2)


def _motw_payload(motw):
    if not motw:
        return None
    return {
        "status": motw["status"],
        "team_a": motw["team_a"],
        "team_b": motw["team_b"],
    }


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
        "headline": "BOOM \u2014 The Ballers Tribune Is Open For Business",
        "deck":     "Twelve teams, twelve zeroes, one family league and a budding rivalry. The Tribune is on the couch.",
        "body":     "Alright, alright, here we are. Twelve managers, twelve rosters, twelve zeroes across the board. You can't tell me anything yet \u2014 nobody's played. It's like calling a baseball game when the pitcher's still in the dugout. But here's what I CAN see. The patriarch is in. Three of his kids are in. The fourth sibling is somewhere else and probably relieved about it. There's a commissioner in here who happens to be the youngest of the bunch, which is its own little drama. And somebody named Roundhouse is allegedly feuding with someone \u2014 they won't say who. That's gonna be the year. Let's watch some football.",
    },
    "motw_blurb":     "BOOM \u2014 first matchup on the docket. Two teams, no scores, all preview. The Tribune is keeping the receipt for later. Dad's team is involved, which means somebody's about to learn something about their own roster in front of their kids.",
    "pick": {
        "favorite": "\u2014",
        "spread": 0,
        "blurb":   "[STUB \u2014 no LLM pick this edition; live model did not run]",
    },
    "rankings_blurb": "Now watch this \u2014 here's a guy who's ranked number one. And here's another guy ranked number one. They're all ranked number one, that's the problem. Tiebreakers? None. Schedule? Same. Power score? Fifty flat, the whole board. You can't rank 'em yet. You just gotta play 'em.",
    "by_the_numbers": [
        {"value": "12", "label": "Coaches Drawing Up Plays"},
        {"value": "0",  "label": "Games In The Books"},
        {"value": "4",  "label": "Family Members On One Roster Tree"},
        {"value": "1",  "label": "Budding Rivalry Nobody Will Name"},
    ],
    "closing": "That's the week. Same couch, same family, next Tuesday. BOOM.",
}


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
        Path(args.out).write_text(json.dumps(STUB_FALLBACK, indent=2))
        print(f"[llm_commentary] wrote {args.out} (stub, no API key)")
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
        pass  # LLM omitted it intentionally
    else:
        # Unexpected type
        commentary["pick"] = None

    Path(args.out).write_text(json.dumps(commentary, indent=2))
    print(f"[llm_commentary] wrote {args.out} (from API)")


if __name__ == "__main__":
    main()