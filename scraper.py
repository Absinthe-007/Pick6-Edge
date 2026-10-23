#!/usr/bin/env python3
"""
Pick6 Edge scraper
==================
Pulls every DraftKings Pick6 line for NFL, College Football and NBA, enriches each
prop with the player's recent game log / season average / matchup notes, pulls
player-prop and game odds from sportsbooks, and computes the probability of the
prop going MORE or LESS plus the best place to play it.

Output: data/props.json  (read by index.html)

Sources (all public, no login):
  * DraftKings Pick6        pick6.draftkings.com       (lines, alt ladders, last-5 stats, news, matchup)
  * BetRivers (Kambi feed)  eu-offering-api.kambicdn   (player props + game lines)
  * Bovada                  bovada.lv                  (player props + game lines)
  * The Odds API (optional) api.the-odds-api.com       (DraftKings / FanDuel / BetMGM / Caesars ... props)
      -> set env ODDS_API_KEY (GitHub secret) to switch it on.

Every source is fail-soft: if one is blocked or down, the rest still run.
"""
import json
import math
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "pick6-edge/1.0"}
P6 = "https://pick6.draftkings.com"
KAMBI = "https://eu-offering-api.kambicdn.com/offering/v2018/rsiusnj"
BOVADA = "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
ODDS_API = "https://api.the-odds-api.com/v4"

SPORTS = {
    "NFL": {"kambi": "american_football/nfl", "bovada": "football/nfl", "oddsapi": "americanfootball_nfl"},
    "CFB": {"kambi": "american_football/ncaaf", "bovada": "football/college-football", "oddsapi": "americanfootball_ncaaf"},
    "NBA": {"kambi": "basketball/nba", "bovada": "basketball/nba", "oddsapi": "basketball_nba"},
}

SESSION = requests.Session()
SESSION.headers.update(UA)
LOG = []


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.append(msg)


def get(url, params=None, as_text=False, tries=3):
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.text if as_text else r.json()
            if r.status_code in (403, 404, 400, 401, 426):
                return None
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


# --------------------------------------------------------------------------------------
# turbo-stream decoder (React Router v7 data format used by pick6.draftkings.com)
# --------------------------------------------------------------------------------------
_SPECIAL = {-1: None, -2: None, -3: None, -4: 0.0, -5: None, -6: None, -7: None}


def turbo_decode(text):
    lines = [l for l in text.split("\n") if l.strip()]
    values = json.loads(lines[0])
    cache = {}

    def hyd(idx):
        if isinstance(idx, int) and idx < 0:
            return _SPECIAL.get(idx)
        if idx in cache:
            return cache[idx]
        v = values[idx]
        if isinstance(v, list):
            if len(v) >= 2 and isinstance(v[0], str) and len(v[0]) == 1:
                cache[idx] = v[1]
                return v[1]
            out = []
            cache[idx] = out
            out.extend(hyd(i) for i in v)
            return out
        if isinstance(v, dict):
            out = {}
            cache[idx] = out
            for k, vi in v.items():
                key = values[int(k[1:])] if k.startswith("_") else k
                out[key] = hyd(vi)
            return out
        cache[idx] = v
        return v

    return hyd(0)


# --------------------------------------------------------------------------------------
# Name + stat normalisation
# --------------------------------------------------------------------------------------
def norm_name(n):
    if not n:
        return ""
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = re.sub(r"\s*\([^)]*\)", "", n)
    n = re.sub(r"\s+-\s+(1h|1q|2h|game)$", "", n, flags=re.I)
    if "," in n:  # "Fant, Noah" -> "Noah Fant"
        last, first = n.split(",", 1)
        n = f"{first} {last}"
    n = n.lower().replace(".", "").replace("'", "").replace("-", " ")
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


PARTIAL = re.compile(r"(1st|2nd|3rd|4th|first|second|third|fourth)\s*(half|quarter|qtr|inning|5)|\b1h\b|\b2h\b|\b1q\b|\b2q\b|\b3q\b|\b4q\b|quarter|half|first 5|1st 5|f5|first inning|most |game high|\d+\+", re.I)


def special_key(label):
    """TD-scorer style markets (checked before the partial-period filter)."""
    s = label.lower()
    if "first touchdown scorer" in s or "1st touchdown scorer" in s or s.startswith("1st td"):
        return None if (" - " in s and "overtime" not in s) else "first_td"
    if ("1h" in s or "first half" in s) and ("td scorer" in s or "touchdown" in s) and "2+" not in s and "3+" not in s:
        return "td_1h"
    if ("1q" in s or "quarter 1" in s or "1st quarter" in s) and ("td scorer" in s or "touchdown" in s) and "2+" not in s:
        return "td_1q"
    return None


def full_key(label, sport):
    if sport in ("NFL", "CFB"):
        k = special_key(label)
        if k:
            return k
        if "home td" in label.lower() or "away td" in label.lower():
            return None
    return None if PARTIAL.search(label) else stat_key(label, sport)


