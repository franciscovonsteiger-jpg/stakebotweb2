import os, json, time, logging
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
import requests

log = logging.getLogger("stakebot")

API_KEY       = os.getenv("ODDS_API_KEY", "")
BANKROLL      = float(os.getenv("BANKROLL_USD", 1000))
KELLY_FRAC    = float(os.getenv("KELLY_FRACTION", 0.5))
MAX_STAKE_PCT = float(os.getenv("MAX_STAKE_PCT", 0.05))
MIN_EDGE      = float(os.getenv("MIN_EDGE_PCT", 0.05))
BASE_URL      = "https://api.the-odds-api.com/v4"

SPORTS_ACTIVE = {
    "soccer_epl":                {"nombre": "Premier League",     "deporte": "Fútbol"},
    "soccer_spain_la_liga":      {"nombre": "La Liga",            "deporte": "Fútbol"},
    "soccer_germany_bundesliga": {"nombre": "Bundesliga",         "deporte": "Fútbol"},
    "soccer_italy_serie_a":      {"nombre": "Serie A",            "deporte": "Fútbol"},
    "soccer_uefa_champs_league": {"nombre": "Champions League",   "deporte": "Fútbol"},
    "tennis_atp_french_open":    {"nombre": "ATP Roland Garros",  "deporte": "Tenis"},
    "tennis_wta_french_open":    {"nombre": "WTA Roland Garros",  "deporte": "Tenis"},
    "basketball_nba":            {"nombre": "NBA",                "deporte": "Básquet"},
    "baseball_mlb":              {"nombre": "MLB",                "deporte": "Béisbol"},
    "mma_mixed_martial_arts":    {"nombre": "MMA/UFC",            "deporte": "MMA"},
    "esports_lol":               {"nombre": "LoL LCK/LEC",        "deporte": "Esports"},
    "esports_csgo":              {"nombre": "CS2 Pro League",     "deporte": "Esports"},
}

CONTEXT_RULES = [
    {
        "id": "champion_early",
        "descripcion": "Campeón anticipado — sin motivación en liga",
        "penalizacion": 0.22, "descartar": True,
        "equipos": ["Bayern Munich", "FC Bayern", "Bayern München"],
    },
    {
        "id": "relegated",
        "descripcion": "Equipo ya descendido matemáticamente",
        "penalizacion": 0.20, "descartar": True,
        "equipos": [],
    },
    {
        "id": "nothing_to_play",
        "descripcion": "Sin objetivos esta temporada",
        "penalizacion": 0.10, "descartar": True,
        "equipos": [],
    },
    {
        "id": "esport",
        "descripcion": "Esport — Kelly reducido por mercado menos eficiente",
        "penalizacion": 0.0, "descartar": False,
        "kelly_override": 0.25, "max_stake_override": 0.02,
        "deportes": ["Esports"],
    },
]

@dataclass
class Pick:
    id:            str
    evento:        str
    deporte:       str
    liga:          str
    equipo_pick:   str
    odds_stake:    float
    prob_ajustada: float
    edge:          float
    stake_usd:     float
    ganancia_pot:  float
    contexto_id:   str
    contexto_desc: str
    descartado:    bool
    razon_descarte: Optional[str]
    commence_time:  Optional[str]
    hora_local:     Optional[str]

def detect_context(equipo: str, deporte: str) -> dict:
    for rule in CONTEXT_RULES:
        if "deportes" in rule and deporte in rule["deportes"]:
            return rule
        for eq in rule.get("equipos", []):
            if eq.lower() in equipo.lower():
                return rule
    return {"id": "clean", "descripcion": "Sin alertas", "penalizacion": 0.0, "descartar": False}

