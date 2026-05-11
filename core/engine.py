import os, time, logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import requests

log = logging.getLogger("stakebot")

API_KEY        = os.getenv("ODDS_API_KEY", "")
BANKROLL       = float(os.getenv("BANKROLL_USD", 1000))
KELLY_FRAC     = float(os.getenv("KELLY_FRACTION", 0.5))
MAX_STAKE_PCT  = float(os.getenv("MAX_STAKE_PCT", 0.05))
MIN_EDGE       = float(os.getenv("MIN_EDGE_PCT", 0.03))
MAX_GOLD_TIPS  = int(os.getenv("MAX_GOLD_TIPS", 5))
# Horas hacia adelante que queremos ver — default 36hs
VENTANA_HORAS  = int(os.getenv("VENTANA_HORAS", 36))
BASE_URL       = "https://api.the-odds-api.com/v4"

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
    roi_esperado:   float
    gold_score:     float
    stake_usd:      float
    ganancia_pot:   float
    roi_diario_pct: float
    es_gold:        bool
    contexto_id:    str
    contexto_desc:  str
    descartado:     bool
    razon_descarte: Optional[str]
    commence_time:  Optional[str]
    hora_local:     Optional[str]
    horas_para_inicio: float  # cuántas horas faltan para el partido

def evento_en_ventana(commence: str, ventana_horas: int) -> tuple[bool, float]:
    """
    Verifica si el evento empieza dentro de la ventana de tiempo.
    Retorna (esta_en_ventana, horas_para_inicio).
    """
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        ahora = datetime.now(timezone.utc)
        diff  = dt - ahora
        horas = diff.total_seconds() / 3600

        # Solo eventos que empiezan en las próximas VENTANA_HORAS horas
        # y que no hayan empezado ya (horas > -2 para dar margen a partidos en curso)
        en_ventana = -2 <= horas <= ventana_horas
        return en_ventana, round(horas, 1)
    except Exception:
        return True, 0.0  # si no podemos parsear, lo incluimos

def format_hora_local(commence: str, horas: float) -> str:
    """Formatea la hora con indicador de cuándo empieza."""
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        # Convertir a GMT-3 (Argentina)
        dt_arg = dt.astimezone(timezone(timedelta(hours=-3)))
        fecha  = dt_arg.strftime("%d/%m")
        hora   = dt_arg.strftime("%H:%M")

        if horas < 0:
            cuando = "En curso"
        elif horas < 1:
            cuando = f"En {int(horas*60)}min"
        elif horas < 24:
            cuando = f"En {int(horas)}h"
        else:
            cuando = f"En {int(horas/24)}d {int(horas%24)}h"

        return f"{fecha} {hora} · {cuando}"
    except Exception:
        return commence[:16] if commence else ""

def detect_context(equipo: str, deporte: str) -> dict:
    for rule in CONTEXT_RULES:
        if "deportes" in rule and deporte in rule["deportes"]:
            return rule
        for eq in rule.get("equipos", []):
            if eq.lower() in equipo.lower():
                return rule
    return {"id":"clean","descripcion":"Sin alertas","penalizacion":0.0,"descartar":False}

def pinnacle_prob(bookmakers: list, team: str) -> Optional[float]:
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

def calc_gold_score(prob: float, odds: float, edge: float, horas: float) -> float:
    """
    Score para rankear Gold Tips.
    Maximiza valor esperado y prioriza partidos más cercanos (más información disponible).
    """
    ev    = prob * (odds - 1)
    score = ev * edge

    # Bonus zona de cuota ideal
    if 1.60 <= odds <= 2.50:
        score *= 1.2
    elif odds > 4.0:
        score *= 0.6
    elif odds < 1.30:
        score *= 0.7

    # Bonus por proximidad — partidos en las próximas 6hs tienen mejores odds
    if 0 <= horas <= 6:
        score *= 1.15
    elif horas <= 12:
        score *= 1.05

    return round(score, 6)