def stat_key(label, sport):
    """Map any book / Pick6 stat label to a canonical key. Returns None if unsupported."""
    s = label.lower()
    s = s.replace("&", "+").replace(" and ", " + ")
    if sport in ("NFL", "CFB"):
        if "first" in s and "touchdown" in s or "1st" in s and ("td" in s or "touchdown" in s):
            return None
        if ("pass" in s and "rush" in s and "yard" in s):
            return "pass_rush_yds"
        if ("rush" in s and "rec" in s and "yard" in s):
            return "rush_rec_yds"
        if ("rush" in s and "rec" in s and ("td" in s or "touchdown" in s)) or "anytime touchdown" in s or s.strip() in ("touchdown scorer", "player to score a touchdown"):
            return "anytime_td"
        if "longest" in s and "recep" in s:
            return "long_rec"
        if "longest" in s and "rush" in s:
            return "long_rush"
        if "longest" in s and ("pass" in s or "completion" in s):
            return "long_pass"
        if "passing" in s and "yard" in s:
            return "pass_yds"
        if ("passing" in s or "pass" in s) and ("td" in s or "touchdown" in s):
            return "pass_tds"
        if "pass" in s and "attempt" in s:
            return "pass_att"
        if "complet" in s:
            return "pass_cmp"
        if "intercept" in s:
            return "ints"
        if "rush" in s and "yard" in s:
            return "rush_yds"
        if "rush" in s and "attempt" in s:
            return "rush_att"
        if "receiving" in s and "yard" in s or s.startswith("rec yards"):
            return "rec_yds"
        if "reception" in s:
            return "receptions"
        if "field goal" in s:
            return "fg_made"
        if "kicking point" in s:
            return "kick_pts"
        if "tackle" in s:
            return "tackles"
        if "sack" in s:
            return "sacks"
        return None
    # NBA
    if "first" in s or "1st" in s or "double" in s or "fantasy" in s:
        return None
    if re.search(r"3[- ]?p(oint|t)|three|made 3|threes", s):
        return "threes"
    pts = "point" in s or re.search(r"\bpts\b", s)
    reb = "rebound" in s or re.search(r"\brebs?\b", s)
    ast = "assist" in s or re.search(r"\basts?\b", s)
    stl = "steal" in s or re.search(r"\bstls?\b", s)
    blk = "block" in s or re.search(r"\bblks?\b", s)
    if pts and reb and ast:
        return "pra"
    if pts and reb:
        return "pts_reb"
    if pts and ast:
        return "pts_ast"
    if reb and ast:
        return "reb_ast"
    if stl and blk:
        return "stl_blk"
    if pts:
        return "points"
    if reb:
        return "rebounds"
    if ast:
        return "assists"
    if stl:
        return "steals"
    if blk:
        return "blocks"
    if "turnover" in s:
        return "turnovers"
    return None


COUNT_STATS = {"first_td", "td_1h", "td_1q", "receptions", "pass_tds", "pass_att", "pass_cmp", "ints", "rush_att", "anytime_td", "fg_made", "kick_pts",
               "tackles", "sacks", "threes", "steals", "blocks", "stl_blk", "turnovers"}
CV = {"pass_yds": 0.30, "rush_yds": 0.55, "rec_yds": 0.65, "rush_rec_yds": 0.50, "pass_rush_yds": 0.30,
      "long_rec": 0.55, "long_rush": 0.6, "long_pass": 0.35,
      "points": 0.32, "rebounds": 0.40, "assists": 0.45, "pra": 0.26, "pts_reb": 0.29, "pts_ast": 0.30, "reb_ast": 0.36}


# --------------------------------------------------------------------------------------
# Probability helpers
# --------------------------------------------------------------------------------------
def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def phi_inv(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def pois_over(lam, line):
    """P(X > line) for Poisson(lam)."""
    k = math.floor(line)
    if lam <= 0:
        return 0.0
    cdf, term = 0.0, math.exp(-lam)
    for i in range(0, k + 1):
        if i > 0:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1 - cdf))


def lam_from_over(p_over, line):
    lo, hi = 1e-4, 60.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if pois_over(mid, line) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def parse_am(a):
    if a is None:
        return None
    if isinstance(a, str) and a.strip().upper() in ("EVEN", "EV"):
        return 100.0
    return float(a)


def am_to_prob(a):
    a = parse_am(a)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def am_to_dec(a):
    a = parse_am(a)
    return 1 + (a / 100 if a > 0 else 100 / -a)


def sigma_for(key, mu, sample):
    base = max(CV.get(key, 0.5) * max(mu, 1), 2.5)
    if len(sample) >= 4:
        m = sum(sample) / len(sample)
        sd = math.sqrt(sum((x - m) ** 2 for x in sample) / (len(sample) - 1))
        return math.sqrt(0.5 * sd ** 2 + 0.5 * base ** 2)
    return base


def p_over_from_mu(key, mu, line, sigma):
    if key in COUNT_STATS:
        return pois_over(max(mu, 0.01), line)
    return phi((mu - line) / max(sigma, 0.5))


