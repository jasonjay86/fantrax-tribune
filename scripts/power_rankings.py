"""
power_rankings.py — compute Composite Power Score and Matchup of the Week
for the Fantrax league.

Formula (per config.json weights):
  PowerScore = w_win * W%
             + w_pf  * (PFpg / league_avg_PFpg)
             + w_sos * SoS_factor
             + w_ap  * AllPlayW%
         multiplied by te_premium_factor (TE premium 1.5/rec in this league)

Usage: python power_rankings.py [--in data.json] [--out rankings.json]
"""
import argparse
import json
import urllib.request
from collections import defaultdict
from pathlib import Path


def load_generic_names(repo_root: Path) -> dict[str, str]:
    """
    Load commissioner-provided display-name overrides from league_context.json.
    Returns {team_name_or_id: display_name}. Fantrax team names are visible
    on the page; we don't override them with handles here.
    """
    ctx_path = repo_root / "league_context.json"
    if not ctx_path.exists():
        return {}
    try:
        ctx = json.loads(ctx_path.read_text())
        members = ctx.get("members") or {}
        # Map Fantrax team_name -> handle (used as the byline for the family 4)
        return {
            (info.get("team_name") or handle): handle
            for handle, info in members.items()
            if info.get("team_name")
        }
    except Exception:
        return {}


def build_user_map(users, generic_names: dict[str, str] | None = None):
    """
    Returns dict[team_id] -> {display_name, team_name, handle}.

    For Fantrax, "display_name" is the team_name (which is what readers see),
    and "handle" is the Fantrax username when we have it from league_context.
    """
    generic_names = generic_names or {}
    out = {}
    for u in users:
        tid = u.get("team_id") or u.get("user_id")
        team_name = u.get("team_name") or "Unknown"
        handle = generic_names.get(team_name)  # may be None for non-family
        out[tid] = {
            "display_name": team_name,   # what shows in the Tribune
            "team_name":    "",          # we already use display_name as the team label
            "handle":       handle,
        }
    return out


def current_week(bundle: dict) -> int:
    """
    Best-effort current week. Fantrax doesn't expose a /state/nfl equivalent
    we can hit cheaply; we use the first scoringPeriod that's not "off" if
    available, otherwise default to 1.
    """
    raw = bundle.get("_raw_league_info") or {}
    periods = raw.get("scoringPeriods") or []
    for p in periods:
        # Some scoringPeriods are dicts with status info, others are ints
        if isinstance(p, dict):
            status = (p.get("status") or "").lower()
            if status not in ("off", "final", "completed", ""):
                return p.get("period", 1)
        elif isinstance(p, int) and p == 1:
            return 1
    return 1


def standings_to_enriched(standings: list, user_map: dict, roster_keys: list) -> list[dict]:
    """
    Build the per-team enriched dict from Fantrax standings + roster keys.
    """
    by_id = {s.get("teamId"): s for s in standings if isinstance(s, dict)}
    enriched = []
    for tid in roster_keys:
        s = by_id.get(tid) or {}
        wins   = 0
        losses = 0
        ties   = 0
        # Fantrax encodes W-L-T as a string like "0-0-0" in standings["points"]
        wlt = (s.get("points") or "0-0-0").split("-")
        try:
            wins, losses, ties = int(wlt[0]), int(wlt[1]), int(wlt[2]) if len(wlt) > 2 else 0
        except (ValueError, IndexError):
            pass
        games   = wins + losses + ties
        pf      = float(s.get("totalPointsFor") or 0)
        enriched.append({
            "team_id":      tid,
            "owner":        user_map.get(tid, {}),
            "wins":         wins,
            "losses":       losses,
            "ties":         ties,
            "games":        games,
            "points_for":   pf,
            "points_against": 0.0,  # not exposed in public standings yet
            "win_pct":      (wins + 0.5 * ties) / games if games else 0.0,
            "pf_per_game":  pf / games if games else 0.0,
        })
    return enriched


def te_premium_factor(rosters_flat: dict, team_id: str, players_index: dict) -> float:
    """
    Weight a team's TE holdings — in this league TEs get 1.5/rec vs 1.0 PPR
    for everyone else. A roster with multiple elite TEs is structurally stronger.

    Returns a small multiplier (~0.95-1.05) intended to nudge the composite
    without dominating the other factors.
    """
    if team_id not in rosters_flat:
        return 1.0
    te_count = sum(
        1 for item in rosters_flat[team_id]
        if item.get("position") == "TE"
        and item.get("status") == "ACTIVE"
    )
    # 0 TEs = 0.97, 1 = 1.0, 2+ = 1.03 (slight premium signal)
    return {0: 0.97, 1: 1.0, 2: 1.03}.get(min(te_count, 2), 1.03)