def analizar_evento(ev: dict, meta: dict, ventana_horas: int) -> list[Pick]:
    picks      = []
    home, away = ev["home_team"], ev["away_team"]
    commence   = ev.get("commence_time", "")
    bookmakers = ev.get("bookmakers", [])

    if not bookmakers:
        return []

    # Filtro de ventana temporal — clave para no mostrar partidos lejanos
    en_ventana, horas = evento_en_ventana(commence, ventana_horas)
    if not en_ventana:
        return []

    hora_local = format_hora_local(commence, horas)

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
        edge    = prob_aj * mejor_odds - 1

        kf  = ctx.get("kelly_override", KELLY_FRAC)
        msp = ctx.get("max_stake_override", MAX_STAKE_PCT)
        stake_usd, _ = kelly_stake(prob_aj, mejor_odds, BANKROLL, kf, msp)

        roi_esp        = round(prob_aj * (mejor_odds - 1) * edge, 6)
        gscore         = calc_gold_score(prob_aj, mejor_odds, edge, horas)
        ganancia_pot   = round(stake_usd * (mejor_odds - 1), 2)
        roi_diario_pct = round(ganancia_pot / BANKROLL * 100, 2)

        descartado = ctx.get("descartar", False) or edge < MIN_EDGE
        razon = None
        if ctx.get("descartar"):
            razon = ctx["descripcion"]
        elif edge < MIN_EDGE:
            razon = f"Edge {edge*100:.1f}% bajo mínimo {MIN_EDGE*100:.0f}%"

        picks.append(Pick(
            id             = f"{ev['id']}-{team}".replace(" ", "_"),
            evento         = f"{home} vs {away}",
            deporte        = meta["deporte"],
            liga           = meta["nombre"],
            equipo_pick    = team,
            odds_stake     = mejor_odds,
            prob_ajustada  = round(prob_aj, 4),
            edge           = round(edge, 4),
            roi_esperado   = roi_esp,
            gold_score     = gscore,
            stake_usd      = stake_usd,
            ganancia_pot   = ganancia_pot,
            roi_diario_pct = roi_diario_pct,
            es_gold        = False,
            contexto_id    = ctx["id"],
            contexto_desc  = ctx["descripcion"],
            descartado     = descartado,
            razon_descarte = razon,
            commence_time  = commence,
            hora_local     = hora_local,
            horas_para_inicio = horas,
        ))

    return picks

def escanear_mercado() -> dict:
    if not API_KEY:
        raise ValueError("Sin API key configurada")

    validos, descartados, total, fuera_ventana = [], [], 0, 0
    ahora_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M") + " UTC"

    for sport_key, meta in SPORTS_ACTIVE.items():
        try:
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
            log.info(f"{meta['nombre']}: {len(eventos)} eventos | requests: {remaining}")

            for ev in eventos:
                total += 1
                picks_ev = analizar_evento(ev, meta, VENTANA_HORAS)
                if not picks_ev:
                    # Puede ser porque está fuera de ventana
                    en_v, _ = evento_en_ventana(ev.get("commence_time",""), VENTANA_HORAS)
                    if not en_v:
                        fuera_ventana += 1
                for p in picks_ev:
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

    # Ordenar por gold_score
    validos.sort(key=lambda p: p.gold_score, reverse=True)

    # Gold Tips — top N con filtros de calidad
    candidatos = [
        p for p in validos
        if p.edge >= 0.05
        and 1.40 <= p.odds_stake <= 3.50
        and p.horas_para_inicio >= 0  # no empezados
    ]
    for i, p in enumerate(candidatos):
        if i < MAX_GOLD_TIPS:
            p.es_gold = True

    gold_picks      = [p for p in validos if p.es_gold]
    roi_total_pct   = round(sum(p.ganancia_pot for p in gold_picks) / BANKROLL * 100, 2) if BANKROLL else 0
    expo_total_gold = round(sum(p.stake_usd for p in gold_picks), 2)

    return {
        "timestamp":           datetime.now().isoformat(),
        "ahora_utc":           ahora_str,
        "ventana_horas":       VENTANA_HORAS,
        "total_eventos":       total,
        "eventos_fuera_ventana": fuera_ventana,
        "picks_validos":       [asdict(p) for p in validos],
        "picks_descartados":   [asdict(p) for p in descartados],
        "gold_tips":           [asdict(p) for p in gold_picks],
        "roi_gold_potencial":  roi_total_pct,
        "expo_gold_usd":       expo_total_gold,
        "bankroll":            BANKROLL,
    }