def shift_line(key, book_line, p_over_book, target_line, sigma):
    """Given a fair P(over book_line), estimate fair P(over target_line)."""
    if abs(book_line - target_line) < 1e-9:
        return p_over_book
    if key in COUNT_STATS:
        lam = lam_from_over(p_over_book, book_line)
        return pois_over(lam, target_line)
    mu = book_line + sigma * phi_inv(p_over_book)
    return phi((mu - target_line) / sigma)


def breakevens(payout_packages):
    """Per-pick probability needed to break even for each Pick6 entry size."""
    out = {}
    for pkg in payout_packages or []:
        n = pkg["pickSetSize"]
        tiers = {t["numberOfPicksCorrect"]: t["guaranteedMultiplier"] for t in pkg["payoutTiers"]}
        lo, hi = 0.3, 0.95
        for _ in range(60):
            p = (lo + hi) / 2
            ev = sum(m * math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k, m in tiers.items())
            if ev < 1:
                lo = p
            else:
                hi = p
        out[n] = {"breakeven": round((lo + hi) / 2, 4), "payouts": {str(k): v for k, v in sorted(tiers.items(), reverse=True)}}
    return out


# --------------------------------------------------------------------------------------
# DraftKings Pick6
# --------------------------------------------------------------------------------------
def pick6_sport(sport):
    root, got = None, None
    for attempt in range(4):  # Pick6 sometimes falls back to another sport's page - retry
        txt = get(f"{P6}/_root.data", params={"sport": sport}, as_text=True)
        try:
            r = turbo_decode(txt)["routes/_homeShared"]["data"] if txt else None
        except Exception as e:
            r = None
            log(f"[pick6] {sport}: decode failed {e}")
        got = (r or {}).get("selectedSportLeague", {}).get("league", {}).get("leagueAbbreviation")
        if r and got == sport:
            root = r
            break
        time.sleep(3 + 3 * attempt)
    if not root:
        log(f"[pick6] {sport}: no active slate (page returned {got})")
        return [], {}
    payouts = breakevens(root.get("entryDetails", {}).get("payoutPackages"))
    groups = root.get("pickGroups") or []
    props = []
    seen = set()
    for g in groups:
        pg = g["pickGroupId"]
        slate = (g.get("startTimeSuffix") or "").strip(" ()") or "Main"
        cats = None
        if pg == int(root.get("pickGroupId") or 0):
            cats = root["pickCardsResponse"].get("orderedPickCategoryIds")
        else:
            t2 = get(f"{P6}/_root.data", params={"sport": sport, "pickGroup": pg}, as_text=True)
            try:
                cats = turbo_decode(t2)["routes/_homeShared"]["data"]["pickCardsResponse"].get("orderedPickCategoryIds")
            except Exception:
                cats = None
        cats = cats or [1]
        for cat in cats:
            d = get(f"{P6}/resources/pickCardsByCategory/{pg}/{cat}")
            if not d:
                continue
            catname = (d.get("pickCategoryById", {}).get(str(cat)) or {}).get("categoryName", "")
            mkts = d.get("pickSixMarketById", {})
            ents = d.get("entityInfoByDkId", {})
            comps = d.get("competitionById", {})
            teams = d.get("displayTeamById", {})
            promos = {int(k): v.get("promotionalPickType") for k, v in (d.get("promotionalPickConfigurationsByPromotionTypeId") or {}).items()}
            for card in d.get("pickCardByPickableId", {}).values():
                pid = card["pickableId"]
                if pid in seen:
                    continue
                seen.add(pid)
                default = None
                ladder = []
                for m in card.get("activePickableMarkets", []):
                    sels = {s["statLinePropositionId"]: s["standingsMultiplier"] for s in m.get("activeSelections", [])}
                    if m["pickableMarketId"] == card.get("defaultPickableMarketId"):
                        default = (m, sels)
                    elif not m.get("isPaused"):
                        ladder.append({"line": m["targetValue"], "more": sels.get(1), "less": sels.get(2)})
                if not default:
                    continue
                m, sels = default
                mk = mkts.get(str(m["pickSixMarketId"]), {})
                names, team, opp, pos, game, start, comp_id = [], "", "", "", "", "", None
                for e in card.get("entities", []):
                    info = ents.get(str(e["dkId"]), {})
                    names.append(info.get("fullName") or info.get("name") or str(e["dkId"]))
                    for cid in e.get("compIds", []):
                        c = comps.get(str(cid))
                        if not c:
                            continue
                        comp_id = cid
                        game, start = c.get("name", ""), c.get("startTime", "")
                        ec = (c.get("entityCompByDkId") or {}).get(str(e["dkId"]), {})
                        tid = ec.get("teamId")
                        pos = ec.get("position", "")
                        team = (teams.get(str(tid)) or {}).get("name", "")
                        other = c.get("awayTeamId") if tid == c.get("homeTeamId") else c.get("homeTeamId")
                        opp = (teams.get(str(other)) or {}).get("name", "")
                img = (ents.get(str(card["entities"][0]["dkId"]), {}).get("imageUrls") or {}).get("160", "") if card.get("entities") else ""
                props.append({
                    "id": pid, "sport": sport, "pickGroupId": pg, "slate": slate, "category": catname,
                    "player": " + ".join(names), "dkIds": [e["dkId"] for e in card.get("entities", [])],
                    "team": team, "opp": opp, "pos": pos, "game": game, "start": start, "compId": comp_id,
                    "stat": mk.get("name", ""), "statKey": full_key(mk.get("name", ""), sport),
                    "line": m["targetValue"], "marketId": m["pickableMarketId"],
                    "moreMult": sels.get(1), "lessMult": sels.get(2),
                    "promo": promos.get(m.get("promoPickTypeId")) if m.get("promoPickTypeId") not in (None, 1) else None,
                    "ladder": sorted(ladder, key=lambda x: x["line"]), "img": img,
                    "url": f"{P6}/?sport={sport}",
                })
    log(f"[pick6] {sport}: {len(props)} props across {len(groups)} slates")
    return props, payouts