def consensus_prob(bookmakers: list, team: str) -> Optional[float]:
    probs = []
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            outcomes = mkt["outcomes"]
            odds_t = next((o["price"] for o in outcomes if o["name"] == team), None)
            if not odds_t:
                continue
            total = sum(1/o["price"] for o in outcomes)
            probs.append((1/odds_t) / total)
    if not probs:
        return None
    probs.sort()
    n = len(probs)
    return probs[n//2] if n % 2 else (probs[n//2-1] + probs[n//2]) / 2

def kelly_stake(prob, odds, bankroll=BANKROLL, kf=KELLY_FRAC, maxp=MAX_STAKE_PCT):
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    capped = min(k * kf, maxp)
    return round(bankroll * capped, 2), capped

def analizar_evento(ev: dict, meta: dict, consensus: list) -> list[Pick]:
    picks = []
    home, away = ev["home_team"], ev["away_team"]
    commence   = ev.get("commence_time", "")
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        hora_local = dt.strftime("%d/%m %H:%M")
    except Exception:
        hora_local = commence[:16] if commence else ""

    stake_bm = next((b for b in ev.get("bookmakers", []) if b["key"] == "stake"), None)
    if not stake_bm:
        return []

    for mkt in stake_bm.get("markets", []):
        if mkt["key"] != "h2h":
            continue
        for out in mkt["outcomes"]:
            team, odds_s = out["name"], out["price"]
            if odds_s <= 1.0:
                continue
            prob_base = consensus_prob(consensus, team) or (1/odds_s)
            ctx       = detect_context(team, meta["deporte"])
            prob_aj   = max(0.01, prob_base - ctx.get("penalizacion", 0))
            edge      = prob_aj * odds_s - 1
            kf        = ctx.get("kelly_override", KELLY_FRAC)
            msp       = ctx.get("max_stake_override", MAX_STAKE_PCT)
            stake_usd, _ = kelly_stake(prob_aj, odds_s, BANKROLL, kf, msp)
            descartado   = ctx.get("descartar", False) or edge < MIN_EDGE
            razon = ctx["descripcion"] if ctx.get("descartar") else (
                f"Edge {edge*100:.1f}% bajo mínimo {MIN_EDGE*100:.0f}%" if descartado else None
            )
            picks.append(Pick(
                id            = f"{home}-{away}-{team}".replace(" ", "_"),
                evento        = f"{home} vs {away}",
                deporte       = meta["deporte"],
                liga          = meta["nombre"],
                equipo_pick   = team,
                odds_stake    = odds_s,
                prob_ajustada = round(prob_aj, 4),
                edge          = round(edge, 4),
                stake_usd     = stake_usd,
                ganancia_pot  = round(stake_usd * (odds_s - 1), 2),
                contexto_id   = ctx["id"],
                contexto_desc = ctx["descripcion"],
                descartado    = descartado,
                razon_descarte= razon,
                commence_time = commence,
                hora_local    = hora_local,
            ))
    return picks

def escanear_mercado() -> dict:
    if not API_KEY:
        raise ValueError("Sin API key configurada")
    validos, descartados, total = [], [], 0
    for sport_key, meta in SPORTS_ACTIVE.items():
        try:
            r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey": API_KEY, "regions": "eu,uk,us,au",
                "markets": "h2h", "oddsFormat": "decimal",
            }, timeout=15)
            if r.status_code == 422:
                continue
            r.raise_for_status()
            eventos = r.json()
            if not eventos:
                continue
            r2 = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey": API_KEY, "regions": "eu,uk,us,au",
                "markets": "h2h", "oddsFormat": "decimal",
            }, timeout=15)
            consensus = r2.json() if r2.ok else eventos
            for ev in eventos:
                total += 1
                for p in analizar_evento(ev, meta, consensus):
                    (descartados if p.descartado else validos).append(p)
            time.sleep(0.4)
        except Exception as e:
            log.warning(f"{sport_key}: {e}")
    validos.sort(key=lambda p: p.edge, reverse=True)
    return {
        "timestamp":         datetime.now().isoformat(),
        "total_eventos":     total,
        "picks_validos":     [asdict(p) for p in validos],
        "picks_descartados": [asdict(p) for p in descartados],
        "bankroll":          BANKROLL,
        "requests_remaining": None,
    }
