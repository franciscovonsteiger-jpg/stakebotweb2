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

def consensus_prob(bookmakers: list, team: str) -> Optional[float]:
    """
    Probabilidad de mercado real: mediana de todas las casas EXCEPTO Stake.
    Esto nos da el 'precio justo' para detectar si Stake está pagando de más.
    """
    probs = []
    for bm in bookmakers:
        # Excluimos Stake del consensus — queremos comparar contra él
        if bm.get("key") == "stake":
            continue
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

def kelly_stake(prob, odds, bankroll=None, kf=None, maxp=None):
    if bankroll is None: bankroll = BANKROLL
    if kf is None: kf = KELLY_FRAC
    if maxp is None: maxp = MAX_STAKE_PCT
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    capped = min(k * kf, maxp)
    return round(bankroll * capped, 2), capped

def analizar_evento(ev: dict, meta: dict, all_bookmakers_data: list) -> list[Pick]:
    """
    Compara las odds de Stake contra el consensus de todas las demás casas.
    Si Stake paga MÁS que el mercado → hay value → pick válido.
    """
    picks = []
    home, away = ev["home_team"], ev["away_team"]
    commence   = ev.get("commence_time", "")

    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        hora_local = dt.strftime("%d/%m %H:%M")
    except Exception:
        hora_local = commence[:16] if commence else ""

    # Odds de Stake específicamente
    stake_bm = next((b for b in ev.get("bookmakers", []) if b["key"] == "stake"), None)
    if not stake_bm:
        return []

    # Buscamos el evento equivalente en los datos de todas las casas
    ev_all = next(
        (e for e in all_bookmakers_data if e["id"] == ev["id"]),
        None
    )
    all_bms = ev_all["bookmakers"] if ev_all else []

    for mkt in stake_bm.get("markets", []):
        if mkt["key"] != "h2h":
            continue
        for out in mkt["outcomes"]:
            team    = out["name"]
            odds_s  = out["price"]
            if odds_s <= 1.0:
                continue

            # Probabilidad real del mercado (sin Stake)
            prob_consensus = consensus_prob(all_bms, team)

            # Si no hay consensus (pocas casas), usamos la prob implícita de Stake
            # como base pero con descuento de vig estándar del 5%
            if prob_consensus is None:
                total_impl = sum(1/o["price"] for o in mkt["outcomes"])
                prob_consensus = (1/odds_s) / total_impl * 0.95

            # Filtro de contexto motivacional
            ctx      = detect_context(team, meta["deporte"])
            penali   = ctx.get("penalizacion", 0.0)
            prob_aj  = max(0.01, prob_consensus - penali)

            # Edge = cuánto paga Stake vs probabilidad real
            edge = prob_aj * odds_s - 1

            kf  = ctx.get("kelly_override", KELLY_FRAC)
            msp = ctx.get("max_stake_override", MAX_STAKE_PCT)
            stake_usd, _ = kelly_stake(prob_aj, odds_s, BANKROLL, kf, msp)

            descartado = ctx.get("descartar", False) or edge < MIN_EDGE
            razon = None
            if ctx.get("descartar"):
                razon = ctx["descripcion"]
            elif edge < MIN_EDGE:
                razon = f"Edge {edge*100:.1f}% bajo mínimo {MIN_EDGE*100:.0f}%"

            picks.append(Pick(
                id            = f"{ev['id']}-{team}".replace(" ","_"),
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
            # 1. Odds de Stake solamente
            r_stake = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey":     API_KEY,
                "regions":    "eu",
                "markets":    "h2h",
                "oddsFormat": "decimal",
                "bookmakers": "stake",
            }, timeout=15)

            if r_stake.status_code == 422:
                continue
            r_stake.raise_for_status()
            eventos_stake = r_stake.json()
            if not eventos_stake:
                continue

            # 2. Odds de TODAS las casas para consensus (comparación real)
            r_all = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey":     API_KEY,
                "regions":    "eu,uk,us,au",
                "markets":    "h2h",
                "oddsFormat": "decimal",
            }, timeout=15)

            all_data = r_all.json() if r_all.ok else []

            remaining = r_stake.headers.get("x-requests-remaining", "?")
            log.info(f"{meta['nombre']}: {len(eventos_stake)} eventos | requests restantes: {remaining}")

            for ev in eventos_stake:
                total += 1
                for p in analizar_evento(ev, meta, all_data):
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