def pick6_card(p):
    d = get(f"{P6}/resources/pickableEntityCard/{p['id']}/{p['pickGroupId']}", params={"pickableMarketId": p["marketId"]})
    if not d:
        return
    try:
        stats = (d.get("statistics") or [{}])[0]
        games = []
        for g in stats.get("singleGameStatistics") or []:
            if g.get("isCurrentCompetition"):
                continue
            c = g.get("competition") or {}
            games.append({"opp": c.get("name", ""), "date": (c.get("startTime") or "")[:10], "wk": g.get("weekNumber"),
                          "val": g.get("statValue"), "score": f"{c.get('awayTeamScore', '')}-{c.get('homeTeamScore', '')}"})
        p["log"] = games
        p["seasonAvg"] = stats.get("seasonAverageStatValue")
        p["avgLabel"] = stats.get("averageStatisticDescription", "")
        news = d.get("news") or []
        if news:
            p["news"] = {"summary": news[0].get("summary", ""), "details": (news[0].get("details") or "")[:500], "date": (news[0].get("postedOnDateTime") or "")[:10]}
        tabs = []
        for t in d.get("infoTableTabs") or []:
            rows = [[c.get("itemValue") for c in sorted(r.get("tableRowColumnItems", []), key=lambda x: x.get("sortOrder", 0))]
                    for r in sorted(t.get("tableRows", []), key=lambda x: x.get("sortOrder", 0))]
            tabs.append({"name": t.get("tabName"), "caption": (t.get("tableDetail") or {}).get("detailCaption", ""),
                         "headers": [h.get("columnName") for h in t.get("tableColumnHeaders", [])], "rows": rows})
        p["matchup"] = tabs
        ent = (d.get("pickable", {}).get("pickableEntities") or [{}])[0]
        comp = ((ent.get("pickableCompetitions") or [{}])[0]).get("competitionSummary") or {}
        if comp:
            p["homeTeam"] = f"{comp.get('homeTeam', {}).get('city', '')} {comp.get('homeTeam', {}).get('name', '')}".strip()
            p["awayTeam"] = f"{comp.get('awayTeam', {}).get('city', '')} {comp.get('awayTeam', {}).get('name', '')}".strip()
        st = ((ent.get("pickableCompetitions") or [{}])[0]).get("statuses") or []
        if st:
            p["status"] = ", ".join(s.get("statusName") or s.get("name") or str(s) for s in st if isinstance(s, dict)) or ""
    except Exception as e:
        log(f"[pick6 card] {p['id']}: {e}")


# --------------------------------------------------------------------------------------
# Sportsbooks
# --------------------------------------------------------------------------------------
def add_line(book_lines, book, player, key, line, over, under, sport, fair=None):
    if key is None or line is None or not player:
        return
    e = {"book": book, "line": float(line), "over": over, "under": under}
    if fair is not None:
        e["fair"] = fair
    book_lines.setdefault((norm_name(player), key), []).append(e)


def add_multiway(book_lines, book, rows, key, sport):
    """rows = [(player, american)] for a one-winner market (e.g. first TD scorer): de-vig by the field total."""
    probs = [(pl, am_to_prob(a)) for pl, a in rows if pl and a]
    tot = sum(p for _, p in probs)
    if tot <= 0:
        return 0
    for pl, p in probs:
        add_line(book_lines, book, pl, key, 0.5, None, None, sport, fair=p / max(tot, 1.0))
    return len(probs)


