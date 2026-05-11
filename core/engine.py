import os, time, logging
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
import requests

log = logging.getLogger("stakebot")

API_KEY       = os.getenv("ODDS_API_KEY", "")
BANKROLL      = float(os.getenv("BANKROLL_USD", 1000))
KELLY_FRAC    = float(os.getenv("KELLY_FRACTION", 0.5))
MAX_STAKE_PCT = float(os.getenv("MAX_STAKE_PCT", 0.05))
MIN_EDGE      = float(os.getenv("MIN_EDGE_PCT", 0.03))
BASE_URL      = "https://api.the-odds-api.com/v4"

SPORTS_ACTIVE = {
    "soccer_epl":                {"nombre": "Premier League",    "deporte": "Fútbol"},
    "soccer_spain_la_liga":      {"nombre": "La Liga",           "deporte": "Fútbol"},
    "soccer_germany_bundesliga": {"nombre": "Bundesliga",        "deporte": "Fútbol"},
    "soccer_italy_serie_a":      {"nombre": "Serie A",           "deporte": "Fútbol"},
    "soccer_uefa_champs_league": {"nombre": "Champions League",  "deporte": "Fútbol"},
    "tennis_atp_french_open":    {"nombre": "ATP Roland Garros", "deporte": "Tenis"},
    "tennis_wta_french_open":    {"nombre": "WTA Roland Garros", "deporte": "Tenis"},
    "basketball_nba":            {"nombre": "NBA",               "deporte": "Básquet"},
    "baseball_mlb":              {"nombre": "MLB",               "deporte": "Béisbol"},
    "mma_mixed_martial_arts":    {"nombre": "MMA/UFC",           "deporte": "MMA"},
    "esports_lol":               {"nombre": "LoL LCK/LEC",       "deporte": "Esports"},
    "esports_csgo":              {"nombre": "CS2 Pro League",    "deporte": "Esports"},
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
        "id": "esport",
        "descripcion": "Esport — Kelly reducido",
        "penalizacion": 0.0, "descartar": False,
        "kelly_override": 0.25, "max_stake_override": 0.02,
        "deportes": ["Esports"],
    },
]

@dataclass
class Pick:
    id:             str
    evento:         str
    deporte:        str
    liga:           str
    equipo_pick:    str
    odds_stake:     float
    prob_ajustada:  float
    edge:           float
    stake_usd:      float
    ganancia_pot:   float
    contexto_id:    str
    contexto_desc:  str
    descartado:     bool
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
    return {"id":"clean","descripcion":"Sin alertas","penalizacion":0.0,"descartar":False}

def pinnacle_prob(bookmakers: list, team: str) -> Optional[float]:
    """
    Usa Pinnacle como referencia principal de precio justo.
    Pinnacle tiene el margen más bajo del mercado (~2%) — es el benchmark.
    Si no está, usa la mediana de todas las casas disponibles.
    """
    # Intentamos Pinnacle primero
    pinnacle = next((b for b in bookmakers if b.get("key") == "pinnacle"), None)
    if pinnacle:
        for mkt in pinnacle.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            outcomes = mkt["outcomes"]
            odds_t = next((o["price"] for o in outcomes if o["name"] == team), None)
            if odds_t and odds_t > 1.0:
                total_impl = sum(1/o["price"] for o in outcomes)
                return (1/odds_t) / total_impl

    # Fallback: mediana de todas las casas
    probs = []
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            outcomes = mkt["outcomes"]
            odds_t = next((o["price"] for o in outcomes if o["name"] == team), None)
            if not odds_t or odds_t <= 1.0:
                continue
            total_impl = sum(1/o["price"] for o in outcomes)
            probs.append((1/odds_t) / total_impl)

    if not probs:
        return None
    probs.sort()
    n = len(probs)
    return probs[n//2] if n % 2 else (probs[n//2-1] + probs[n//2]) / 2

def mejor_cuota_mercado(bookmakers: list, team: str) -> Optional[float]:
    """La mejor cuota disponible en el mercado para este equipo."""
    best = None
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == team), None)
            if odds_t and (best is None or odds_t > best):
                best = odds_t
    return best