def pick_key_players(roster: list, players_index: dict, n: int = 2,
                      starter_projections: dict[str, float] | None = None) -> list[dict]:
    """
    Choose up to N ACTIVE players from a Fantrax roster.

    Strategy:
    - If `starter_projections` is provided (dict {player_id: projected_points}),
      rank active players by projected points and take the top N. This surfaces
      the actual top scorers (e.g., a hot WR3 over a mediocre QB1) instead of
      always returning QB + RB1 by position order.
    - Otherwise, fall back to position priority (QB → RB → WR/TE → ...) which
      is the previous behavior.
    """
    pos_priority = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "RWT": 4, "K": 5, "DST": 6}
    active = [it for it in (roster or []) if it.get("status") == "ACTIVE"]
    if starter_projections:
        # Sort by projected points desc; tiebreak by position priority
        # then by player_id so the order is deterministic.
        indexed = list(enumerate(active))
        indexed.sort(
            key=lambda ix: (
                -float(starter_projections.get(ix[1].get("player_id"), 0.0)),
                pos_priority.get(ix[1].get("position", "?"), 99),
                ix[1].get("player_id", ""),
            )
        )
        chosen = [it for _, it in indexed[:n]]
    else:
        active.sort(key=lambda it: pos_priority.get(it.get("position", "?"), 99))
        chosen = active[:n]
    out = []
    for item in chosen:
        pid = item.get("player_id")
        info = players_index.get(pid) or item.get("player") or {}
        out.append({
            "name":          info.get("name") or pid,
            "position":      item.get("position"),
            "team":          info.get("team") or "FA",
            "projected_pts": starter_projections.get(pid) if starter_projections else None,
            "years_exp":     info.get("years_exp"),  # 0 = rookie, 1+ = experienced
            "age":           info.get("age"),
        })
    return out