def kambi(sport, book_lines, games):
    path = SPORTS[sport]["kambi"]
    lv = get(f"{KAMBI}/listView/{path}.json", params={"lang": "en_US", "market": "US"})
    if not lv:
        log(f"[kambi] {sport}: blocked/empty")
        return
    evs = [e["event"] for e in lv.get("events", []) if e.get("event", {}).get("state") != "FINISHED"]

    def one(ev):
        return ev, get(f"{KAMBI}/betoffer/event/{ev['id']}.json", params={"lang": "en_US", "market": "US", "includeParticipants": "true"})

    n = 0
    with ThreadPoolExecutor(6) as ex:
        for ev, bo in ex.map(one, evs):
            if not bo:
                continue
            g = games.setdefault(game_key(ev.get("awayName", ""), ev.get("homeName", "")),
                                 {"away": ev.get("awayName"), "home": ev.get("homeName"), "start": ev.get("start"), "sport": sport, "books": {}})
            gl = g["books"].setdefault("BetRivers", {})
            for offer in bo.get("betOffers", []):
                lab = offer.get("criterion", {}).get("label", "")
                outs = offer.get("outcomes", [])
                if "MAIN" in offer.get("tags", []) or offer.get("main"):
                    pass
                if lab in ("Moneyline", "Moneyline - Including Overtime") and len(outs) == 2 and "ml" not in gl:
                    gl["ml"] = {o.get("participant") or o.get("label"): o.get("oddsAmerican") for o in outs}
                if (lab == "Point Spread" or (lab.startswith("Point Spread") and "Including Overtime" in lab)) and "spread" not in gl and len(outs) == 2:
                    gl["spread"] = {o.get("participant") or o.get("label"): [o.get("line", 0) / 1000, o.get("oddsAmerican")] for o in outs}
                if lab.startswith("Total Points") and "Including Overtime" in lab and "total" not in gl and len(outs) == 2:
                    gl["total"] = [outs[0].get("line", 0) / 1000, {o["label"]: o.get("oddsAmerican") for o in outs}]
                sk = special_key(lab) if sport in ("NFL", "CFB") else None
                if sk == "first_td":
                    n += add_multiway(book_lines, "BetRivers", [(o.get("participant"), o.get("oddsAmerican")) for o in outs if o.get("participant")], sk, sport)
                    continue
                if sk in ("td_1h", "td_1q"):
                    for o in outs:
                        if o.get("participant") and o.get("oddsAmerican"):
                            add_line(book_lines, "BetRivers", o["participant"], sk, 0.5, o["oddsAmerican"], None, sport); n += 1
                    continue
                # player props
                is_player = "player" in lab.lower() or "by the" in lab.lower() or offer.get("betOfferType", {}).get("name", "").startswith("Player")
                if not is_player or PARTIAL.search(lab):
                    if not (lab in ("Touchdown Scorer",) or lab.startswith("Player to score a Touchdown - Including")):
                        continue
                key = stat_key(lab, sport)
                if key == "anytime_td":
                    for o in outs:
                        if o.get("participant") and o.get("oddsAmerican") and o.get("label", "").lower() in ("yes", o.get("participant", "").lower()):
                            add_line(book_lines, "BetRivers", o["participant"], key, 0.5, o["oddsAmerican"], None, sport)
                            n += 1
                    continue
                ov = next((o for o in outs if o.get("type") == "OT_OVER" or o.get("label") == "Over"), None)
                un = next((o for o in outs if o.get("type") == "OT_UNDER" or o.get("label") == "Under"), None)
                if ov and ov.get("participant") and ov.get("line") is not None:
                    add_line(book_lines, "BetRivers", ov["participant"], key, ov["line"] / 1000, ov.get("oddsAmerican"),
                             un.get("oddsAmerican") if un else None, sport)
                    n += 1
    log(f"[kambi] {sport}: {len(evs)} events, {n} player lines")


BOV_RE = re.compile(r"^(?P<stat>.+?) - (?P<player>[^()]+?)\s*(\((?P<team>[^)]+)\))?$")