def kelly_stake(prob, odds, bankroll=None, kf=None, maxp=None):
    if bankroll is None: bankroll = BANKROLL
    if kf is None: kf = KELLY_FRAC
    if maxp is None: maxp = MAX_STAKE_PCT
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    capped = min(k * kf, maxp)
    return round(bankroll * capped, 2), capped

def analizar_evento(ev: dict, meta: dict) -> list[Pick]:
    """
    Estrategia: usamos Pinnacle (o consensus) como precio justo.
    Detectamos si HAY value en el mercado.
    El usuario busca esa cuota en Stake y la compara — si Stake paga igual
    o más, el pick tiene valor. Si Stake paga menos, no apostar.
    """
    picks = []
    home, away = ev["home_team"], ev["away_team"]
    commence   = ev.get("commence_time", "")
    bookmakers = ev.get("bookmakers", [])

    if not bookmakers:
        return []

    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        hora_local = dt.strftime("%d/%m %H:%M")
    except Exception:
        hora_local = commence[:16] if commence else ""

    # Tomamos los outcomes del primer bookmaker disponible para iterar equipos
    equipos = set()
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] == "h2h":
                for out in mkt["outcomes"]:
                    equipos.add(out["name"])

    for team in equipos:
        prob_justa = pinnacle_prob(bookmakers, team)
        if prob_justa is None:
            continue

        mejor_odds = mejor_cuota_mercado(bookmakers, team)
        if not mejor_odds or mejor_odds <= 1.0:
            continue

        ctx     = detect_context(team, meta["deporte"])
        penali  = ctx.get("penalizacion", 0.0)
        prob_aj = max(0.01, prob_justa - penali)

        # Edge usando la mejor cuota disponible en el mercado
        edge = prob_aj * mejor_odds - 1

        kf  = ctx.get("kelly_override", KELLY_FRAC)
        msp = ctx.get("max_stake_override", MAX_STAKE_PCT)
        stake_usd, _ = kelly_stake(prob_aj, mejor_odds, BANKROLL, kf, msp)

        descartado = ctx.get("descartar", False) or edge < MIN_EDGE
        razon = None
        if ctx.get("descartar"):
            razon = ctx["descripcion"]
        elif edge < MIN_EDGE:
            razon = f"Edge {edge*100:.1f}% bajo mínimo {MIN_EDGE*100:.0f}%"

        picks.append(Pick(
            id            = f"{ev['id']}-{team}".replace(" ", "_"),
            evento        = f"{home} vs {away}",
            deporte       = meta["deporte"],
            liga          = meta["nombre"],
            equipo_pick   = team,
            odds_stake    = mejor_odds,
            prob_ajustada = round(prob_aj, 4),
            edge          = round(edge, 4),
            stake_usd     = stake_usd,
            ganancia_pot  = round(stake_usd * (mejor_odds - 1), 2),
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
            # Una sola llamada — todas las casas disponibles
            r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey":     API_KEY,
                "regions":    "eu,uk,us,au",
                "markets":    "h2h",
                "oddsFormat": "decimal",
            }, timeout=20)

            if r.status_code == 422:
                continue
            r.raise_for_status()

            eventos = r.json()
            if not eventos:
                continue

            remaining = r.headers.get("x-requests-remaining", "?")
            log.info(f"{meta['nombre']}: {len(eventos)} eventos | requests restantes: {remaining}")

            for ev in eventos:
                total += 1
                for p in analizar_evento(ev, meta):
                    (descartados if p.descartado else validos).append(p)

            time.sleep(0.3)

        except requests.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code == 401:
                log.error("API key inválida")
                break
            log.warning(f"{sport_key} HTTP {code}")
        except Exception as e:
            log.warning(f"{sport_key}: {e}")

    validos.sort(key=lambda p: p.edge, reverse=True)

    return {
        "timestamp":         datetime.now().isoformat(),
        "total_eventos":     total,
        "picks_validos":     [asdict(p) for p in validos],
        "picks_descartados": [asdict(p) for p in descartados],
        "bankroll":          BANKROLL,
    }