def compute_rankings(bundle: dict, weights: dict,
                     generic_names: dict[str, str] | None = None) -> dict:
    users   = bundle.get("users") or []
    rosters_flat = bundle.get("rosters") or {}
    standings = bundle.get("standings") or []
    players_index = bundle.get("players_index") or {}
    team_starter_projections = bundle.get("team_starter_projections") or {}

    user_map = build_user_map(users, generic_names=generic_names)
    enriched = standings_to_enriched(standings, user_map, list(rosters_flat.keys()))

    week = current_week(bundle)

    played = [r for r in enriched if r["games"] > 0]

    # Preseason / week-1 path — season hasn't started. Return zeroed rankings
    # with a schedule preview for week 1 (period 1 matchups from league_info).
    if not played:
        raw = bundle.get("_raw_league_info") or {}
        period1 = next((p for p in (raw.get("matchups") or []) if p.get("period") == 1), None)
        preview_motw = None
        if period1 and enriched:
            # Build a lookup by team_id
            by_team = {r["team_id"]: r for r in enriched}
            pairs = period1.get("matchupList") or []
            # Pick the first matchup as the preview
            if pairs:
                p = pairs[0]
                a_id = p.get("away", {}).get("id")
                b_id = p.get("home", {}).get("id")
                a = by_team.get(a_id) if a_id else None
                b = by_team.get(b_id) if b_id else None
                if a and b:
                    # Build via the same helper that attaches projected spread.
                    # Inline motw_team_side here because it's defined later in
                    # the function body (Python closure can't see it from the
                    # early-return path otherwise).
                    def _side(team_row, tid):
                        proj = None
                        matchups_with_proj = bundle.get("matchups") or []
                        period_matchups = next((p for p in matchups_with_proj if p.get("period") == week), None)
                        if period_matchups:
                            for m in period_matchups.get("matchupList", []):
                                if m.get("away", {}).get("id") == tid:
                                    proj = m.get("away_projected_points")
                                    break
                                if m.get("home", {}).get("id") == tid:
                                    proj = m.get("home_projected_points")
                                    break
                        side = {
                            "name":   team_row["owner"].get("display_name", "?"),
                            "team":   team_row["owner"].get("team_name", ""),
                            "rank":   0,
                            "score":  50.0,
                            "record": "0-0",
                            "key_players": pick_key_players(
                                rosters_flat.get(tid, []), players_index, n=2,
                                starter_projections=team_starter_projections.get(tid),
                            ),
                        }
                        if isinstance(proj, (int, float)):
                            side["projected_points"] = round(proj, 2)
                        return side
                    a_side = _side(a, a_id)
                    b_side = _side(b, b_id)
                    preview_motw = {
                        "status": "preview",
                        "team_a": a_side,
                        "team_b": b_side,
                    }
                    if "projected_points" in a_side and "projected_points" in b_side:
                        a_pts = a_side["projected_points"]
                        b_pts = b_side["projected_points"]
                        spread = round(abs(a_pts - b_pts), 2)
                        preview_motw["projected_spread"]   = spread
                        preview_motw["projected_favorite"] = "team_a" if a_pts >= b_pts else "team_b"
        return {
            "league":      bundle.get("league", {}).get("name"),
            "week":        week,
            "season":      bundle.get("league", {}).get("season"),
            "season_type": "regular",
            "rankings": [
                {**r, "pf_per_game": 0.0, "sos_factor": 1.0,
                 "all_play_pct": 0.5, "raw_score": 0.0, "power_score": 50.0,
                 "te_factor": 1.0, "rank": i + 1}
                for i, r in enumerate(enriched)
            ],
            "matchup_of_week": preview_motw,
        }

    # In-season path — composite ranking
    pf_by_team = {r["team_id"]: r["pf_per_game"] for r in enriched}
    league_avg = sum(pf_by_team.values()) / max(len(pf_by_team), 1)

    for r in enriched:
        r["sos_factor"]   = 1.0  # SoS deferred until we have at least one week of matchup data
        r["all_play_pct"] = r["win_pct"]
        r["te_factor"]    = te_premium_factor(rosters_flat, r["team_id"], players_index)

    for r in enriched:
        pf_norm = (r["pf_per_game"] / league_avg) if league_avg else 1.0
        r["raw_score"] = (
            weights.get("win_pct", 0.40)            * r["win_pct"]
          + weights.get("pf_per_game", 0.25)        * pf_norm
          + weights.get("strength_of_schedule", 0.20) * r["sos_factor"]
          + weights.get("all_play_win_pct", 0.15)   * r["all_play_pct"]
        ) * r["te_factor"]

    ranked = sorted(enriched, key=lambda x: x["raw_score"], reverse=True)
    top = ranked[0]["raw_score"]
    bot = ranked[-1]["raw_score"]
    spread = top - bot if top != bot else 1.0
    for i, r in enumerate(ranked):
        r["power_score"] = 100 - ((top - r["raw_score"]) / spread) * 60
        r["rank"] = i + 1

    # Matchup of the Week — top-2 if they play each other this week; else
    # top-ranked team vs. highest-ranked available opponent
    raw = bundle.get("_raw_league_info") or {}
    week_matchups = next((p for p in (raw.get("matchups") or []) if p.get("period") == week), None)
    week_pairs = []
    if week_matchups:
        for m in week_matchups.get("matchupList", []):
            a = m.get("away", {}).get("id")
            b = m.get("home", {}).get("id")
            if a and b:
                week_pairs.append({a, b})

    # Matchup-of-the-Week selection:
    #   1. Top 2 play each other this week → use them (the marquee case).
    #   2. Otherwise pick the most compelling matchup: maximise combined
    #      wins first (a close game between two 0-1 squads is not marquee;
    #      a 10-point game between two 1-0 squads is), then break ties
    #      by the smallest projected spread, then by highest combined
    #      projected total as a final tiebreaker.
    motw = None
    motw_status = "preview"
    rank_by_id = {r["team_id"]: r["rank"] for r in ranked}
    ranked_by_id = {r["team_id"]: r for r in ranked}

    if ranked and len(ranked) >= 2:
        top_two = (ranked[0]["team_id"], ranked[1]["team_id"])
        for pair in week_pairs:
            if top_two[0] in pair and top_two[1] in pair:
                motw = (ranked[0], ranked[1])
                break

    if motw is None and ranked and week_pairs:
        # Look up projected points from bundle["matchups"] for this period.
        period_matchups = next(
            (p for p in (bundle.get("matchups") or [])
             if p.get("period") == week), None
        )
        proj_lookup = {}
        if period_matchups:
            for m in period_matchups.get("matchupList", []):
                a_id = m.get("away", {}).get("id")
                h_id = m.get("home", {}).get("id")
                pa = m.get("away_projected_points")
                pb = m.get("home_projected_points")
                if a_id and isinstance(pa, (int, float)):
                    proj_lookup[a_id] = pa
                if h_id and isinstance(pb, (int, float)):
                    proj_lookup[h_id] = pb

        best = None  # (-combined_wins, spread, -combined_proj, a_row, b_row)
        for pair in week_pairs:
            ids = [tid for tid in pair if tid in rank_by_id]
            if len(ids) != 2:
                continue
            a_id, b_id = ids
            a_row, b_row = ranked_by_id[a_id], ranked_by_id[b_id]
            combined_wins = a_row["wins"] + b_row["wins"]
            pa = proj_lookup.get(a_id)
            pb = proj_lookup.get(b_id)
            if isinstance(pa, (int, float)) and isinstance(pb, (int, float)):
                spread = abs(pa - pb)
                combined_proj = pa + pb
            else:
                spread = float("inf")
                combined_proj = -float("inf")
            # Lower is better: more wins first, then closer spread,
            # then higher combined projection as the final tiebreaker.
            key = (-combined_wins, spread, -combined_proj)
            if best is None or key < best[0]:
                best = (key, a_row, b_row)
        if best is not None:
            motw = (best[1], best[2])

    def motw_team_side(team_row, tid):
        # Find the projected_points for this team in the enriched matchup list.
        # data.json.matchups[i].matchupList[j] now carries away_projected_points
        # / home_projected_points attached by fetch_fantrax.py.
        proj = None
        matchups_with_proj = bundle.get("matchups") or []
        period_matchups = next((p for p in matchups_with_proj if p.get("period") == week), None)
        if period_matchups:
            for m in period_matchups.get("matchupList", []):
                if m.get("away", {}).get("id") == tid:
                    proj = m.get("away_projected_points")
                    break
                if m.get("home", {}).get("id") == tid:
                    proj = m.get("home_projected_points")
                    break
        side = {
            "name":   team_row["owner"].get("display_name", "?"),
            "team":   team_row["owner"].get("team_name", ""),
            "rank":   team_row["rank"],
            "score":  team_row["power_score"],
            "record": f"{team_row['wins']}-{team_row['losses']}" + (f"-{team_row['ties']}" if team_row['ties'] else ""),
            "key_players": pick_key_players(
                rosters_flat.get(tid, []), players_index, n=2,
                starter_projections=team_starter_projections.get(tid),
            ),
        }
        if isinstance(proj, (int, float)):
            side["projected_points"] = round(proj, 2)
        return side

    motw_payload = None
    if motw:
        tid_a = motw[0]["team_id"]
        tid_b = motw[1]["team_id"]
        proj_a = proj_b = spread = None
        matchups_with_proj = bundle.get("matchups") or []
        period_matchups = next((p for p in matchups_with_proj if p.get("period") == week), None)
        if period_matchups:
            for m in period_matchups.get("matchupList", []):
                a, h = m.get("away", {}).get("id"), m.get("home", {}).get("id")
                if {a, h} == {tid_a, tid_b}:
                    proj_a = m.get("away_projected_points")
                    proj_b = m.get("home_projected_points")
                    spread = m.get("matchup_projected_spread")
                    break
        motw_payload = {
            "status": motw_status,
            "team_a": motw_team_side(motw[0], tid_a),
            "team_b": motw_team_side(motw[1], tid_b),
        }
        if isinstance(spread, (int, float)) and isinstance(proj_a, (int, float)) and isinstance(proj_b, (int, float)):
            motw_payload["projected_spread"]   = spread
            motw_payload["projected_favorite"] = "team_a" if proj_a >= proj_b else "team_b"

    return {
        "league":      bundle.get("league", {}).get("name"),
        "week":        week,
        "season":      bundle.get("league", {}).get("season"),
        "season_type": "regular",
        "rankings":    ranked,
        "matchup_of_week": motw_payload,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="rankings.json")
    args = ap.parse_args()

    bundle = json.loads(Path(args.inp).read_text())
    cfg    = json.loads(Path(args.config).read_text())
    generic_names = load_generic_names(Path(".").resolve())
    result = compute_rankings(bundle, cfg["weights"], generic_names=generic_names)

    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"[power_rankings] wrote {args.out}")
    print(f"  {len(result['rankings'])} teams ranked, week {result['week']} ({result['season_type']})")
    for r in result["rankings"]:
        owner = r["owner"].get("display_name", "?") if r["owner"] else "?"
        print(f"  #{r['rank']:>2}  {owner:<28}  Power {r['power_score']:5.1f}  "
              f"W% {r['win_pct']:.3f}  PFpg {r['pf_per_game']:>6.1f}  "
              f"TE×{r.get('te_factor',1.0):.2f}")
    if result["matchup_of_week"]:
        m = result["matchup_of_week"]
        print(f"  Matchup of the Week: #{m['team_a']['rank']} {m['team_a']['name']} "
              f"vs #{m['team_b']['rank']} {m['team_b']['name']}")


if __name__ == "__main__":
    main()