def bovada(sport, book_lines, games):
    path = SPORTS[sport]["bovada"]
    lst = get(f"{BOVADA}/{path}", params={"marketFilterId": "def", "preMatchOnly": "true", "lang": "en"})
    if not lst:
        log(f"[bovada] {sport}: blocked/empty")
        return
    evs = [e for grp in lst for e in grp.get("events", [])]

    def one(e):
        return e, get(f"{BOVADA}{e['link']}", params={"lang": "en"})

    n = 0
    with ThreadPoolExecutor(6) as ex:
        for e, det in ex.map(one, evs):
            try:
                ev = det[0]["events"][0] if det else e
            except Exception:
                ev = e
            comps = ev.get("competitors", [])
            home = next((c["name"] for c in comps if c.get("home")), "")
            away = next((c["name"] for c in comps if not c.get("home")), "")
            g = games.setdefault(game_key(away, home), {"away": away, "home": home,
                                                         "start": datetime.fromtimestamp(ev.get("startTime", 0) / 1000, timezone.utc).isoformat(), "sport": sport, "books": {}})
            gl = g["books"].setdefault("Bovada", {})
            for dg in ev.get("displayGroups", []):
                for m in dg.get("markets", []):
                    per = m.get("period", {})
                    desc = m.get("description", "")
                    outs = m.get("outcomes", [])
                    pdesc = (per.get("description") or "").lower()
                    if desc == "Anytime Touchdown Scorer" and pdesc in ("first half", "1st quarter"):
                        k2 = "td_1h" if pdesc == "first half" else "td_1q"
                        for o in outs:
                            add_line(book_lines, "Bovada", o["description"], k2, 0.5, o["price"].get("american"), None, sport); n += 1
                        continue
                    if desc == "First Touchdown Scorer" and per.get("main"):
                        n += add_multiway(book_lines, "Bovada", [(o["description"], o["price"].get("american")) for o in outs], "first_td", sport)
                        continue
                    if not per.get("main"):
                        continue
                    if dg.get("description") == "Game Lines":
                        if desc == "Moneyline" and "ml" not in gl:
                            gl["ml"] = {o["description"]: o["price"].get("american") for o in outs}
                        elif desc == "Point Spread" and "spread" not in gl:
                            gl["spread"] = {o["description"]: [float(o["price"].get("handicap") or 0), o["price"].get("american")] for o in outs}
                        elif desc == "Total" and "total" not in gl and outs:
                            gl["total"] = [float(outs[0]["price"].get("handicap") or 0), {o["description"]: o["price"].get("american") for o in outs}]
                        continue
                    if "Anytime Touchdown Scorer" in desc:
                        for o in outs:
                            add_line(book_lines, "Bovada", o["description"], "anytime_td", 0.5, o["price"].get("american"), None, sport)
                            n += 1
                        continue
                    if desc.lower().startswith("alternate") or PARTIAL.search(desc):
                        continue
                    mm = BOV_RE.match(desc)
                    if not mm or len(outs) != 2:
                        continue
                    key = stat_key(mm.group("stat"), sport)
                    ov = next((o for o in outs if o.get("type") == "O" or o["description"] == "Over"), None)
                    un = next((o for o in outs if o.get("type") == "U" or o["description"] == "Under"), None)
                    if ov and ov["price"].get("handicap"):
                        add_line(book_lines, "Bovada", mm.group("player"), key, ov["price"]["handicap"], ov["price"].get("american"),
                                 un["price"].get("american") if un else None, sport)
                        n += 1
    log(f"[bovada] {sport}: {len(evs)} events, {n} player lines")


ODDSAPI_MARKETS = {
    "NFL": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_rush_reception_yds,player_pass_tds,player_anytime_td",
    "CFB": "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_pass_tds",
    "NBA": "player_points,player_rebounds,player_assists,player_threes,player_points_rebounds_assists",
}
ODDSAPI_KEYMAP = {"player_pass_yds": "pass_yds", "player_rush_yds": "rush_yds", "player_reception_yds": "rec_yds", "player_receptions": "receptions",
                  "player_rush_reception_yds": "rush_rec_yds", "player_pass_tds": "pass_tds", "player_anytime_td": "anytime_td",
                  "player_points": "points", "player_rebounds": "rebounds", "player_assists": "assists", "player_threes": "threes",
                  "player_points_rebounds_assists": "pra"}


def odds_api(sport, book_lines, games, wanted_games):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return
    sk = SPORTS[sport]["oddsapi"]
    evs = get(f"{ODDS_API}/sports/{sk}/events", params={"apiKey": key}) or []
    max_events = int(os.environ.get("ODDS_API_MAX_EVENTS", "16"))
    evs = [e for e in evs if any(w in (e.get("home_team", "") + e.get("away_team", "")).lower() for w in wanted_games)][:max_events]
    n = 0
    for e in evs:
        d = get(f"{ODDS_API}/sports/{sk}/events/{e['id']}/odds",
                params={"apiKey": key, "regions": "us", "markets": ODDSAPI_MARKETS[sport], "oddsFormat": "american"})
        if not d:
            continue
        for bk in d.get("bookmakers", []):
            bname = bk.get("title", bk.get("key"))
            for m in bk.get("markets", []):
                ck = ODDSAPI_KEYMAP.get(m["key"])
                pairs = {}
                for o in m.get("outcomes", []):
                    pl = o.get("description") or o.get("name")
                    side = o.get("name", "").lower()
                    if ck == "anytime_td":
                        add_line(book_lines, bname, pl, ck, 0.5, o.get("price"), None, sport); n += 1
                        continue
                    pairs.setdefault((pl, o.get("point")), {})[side] = o.get("price")
                for (pl, pt), sides in pairs.items():
                    if pt is not None and "over" in sides:
                        add_line(book_lines, bname, pl, ck, pt, sides.get("over"), sides.get("under"), sport); n += 1
    log(f"[odds-api] {sport}: {len(evs)} events, {n} player lines")


def game_key(away, home):
    return f"{norm_name(away)}@{norm_name(home)}"


# --------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------
def fair_over(entry):
    """No-vig probability of OVER at the book line."""
    if entry.get("fair") is not None:
        return entry["fair"]
    if entry["over"] is None:
        return None
    po = am_to_prob(entry["over"])
    if entry["under"] is not None:
        pu = am_to_prob(entry["under"])
        return po / (po + pu)
    return po / 1.07  # one-sided market (e.g. anytime TD): strip ~7% hold


def analyse(p, book_lines, best_be):
    key = p["statKey"]
    line = p["line"]
    vals = [g["val"] for g in p.get("log", []) if isinstance(g.get("val"), (int, float))]
    season = p.get("seasonAvg")
    l5 = sum(vals) / len(vals) if vals else None
    if season is not None and l5 is not None:
        mu = 0.55 * season + 0.45 * l5
    else:
        mu = season if season is not None else l5
    p["l5Avg"] = round(l5, 2) if l5 is not None else None
    p["hitL5"] = [sum(1 for v in vals if v > line), len(vals)] if vals else None
    p["proj"] = round(mu, 2) if mu is not None else None
    model_key = key or ("_count" if (line or 0) < 10 else "_cont")
    if model_key == "_count":
        COUNT_STATS.add("_count")
    sigma = sigma_for(model_key, mu or line or 1, vals)

    # 1) stats-only probability (shrunk toward 50% because a 5-game sample is small)
    p_stats = None
    longshot = model_key in ("anytime_td", "first_td", "td_1h", "td_1q") or (p.get("moreMult") or 1) >= 3
    if mu is not None:
        p_stats = p_over_from_mu(model_key, max(mu, 0.05), line, sigma)
        if not longshot:
            p_stats = 0.5 + 0.75 * (p_stats - 0.5)
    stats_only_shrink = 0.8  # extra caution when no sportsbook price backs the number

    # 2) market probability: de-vigged sportsbook price, shifted to the Pick6 line
    books = []
    names = [norm_name(n) for n in p["player"].split(" + ")]
    if key and len(names) == 1:
        uniq = {}
        for e in book_lines.get((names[0], key), []):
            fo = fair_over(e)
            if fo is None:
                continue
            at_p6 = shift_line(key, e["line"], fo, line, sigma_for(key, e["line"], vals))
            uniq.setdefault((e["book"], e["line"]), {"book": e["book"], "line": e["line"], "over": e["over"], "under": e["under"],
                                                    "fair": round(fo, 3), "atP6": round(at_p6, 3)})
        books = list(uniq.values())
    p_mkt = sum(b["atP6"] for b in books) / len(books) if books else None

    if p_mkt is not None and p_stats is not None and longshot:
        p_over, basis = 0.9 * p_mkt + 0.1 * p_stats, "market+stats"
    elif p_mkt is not None and p_stats is not None:
        p_over, basis = 0.75 * p_mkt + 0.25 * p_stats, "market+stats"
    elif p_mkt is not None:
        p_over, basis = p_mkt, "market"
    elif p_stats is not None:
        p_over, basis = 0.5 + stats_only_shrink * (p_stats - 0.5), "stats"
    else:
        p_over, basis = None, "none"
    p["pStats"] = round(p_stats, 3) if p_stats is not None else None
    p["pMarket"] = round(p_mkt, 3) if p_mkt is not None else None
    p["basis"] = basis
    p["books"] = books
    if p_over is None:
        p["pMore"] = p["pLess"] = p["pick"] = None
        return
    p_over = min(max(p_over, 0.005), 0.995)
    p["pMore"] = round(p_over, 3)
    p["pLess"] = round(1 - p_over, 3) if p.get("lessMult") else None

    # value vs Pick6 break-even, multiplier aware (1.0 = neutral pick)
    v_more = p_over * (p.get("moreMult") or 1) / best_be - 1 if p.get("moreMult") else None
    v_less = (1 - p_over) * p["lessMult"] / best_be - 1 if p.get("lessMult") else None
    p["boosted"] = bool((p.get("moreMult") or 1) != 1 or (p.get("lessMult") not in (None, 1)))
    if v_less is not None and (v_more is None or v_less > v_more):
        side, p_side, val, mult = "LESS", 1 - p_over, v_less, p["lessMult"]
    else:
        side, p_side, val, mult = "MORE", p_over, v_more if v_more is not None else p_over / best_be - 1, p.get("moreMult") or 1
    p["pick"], p["pPick"], p["value"] = side, round(p_side, 3), round(val, 3)

    # alt ladder (MORE only on Pick6)
    lad = []
    for a in p.get("ladder", []):
        if not a.get("more"):
            continue
        pa = shift_line(model_key, line, p_over, a["line"], sigma)
        if 0.08 <= pa <= 0.95:
            lad.append({"line": a["line"], "mult": a["more"], "p": round(pa, 3), "value": round(pa * a["more"] / best_be - 1, 3)})
    p["ladder"] = [[a["line"], a["mult"], a["p"], a["value"]] for a in lad]
    best_alt = max(lad, key=lambda x: x["value"], default=None)
    p["bestAlt"] = best_alt if best_alt and best_alt["value"] > p["value"] else None

    # where to play: DraftKings only - Pick6 (main + alt line) and the DraftKings sportsbook when The Odds API supplies it
    where = [{"venue": "DK Pick6", "line": line, "price": f"{mult}x", "p": round(p_side, 3), "score": round(val, 3)}]
    if p["bestAlt"]:
        where.append({"venue": "DK Pick6 (alt)", "line": best_alt["line"], "price": f"{best_alt['mult']}x", "p": best_alt["p"], "score": best_alt["value"]})
    for b in books:
        if "draftkings" not in b["book"].lower():  # other books feed the probability but are never a place to play
            continue
        price = b["over"] if side == "MORE" else b["under"]
        if price is None:
            continue
        pb = shift_line(model_key, line, p_over, b["line"], sigma)
        pb = pb if side == "MORE" else 1 - pb
        where.append({"venue": b["book"], "line": b["line"], "price": str(price), "p": round(pb, 3), "score": round(pb * am_to_dec(price) - 1, 3)})
    where.sort(key=lambda w: w["score"], reverse=True)
    p["where"] = where
    p["bestVenue"] = where[0]["venue"]


# --------------------------------------------------------------------------------------
def main():
    t0 = time.time()
    cache = os.environ.get("DEV_CACHE")
    if cache and os.path.exists(cache):
        import pickle
        all_props, payouts_by_sport, book_lines, games = pickle.load(open(cache, "rb"))
        return finish(t0, all_props, payouts_by_sport, book_lines, games)
    all_props, payouts_by_sport = [], {}
    for sport in SPORTS:
        props, payouts = pick6_sport(sport)
        all_props += props
        if payouts:
            payouts_by_sport[sport] = payouts

    log(f"[pick6] fetching {len(all_props)} player cards...")
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(pick6_card, all_props))

    book_lines, games = {}, {}
    for sport in SPORTS:
        if not any(p["sport"] == sport for p in all_props):
            continue
        for fn in (kambi, bovada):
            try:
                fn(sport, book_lines, games)
            except Exception as e:
                log(f"[{fn.__name__}] {sport} failed: {e}")
        try:
            wanted = {w.lower().split()[-1] for p in all_props if p["sport"] == sport
                      for w in (p.get("homeTeam", ""), p.get("awayTeam", "")) if w}
            odds_api(sport, book_lines, games, wanted)
        except Exception as e:
            log(f"[odds-api] {sport} failed: {e}")

    if cache:
        import pickle
        pickle.dump((all_props, payouts_by_sport, book_lines, games), open(cache, "wb"))
    finish(t0, all_props, payouts_by_sport, book_lines, games)


def finish(t0, all_props, payouts_by_sport, book_lines, games):
    pay = next(iter(payouts_by_sport.values()), {})
    best_be = min((v["breakeven"] for v in pay.values()), default=0.55)
    for p in all_props:
        try:
            analyse(p, book_lines, best_be)
        except Exception as e:
            log(f"[model] {p.get('player')} {p.get('stat')}: {e}")

    # match game lines to Pick6 games (by team nickname)
    def find_game(p):
        h, a = (p.get("homeTeam") or "").lower(), (p.get("awayTeam") or "").lower()
        if not h:
            return None
        def same(full, book):
            full, book = norm_name(full), norm_name(book)
            if not full or not book:
                return False
            return full.split()[-1] in book.split() or book in full or full in book
        for gk, g in games.items():
            if g["sport"] == p["sport"] and same(h, g.get("home") or "") and same(a, g.get("away") or ""):
                return gk
        return None

    used = {}
    for p in all_props:
        gk = find_game(p)
        if gk:
            used[gk] = games[gk]
            p["gk"] = gk

    # shrink output: matchup tables / news are shared, store once
    tables, players = {}, {}
    for p in all_props:
        mt = p.pop("matchup", None)
        if mt:
            k = f"{p.get('opp')}|{p.get('statKey') or p.get('stat')}|{mt[0].get('caption')}"
            tables.setdefault(k, mt)
            p["mt"] = k
        pk = str(p["dkIds"][0]) if p.get("dkIds") else p["player"]
        pl = players.setdefault(pk, {})
        if p.get("news"):
            pl["news"] = p.pop("news")
        if p.get("img"):
            pl["img"] = p.pop("img")
        p["pk"] = pk
        for k in ("dkIds", "marketId", "url", "compId"):
            p.pop(k, None)
        p["log"] = [[g["opp"], g["date"], g["val"]] for g in p.get("log", [])]
        p["books"] = [[b["book"], b["line"], b["over"], b["under"], b["atP6"]] for b in p.get("books", [])]
        p["where"] = [[w["venue"], w["line"], w["price"], w["p"], w["score"]] for w in p.get("where", [])]
        for k in ("avgLabel", "homeTeam", "awayTeam", "pickGroupId"):
            p.pop(k, None)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payouts": pay,
        "bestBreakeven": best_be,
        "props": all_props,
        "players": players,
        "tables": tables,
        "games": used,
        "sources": ["DraftKings Pick6"] + sorted({b["book"] for v in book_lines.values() for b in v}),
        "log": LOG[-60:],
        "runtimeSec": round(time.time() - t0, 1),
    }
    os.makedirs("data", exist_ok=True)
    if not all_props and os.path.exists("data/props.json"):
        log("No Pick6 props scraped - keeping previous data file")
        sys.exit(0)
    with open("data/props.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log(f"Wrote {len(all_props)} props, {len(used)} games in {out['runtimeSec']}s")


if __name__ == "__main__":
    main()
