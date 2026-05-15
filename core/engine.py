import os, time, logging, re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import requests

log = logging.getLogger("stakebot")

# Importar contexto si está disponible
try:
    from core.context import enriquecer_evento, ajustar_prob_con_contexto
    CONTEXTO_DISPONIBLE = True
except:
    CONTEXTO_DISPONIBLE = False
    def enriquecer_evento(home, away, deporte, liga): return {}
    def ajustar_prob_con_contexto(p, ctx): return p, []

# ── Configuración ──────────────────────────────────────────────────────────────
API_KEY        = os.getenv("ODDS_API_KEY", "")
ODDSPAPI_KEY    = os.getenv("ODDSPAPI_KEY", "")      # Nueva API con Pinnacle real
PANDASCORE_KEY  = os.getenv("PANDASCORE_KEY", "")   # Esports stats y contexto
BANKROLL       = float(os.getenv("BANKROLL_USD", 1000))
KELLY_FRAC     = float(os.getenv("KELLY_FRACTION", 0.25))   # 1/4 Kelly — más conservador
MAX_STAKE_PCT  = float(os.getenv("MAX_STAKE_PCT", 0.03))    # Cap 3% bankroll
MIN_EDGE       = float(os.getenv("MIN_EDGE_PCT", 0.10))     # Edge mínimo 10% — solo picks con convicción real
MIN_EDGE_VIVO  = 0.10                                        # En vivo más estricto
MAX_GOLD_TIPS  = int(os.getenv("MAX_GOLD_TIPS", 8))
VENTANA_HORAS  = int(os.getenv("VENTANA_HORAS", 48))
MIN_SURE_PROB  = float(os.getenv("MIN_SURE_PROB", 0.80))    # 80% prob mínima — alta confianza pero NO infalible (1 de 5 puede fallar). Stake siempre vía Kelly.
TZ_OFFSET      = -3  # Argentina/LATAM (UTC-3). Hardcodeado para evitar errores de config en Railway.
BASE_URL       = "https://api.the-odds-api.com/v4"
ODDSPAPI_URL   = "https://api.oddspapi.io/v4"  # Dominio correcto (.io, no .com) y versión v4

# ── OddsPapi: estado interno para rate-limit awareness ────────────────────────
# Contador de requests consumidos en el mes actual (se reinicia el 1 de cada mes).
# Plan free = 250 req/mes. Visible en logs para evitar agotar quota silenciosamente.
_oddspapi_state = {
    "requests_mes": 0,
    "mes_actual": datetime.now().month,
    "ultimo_error": None,
    "cache": {},  # key: f"{tournamentId}", value: {"ts": timestamp, "data": [...]}
}
ODDSPAPI_CACHE_TTL = 3600  # 60 min — datos frescos pero ahorra requests

# ── Mercados por deporte ───────────────────────────────────────────────────────
# Béisbol: solo h2h por ahora (más predecible sin Pinnacle)
# Con OddsPapi habilitamos totals y spreads en béisbol también
MARKETS_BY_SPORT = {
    "soccer":     ["h2h", "btts", "totals"],
    "tennis":     ["h2h", "spreads", "totals"],  # The Odds API soporta solo estos para tenis
    "basketball": ["h2h", "totals"],
    "mma":        ["h2h"],
    "baseball":   ["h2h"],          # Solo h2h hasta tener Pinnacle confirmado
    "hockey":     ["h2h"],
    "esports":    ["h2h", "spreads", "totals"],
}

SPORTS_ACTIVE = {
    # ── Fútbol Europa ───────────────────────────────────────────────────────────
    "soccer_epl":                        {"nombre": "Premier League",       "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_spain_la_liga":              {"nombre": "La Liga",              "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_germany_bundesliga":         {"nombre": "Bundesliga",           "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_italy_serie_a":              {"nombre": "Serie A",              "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_uefa_champs_league":         {"nombre": "Champions League",     "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_uefa_europa_league":         {"nombre": "Europa League",        "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_uefa_europa_conference_league": {"nombre": "Conference League", "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_france_ligue_one":           {"nombre": "Ligue 1",              "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_netherlands_eredivisie":     {"nombre": "Eredivisie",           "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_portugal_primeira_liga":     {"nombre": "Primeira Liga",        "deporte": "Fútbol",  "tipo": "soccer"},
    # ── Fútbol Sudamérica ───────────────────────────────────────────────────────
    "soccer_conmebol_copa_libertadores": {"nombre": "Copa Libertadores",    "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_conmebol_copa_sudamericana": {"nombre": "Copa Sudamericana",    "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_argentina_primera_division": {"nombre": "Liga Argentina",       "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_brazil_campeonato":          {"nombre": "Brasileirão",          "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_brazil_serie_b":             {"nombre": "Brasileirão B",        "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_chile_campeonato":           {"nombre": "Primera Chile",        "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_mexico_ligamx":              {"nombre": "Liga MX",              "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_usa_mls":                    {"nombre": "MLS",                  "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_fifa_world_cup":             {"nombre": "FIFA World Cup 2026",  "deporte": "Fútbol",  "tipo": "soccer"},
    # ── Tenis ───────────────────────────────────────────────────────────────────
    # ── Tenis — mercados alternativos (no h2h puro) ────────────────────────────
    "tennis_atp_italian_open":           {"nombre": "ATP Roma",             "deporte": "Tenis",   "tipo": "tennis"},
    "tennis_wta_italian_open":           {"nombre": "WTA Roma",             "deporte": "Tenis",   "tipo": "tennis"},
    "tennis_atp_french_open":            {"nombre": "ATP Roland Garros",    "deporte": "Tenis",   "tipo": "tennis"},
    "tennis_wta_french_open":            {"nombre": "WTA Roland Garros",    "deporte": "Tenis",   "tipo": "tennis"},
    # ── Básquet ─────────────────────────────────────────────────────────────────
    "basketball_nba":                    {"nombre": "NBA",                  "deporte": "Básquet", "tipo": "basketball"},
    "basketball_euroleague":             {"nombre": "Euroleague",           "deporte": "Básquet", "tipo": "basketball"},
    # ── Béisbol ─────────────────────────────────────────────────────────────────
    "baseball_mlb":                      {"nombre": "MLB",                  "deporte": "Béisbol", "tipo": "baseball"},
    "baseball_kbo":                      {"nombre": "KBO (Corea)",          "deporte": "Béisbol", "tipo": "baseball"},
    # ── Hockey ──────────────────────────────────────────────────────────────────
    "icehockey_nhl":                     {"nombre": "NHL",                  "deporte": "Hockey",  "tipo": "hockey"},
    # ── MMA ─────────────────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":            {"nombre": "MMA/UFC",              "deporte": "MMA",     "tipo": "mma"},
    # ── Esports — nicho con mayor ineficiencia de mercado ──────────────────────
    # CS2: mercados h2h + map handicap + total maps
    "esports_cs2":                       {"nombre": "CS2",                  "deporte": "Esports", "tipo": "esports"},
    "esports_csgo":                      {"nombre": "CS2 Pro League",       "deporte": "Esports", "tipo": "esports"},
    # LoL: LCK (Korea), LEC (Europa), LCS (USA), LPL (China)
    "esports_lol":                       {"nombre": "LoL LCK/LEC/LCS",      "deporte": "Esports", "tipo": "esports"},
    # Dota 2: The International, DPC
    "esports_dota2":                     {"nombre": "Dota 2",               "deporte": "Esports", "tipo": "esports"},
    # Valorant: VCT
    "esports_valorant":                  {"nombre": "Valorant VCT",         "deporte": "Esports", "tipo": "esports"},
}

CONTEXT_RULES = [
    {"id":"champion_early","descripcion":"Campeón anticipado","penalizacion":0.22,"descartar":True,
     "equipos":["Bayern Munich","FC Bayern","Bayern München"]},
    {"id":"relegated","descripcion":"Equipo ya descendido","penalizacion":0.20,"descartar":True,"equipos":[]},
    {"id":"esport","descripcion":"Esport — Kelly conservador","penalizacion":0.0,"descartar":False,
     "kelly_override":0.20,"max_stake_override":0.02,"deportes":["Esports"]},
]

# ── Mapeo OddsPapi: sport_key (The Odds API) → tournamentId (OddsPapi) ────────
# OddsPapi usa IDs numéricos por torneo. Este mapeo se obtuvo de
# https://api.oddspapi.io/v4/tournaments?sportId={N} en mayo 2026.
# Mapeo conservador: solo los torneos con cobertura confirmada de Pinnacle.
ODDSPAPI_TOURNAMENT_MAP = {
    # ── Fútbol ────────────────────────────────────────────────────────────────
    "soccer_epl":                           17,    # Premier League (England)
    "soccer_spain_la_liga":                 8,     # LaLiga (Spain)
    "soccer_germany_bundesliga":            35,    # Bundesliga (Germany)
    "soccer_italy_serie_a":                 23,    # Serie A (Italy)
    "soccer_uefa_champs_league":            7,     # UEFA Champions League
    "soccer_uefa_europa_league":            679,   # UEFA Europa League
    "soccer_uefa_europa_conference_league": 34480, # UEFA Conference League
    "soccer_france_ligue_one":              34,    # Ligue 1 (France)
    "soccer_netherlands_eredivisie":        37,    # Eredivisie
    "soccer_portugal_primeira_liga":        238,   # Liga Portugal
    "soccer_conmebol_copa_libertadores":    384,   # Copa Libertadores
    "soccer_conmebol_copa_sudamericana":    480,   # Copa Sudamericana
    "soccer_argentina_primera_division":    155,   # Liga Profesional (Argentina)
    "soccer_brazil_campeonato":             325,   # Brasileiro Serie A
    "soccer_brazil_serie_b":                390,   # Brasileiro Serie B
    "soccer_chile_campeonato":              27665, # Primera Division (Chile)
    "soccer_mexico_ligamx":                 27464, # Liga MX Apertura (cambia según torneo activo)
    "soccer_usa_mls":                       242,   # MLS
    "soccer_fifa_world_cup":                16,    # World Cup
    # ── Otros deportes: se completarán cuando confirmemos sus IDs en próximos
    # pasos. Por ahora OddsPapi solo se usa para fútbol — donde Pinnacle aporta
    # más valor (líneas sharp más estables).
    # TODO: tennis (sportId=12), basketball (11), baseball (13), hockey (15),
    #       mma (20), esports varios (16-18, 56-61)
}

MARKET_LABELS = {
    "h2h":     "Resultado (1X2)",
    "btts":    "Ambos anotan",
    "totals":  "Over/Under",
    "spreads": "Hándicap",
    "sets":    "Sets totales",
}

# Labels específicos por tipo de deporte (más informativos)
# The Odds API usa "spreads" y "totals" pero la unidad varía por deporte:
#  - Tenis: hándicap = GAMES, totals = GAMES
#  - Básquet/Hockey/Béisbol: hándicap = PUNTOS/GOLES/RUNS, totals = idem
#  - Fútbol: hándicap = GOLES (asian handicap), totals = GOLES
def market_label_for(market_key: str, tipo_sport: str) -> str:
    base = MARKET_LABELS.get(market_key, market_key)
    if market_key == "totals":
        if tipo_sport == "tennis":      return "Over/Under games"
        if tipo_sport == "basketball":  return "Over/Under puntos"
        if tipo_sport == "soccer":      return "Over/Under goles"
        if tipo_sport == "hockey":      return "Over/Under goles"
        if tipo_sport == "baseball":    return "Over/Under runs"
        return base
    if market_key == "spreads":
        if tipo_sport == "tennis":      return "Hándicap games"
        if tipo_sport == "basketball":  return "Hándicap puntos"
        if tipo_sport == "soccer":      return "Hándicap goles"
        if tipo_sport == "hockey":      return "Hándicap goles"
        if tipo_sport == "baseball":    return "Hándicap runs"
        return base
    return base

# ── Categorías de picks por cuota ─────────────────────────────────────────────
# Política conservadora: el usuario prefiere picks con probabilidad implícita
# razonable (>40%). Cuotas >2.50 son demasiado azarosas aunque haya "edge".
# Seguro: @1.30-@2.10 — mayor win rate, menor varianza (>47% prob implícita)
# Alto Valor: @2.10-@2.50 — equilibrio riesgo/recompensa (>40% prob implícita)
# Especulativo: @2.50-@3.00 — solo para mostrar como referencia, NO se incluyen
# como Gold Tips (filtrado por ODDS_GOLD_MAX abajo).
ODDS_SEGURO_MAX = 2.10
ODDS_ALTO_MAX   = 2.50
ODDS_GOLD_MAX   = 2.50  # Cuota máxima que se acepta como Gold Tip. Picks con
                        # cuotas >2.50 quedan en "descartados" con razón clara.

@dataclass
class ValuePick:
    id: str; tipo: str; evento: str; deporte: str; liga: str
    mercado: str; equipo_pick: str; odds_ref: float
    prob_ajustada: float; edge: float; gold_score: float
    stake_usd: float; ganancia_pot: float; roi_diario_pct: float
    es_gold: bool; es_vivo: bool; categoria: str   # "seguro" o "alto_valor"
    tiene_pinnacle: bool                            # Si Pinnacle confirmó la línea
    contexto_id: str; contexto_desc: str
    descartado: bool; razon_descarte: Optional[str]
    hora_local: Optional[str]; horas_para_inicio: float

@dataclass
class SurePick:
    id: str; tipo: str; evento: str; deporte: str; liga: str
    mercado: str; equipo_pick: str; odds_ref: float
    prob_modelo: float; prob_pinnacle: float; prob_consensus: float
    confianza_pct: float; nivel_confianza: str; señales: str
    stake_usd: float; stake_pct: float; ganancia_pot: float; roi_pct: float
    contexto_id: str; contexto_desc: str
    hora_local: Optional[str]; horas_para_inicio: float

# ── Helpers ────────────────────────────────────────────────────────────────────

def horas_hasta(commence: str) -> float:
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except:
        return 999.0

def format_hora(commence: str, horas: float) -> str:
    try:
        dt     = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        dt_arg = dt.astimezone(timezone(timedelta(hours=TZ_OFFSET)))
        fecha  = dt_arg.strftime("%d/%m")
        hora   = dt_arg.strftime("%H:%M")
        if horas < 0:    cuando = "En curso"
        elif horas < 1:  cuando = f"En {int(horas*60)}min"
        elif horas < 24: cuando = f"En {int(horas)}h"
        else:            cuando = f"En {int(horas/24)}d"
        return f"{fecha} {hora} · {cuando}"
    except:
        return commence[:16] if commence else ""

def detect_context(equipo: str, deporte: str) -> dict:
    for rule in CONTEXT_RULES:
        if "deportes" in rule and deporte in rule["deportes"]:
            return rule
        for eq in rule.get("equipos", []):
            if eq.lower() in equipo.lower():
                return rule
    return {"id":"clean","descripcion":"Sin alertas","penalizacion":0.0,"descartar":False}

def prob_pinnacle(bookmakers, outcome_name, market_key) -> Optional[float]:
    bm = next((b for b in bookmakers if b.get("key") == "pinnacle"), None)
    if not bm: return None
    for mkt in bm.get("markets", []):
        if mkt["key"] != market_key: continue
        odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == outcome_name), None)
        if not odds_t or odds_t <= 1.0: return None
        total = sum(1/o["price"] for o in mkt["outcomes"] if o["price"] > 1.0)
        return (1/odds_t) / total if total > 0 else None

def prob_consensus(bookmakers, outcome_name, market_key) -> Optional[float]:
    probs = []
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key: continue
            odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == outcome_name), None)
            if not odds_t or odds_t <= 1.0: continue
            total = sum(1/o["price"] for o in mkt["outcomes"] if o["price"] > 1.0)
            if total > 0: probs.append((1/odds_t) / total)
    if not probs: return None
    probs.sort()
    n = len(probs)
    return probs[n//2] if n % 2 else (probs[n//2-1] + probs[n//2]) / 2

def odds_count(bookmakers, outcome_name, market_key) -> int:
    return sum(
        1 for bm in bookmakers
        for mkt in bm.get("markets", [])
        if mkt["key"] == market_key
        and any(o["name"] == outcome_name and o["price"] > 1.0 for o in mkt["outcomes"])
    )

def best_odds(bookmakers, outcome_name, market_key) -> tuple[float, str]:
    best, casa = 0.0, ""
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key: continue
            odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == outcome_name), None)
            if odds_t and odds_t > best:
                best = odds_t; casa = bm.get("title", bm.get("key", ""))
    return best, casa

def best_odds_filtered(bookmakers, outcome_name, market_key, max_dev_pct: float = 0.05) -> tuple[float, str, bool]:
    """Versión anti-outlier de best_odds.

    Calcula el promedio de cuotas de todas las casas y descarta la mejor cuota
    si difiere más de `max_dev_pct` del promedio (default 5%). Esto evita
    picks de edge fantasma causados por una sola casa con cuota errónea o muy
    diferente al consenso del mercado.

    Returns:
        (mejor_cuota, nombre_casa, fue_outlier_descartado)
        Si la mejor era outlier, retorna la SEGUNDA mejor cuota legítima.
    """
    # Recopilar todas las cuotas válidas
    cuotas = []
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key: continue
            odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == outcome_name), None)
            if odds_t and odds_t > 1.0:
                cuotas.append((odds_t, bm.get("title", bm.get("key", ""))))

    if not cuotas:
        return 0.0, "", False
    if len(cuotas) < 3:
        # Pocas casas: no podemos detectar outliers, usar la mejor
        cuotas.sort(reverse=True)
        return cuotas[0][0], cuotas[0][1], False

    cuotas.sort(reverse=True)
    # Promedio excluyendo la cuota más alta (para no contaminarse del outlier)
    avg_sin_max = sum(c[0] for c in cuotas[1:]) / len(cuotas[1:])
    max_aceptable = avg_sin_max * (1 + max_dev_pct)

    if cuotas[0][0] > max_aceptable:
        # La mejor cuota es outlier — descartar y usar la segunda
        return cuotas[1][0], cuotas[1][1], True
    return cuotas[0][0], cuotas[0][1], False

def calc_modelo_prob(p_pinn, p_cons, penali, n_casas) -> float:
    if p_pinn is not None:
        # Con Pinnacle real: peso mayor a Pinnacle
        p_base = 0.70 * p_pinn + 0.30 * p_cons
    else:
        # Sin Pinnacle: descuento más agresivo — no confiar solo en consensus
        p_base = p_cons * 0.94  # descuento 6% por falta de sharp data
    bonus = min(n_casas / 10, 1.0) * 0.015
    return max(0.01, min(0.99, p_base + bonus - penali))

def nivel_label(prob: float) -> str:
    if prob >= 0.92: return "EXTREMA"
    if prob >= 0.89: return "MUY ALTA"
    return "ALTA"

def señales_texto(p_pinn, p_cons, n_casas, horas, tiene_pinnacle) -> str:
    s = []
    if tiene_pinnacle and p_pinn:
        s.append(f"✓ Pinnacle confirma {p_pinn*100:.0f}%")
    if n_casas >= 8: s.append(f"{n_casas} casas cubren el evento")
    if 0 < horas <= 6: s.append("Menos de 6hs — odds estables")
    if p_pinn and abs(p_pinn - p_cons) < 0.03: s.append("Consensus concentrado")
    return " · ".join(s) if s else f"Consensus de {n_casas} casas"

def kelly_stake(prob, odds, kf=None, maxp=None) -> float:
    kf = kf or KELLY_FRAC; maxp = maxp or MAX_STAKE_PCT
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    return round(BANKROLL * min(k * kf, maxp), 2)

def gold_score_fn(prob, odds, edge, horas, tiene_pinnacle) -> float:
    score = prob * (odds - 1) * edge
    # Bonificación por cuotas en zona segura
    if 1.40 <= odds <= 2.10:  score *= 1.30   # sweet spot máximo
    elif 2.10 < odds <= 2.50: score *= 1.10
    elif odds > 3.0:          score *= 0.70   # penalizar cuotas muy altas
    elif odds < 1.30:         score *= 0.60   # muy baja cuota
    # Bonificación por Pinnacle real
    if tiene_pinnacle:        score *= 1.20
    # Bonificación POR PROXIMIDAD TEMPORAL (clave para mostrar primero los partidos
    # de hoy/próximas horas, no los de mañana). Cuanto más lejano, más penalización
    # porque las cuotas y el modelo son menos confiables a 24-48hs.
    if 0 < horas <= 3:        score *= 1.50   # ⚡ próximas 3hs — máxima prioridad
    elif horas <= 6:          score *= 1.30   # hoy mismo
    elif horas <= 12:         score *= 1.15   # mismo día / noche
    elif horas <= 24:         score *= 1.00   # mañana — neutral
    elif horas <= 36:         score *= 0.80   # pasado mañana — penaliza
    else:                     score *= 0.60   # >36hs — cuotas inestables, baja prioridad
    return round(score, 6)

def enriquecer_outcome(outcome_name, mkt_outcomes) -> str:
    for o in mkt_outcomes:
        if o.get("name") == outcome_name:
            punto = o.get("point") or o.get("handicap")
            if punto is not None:
                if outcome_name in ("Over", "Under"):
                    return f"{outcome_name} {punto}"
                elif outcome_name not in ("Yes", "No"):
                    return f"{outcome_name} ({punto:+.1f})" if punto != 0 else outcome_name
    return outcome_name

# ── OddsPapi integration ───────────────────────────────────────────────────────
# API correcta: https://api.oddspapi.io/v4/
# Documentación: https://oddspapi.io/en/docs
#
# Estructura de respuesta (relevante):
#   [{
#     "fixtureId": "id...",
#     "participant1Name": "Manchester United",
#     "participant2Name": "Liverpool",
#     "startTime": "2026-05-14T15:00:00.000Z",
#     "bookmakerOdds": {
#       "pinnacle": {
#         "markets": {
#           "101": {  # 101 = Full Time Result (1X2)
#             "outcomes": {
#               "101": {"players":{"0":{"price": 2.50}}},  # Home
#               "102": {"players":{"0":{"price": 3.40}}},  # Draw
#               "103": {"players":{"0":{"price": 2.90}}}   # Away
#             }
#           }
#         }
#       },
#       "bet365": {...}, "draftkings": {...}
#     }
#   }]

# Market IDs de OddsPapi (los que nos interesan)
OP_MARKET_FULLTIME_1X2 = "101"  # equivalente a h2h en The Odds API

# Outcome IDs dentro de market 101
OP_OUTCOME_HOME = "101"
OP_OUTCOME_DRAW = "102"
OP_OUTCOME_AWAY = "103"


def _oddspapi_reset_counter_if_new_month():
    """Resetea el contador mensual si cambió el mes."""
    mes_actual = datetime.now().month
    if _oddspapi_state["mes_actual"] != mes_actual:
        log.info(f"OddsPapi: nuevo mes, reseteando contador (eran {_oddspapi_state['requests_mes']} req en mes anterior)")
        _oddspapi_state["requests_mes"] = 0
        _oddspapi_state["mes_actual"] = mes_actual


def _oddspapi_quota_status() -> dict:
    """Estado actual del consumo de quota — visible para admin/logs."""
    _oddspapi_reset_counter_if_new_month()
    return {
        "requests_consumidos_mes": _oddspapi_state["requests_mes"],
        "limite_free_tier": 250,
        "ultimo_error": _oddspapi_state["ultimo_error"],
        "cache_entries": len(_oddspapi_state["cache"]),
    }


def _oddspapi_get_tournament_odds(tournament_id: int) -> list:
    """Obtiene odds (con Pinnacle) para todos los fixtures de un torneo.

    Usa cache de 60 min para no quemar quota.
    Retorna lista de fixtures con bookmakerOdds, o [] si falla / sin key / quota agotada.
    """
    if not ODDSPAPI_KEY:
        return []

    _oddspapi_reset_counter_if_new_month()

    # Cache hit?
    cache_key = str(tournament_id)
    cached = _oddspapi_state["cache"].get(cache_key)
    if cached and (time.time() - cached["ts"]) < ODDSPAPI_CACHE_TTL:
        log.debug(f"OddsPapi cache HIT tournament {tournament_id} ({len(cached['data'])} fixtures)")
        return cached["data"]

    # Aviso si estamos cerca del límite free
    if _oddspapi_state["requests_mes"] >= 240:
        log.warning(f"OddsPapi quota casi agotada: {_oddspapi_state['requests_mes']}/250 req. Continúo sin Pinnacle hasta el próximo mes.")
        return []

    try:
        # Endpoint correcto: /v4/odds-by-tournaments?bookmaker=pinnacle&tournamentIds=17
        url = f"{ODDSPAPI_URL}/odds-by-tournaments"
        params = {
            "apiKey": ODDSPAPI_KEY,
            "tournamentIds": str(tournament_id),
            "bookmaker": "pinnacle",  # solo Pinnacle — es lo que nos da edge sharp
            "oddsFormat": "decimal",
        }
        r = requests.get(url, params=params, timeout=20)
        _oddspapi_state["requests_mes"] += 1

        if r.status_code == 401:
            _oddspapi_state["ultimo_error"] = "INVALID_API_KEY"
            log.error(f"OddsPapi: API key inválida (HTTP 401). Verificá ODDSPAPI_KEY en Railway.")
            return []
        if r.status_code == 429:
            _oddspapi_state["ultimo_error"] = "RATE_LIMITED"
            log.error(f"OddsPapi: rate limit alcanzado (HTTP 429). Quota mensual agotada.")
            return []
        if r.status_code != 200:
            _oddspapi_state["ultimo_error"] = f"HTTP_{r.status_code}"
            log.warning(f"OddsPapi tournament {tournament_id}: HTTP {r.status_code}")
            return []

        data = r.json() or []
        _oddspapi_state["cache"][cache_key] = {"ts": time.time(), "data": data}
        _oddspapi_state["ultimo_error"] = None
        log.info(f"OddsPapi tournament {tournament_id}: {len(data)} fixtures con Pinnacle · {_oddspapi_state['requests_mes']}/250 req mensuales")
        return data

    except requests.Timeout:
        _oddspapi_state["ultimo_error"] = "TIMEOUT"
        log.warning(f"OddsPapi tournament {tournament_id}: timeout")
        return []
    except Exception as e:
        _oddspapi_state["ultimo_error"] = str(e)
        log.error(f"OddsPapi tournament {tournament_id}: {e}")
        return []


def _normalize_name(s: str) -> str:
    """Normaliza nombres de equipos para matching entre APIs.
    Quita acentos, lowercase, abreviaturas comunes."""
    if not s:
        return ""
    s = s.lower().strip()
    # Quitar abreviaturas y palabras de relleno comunes
    for w in [" fc", " cf", " ac", " sc", " united", " utd", " city", " athletic", " club"]:
        s = s.replace(w, "")
    # Normalizar caracteres latinos comunes
    for a, b in [("á","a"),("à","a"),("â","a"),("ã","a"),("ä","a"),
                 ("é","e"),("è","e"),("ê","e"),("ë","e"),
                 ("í","i"),("ì","i"),("î","i"),("ï","i"),
                 ("ó","o"),("ò","o"),("ô","o"),("õ","o"),("ö","o"),
                 ("ú","u"),("ù","u"),("û","u"),("ü","u"),
                 ("ñ","n"),("ç","c")]:
        s = s.replace(a, b)
    # Quitar espacios extra
    return " ".join(s.split())


def _match_fixture(home: str, away: str, op_fixtures: list) -> Optional[dict]:
    """Encuentra el fixture de OddsPapi que matchea con los equipos dados.
    Hace fuzzy match por nombre normalizado."""
    h_norm = _normalize_name(home)
    a_norm = _normalize_name(away)
    if not h_norm or not a_norm:
        return None

    for f in op_fixtures:
        p1 = _normalize_name(f.get("participant1Name", ""))
        p2 = _normalize_name(f.get("participant2Name", ""))
        # Match exacto (después de normalizar) o que uno contenga al otro (caso "Man Utd" vs "Manchester")
        if (h_norm in p1 or p1 in h_norm) and (a_norm in p2 or p2 in a_norm):
            return f
        # También probar invertido (a veces home/away se invierte entre APIs)
        if (h_norm in p2 or p2 in h_norm) and (a_norm in p1 or p1 in a_norm):
            return f
    return None


def _convert_op_to_bookmaker_format(op_fixture: dict, market_key: str) -> Optional[dict]:
    """Convierte el fixture de OddsPapi al formato bookmaker que usa el engine.

    El engine espera estructura tipo The Odds API:
      {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [{"name": "Team A", "price": 2.5}]}]}

    Retorna ese dict o None si no hay datos de Pinnacle para este mercado.
    """
    op_pinnacle = op_fixture.get("bookmakerOdds", {}).get("pinnacle")
    if not op_pinnacle:
        return None

    p1_name = op_fixture.get("participant1Name", "")
    p2_name = op_fixture.get("participant2Name", "")

    # Por ahora solo soportamos h2h (mercado 101 en OddsPapi)
    # Otros mercados (totals, spreads) requieren mapeo de market IDs adicional —
    # se agregarán cuando ampliemos el piloto.
    if market_key != "h2h":
        return None

    market_101 = op_pinnacle.get("markets", {}).get(OP_MARKET_FULLTIME_1X2)
    if not market_101:
        return None

    outcomes_raw = market_101.get("outcomes", {})
    outcomes_out = []

    # Home win (outcome 101)
    home_oc = outcomes_raw.get(OP_OUTCOME_HOME, {})
    home_price = home_oc.get("players", {}).get("0", {}).get("price")
    if home_price and home_price > 1.0:
        outcomes_out.append({"name": p1_name, "price": float(home_price)})

    # Draw (outcome 102) — solo si existe (no aplica para tenis/básquet/etc)
    draw_oc = outcomes_raw.get(OP_OUTCOME_DRAW, {})
    draw_price = draw_oc.get("players", {}).get("0", {}).get("price")
    if draw_price and draw_price > 1.0:
        outcomes_out.append({"name": "Draw", "price": float(draw_price)})

    # Away win (outcome 103)
    away_oc = outcomes_raw.get(OP_OUTCOME_AWAY, {})
    away_price = away_oc.get("players", {}).get("0", {}).get("price")
    if away_price and away_price > 1.0:
        outcomes_out.append({"name": p2_name, "price": float(away_price)})

    if not outcomes_out:
        return None

    return {
        "key": "pinnacle",
        "title": "Pinnacle",
        "markets": [{"key": market_key, "outcomes": outcomes_out}],
    }


def get_oddspapi_bookmakers(sport_key: str, eventos_tao: list = None) -> list:
    """Obtiene fixtures de OddsPapi con Pinnacle para un sport_key dado.

    Versión nueva (mayo 2026): usa api.oddspapi.io/v4/odds-by-tournaments con
    tournamentId numérico. Cache 60 min. Solo pide si hay partidos en próximas
    12 horas (regla dinámica) para minimizar consumo de quota.

    Args:
        sport_key: clave de The Odds API (ej "soccer_epl")
        eventos_tao: lista de eventos de The Odds API para este sport.
            Si está provista, solo pedimos OddsPapi si hay eventos en próximas 12hs.
            Si es None, pedimos siempre (modo legacy).

    Returns:
        Lista de fixtures de OddsPapi (con bookmakerOdds.pinnacle), o [] si:
        - No hay key configurada
        - El sport no está mapeado a un tournamentId
        - No hay eventos próximos en 12hs (cuando eventos_tao se pasa)
        - La request falla / quota agotada
    """
    if not ODDSPAPI_KEY:
        return []

    tournament_id = ODDSPAPI_TOURNAMENT_MAP.get(sport_key)
    if not tournament_id:
        log.debug(f"OddsPapi: sport_key '{sport_key}' no mapeado, saltando enriquecimiento Pinnacle")
        return []

    # Regla dinámica: solo pedir si hay partidos en próximas 12 horas.
    # Para partidos lejanos, las líneas de Pinnacle todavía no se estabilizaron
    # y no aporta tanto valor — mejor reservar quota.
    if eventos_tao is not None:
        hay_partido_proximo = any(
            0 < horas_hasta(ev.get("commence_time", "")) <= 12
            for ev in eventos_tao
        )
        if not hay_partido_proximo:
            log.debug(f"OddsPapi {sport_key}: sin partidos en próximas 12hs, saltando")
            return []

    return _oddspapi_get_tournament_odds(tournament_id)


# ── Analizador ────────────────────────────────────────────────────────────────

def _analizar(ev, meta, market_key, es_vivo=False, oddspapi_eventos=None):
    value_picks, sure_picks = [], []
    home, away   = ev["home_team"], ev["away_team"]
    commence     = ev.get("commence_time", "")
    bookmakers   = list(ev.get("bookmakers", []))
    if not bookmakers: return [], []

    # Enriquecer con datos de OddsPapi si están disponibles (Pinnacle real).
    # Nueva estructura: oddspapi_eventos viene del endpoint /odds-by-tournaments
    # con campos participant1Name/participant2Name y bookmakerOdds.pinnacle nested.
    if oddspapi_eventos:
        op_match = _match_fixture(home, away, oddspapi_eventos)
        if op_match:
            op_bm = _convert_op_to_bookmaker_format(op_match, market_key)
            if op_bm and not any(b.get("key") == "pinnacle" for b in bookmakers):
                bookmakers.append(op_bm)
                log.info(f"Pinnacle enriquecido: {home} vs {away} ({market_key})")

    horas       = horas_hasta(commence)
    hora_local  = format_hora(commence, horas)
    mercado_lbl = market_label_for(market_key, meta.get("tipo", "soccer"))
    min_edge_ok = MIN_EDGE_VIVO if es_vivo else MIN_EDGE
    tiene_pinnacle = any(b.get("key") == "pinnacle" for b in bookmakers)

    # Enriquecer con contexto real (API-Sports)
    ctx_deportivo = enriquecer_evento(home, away, meta["deporte"], meta["nombre"])

    # Filtros críticos por deporte
    # Tenis con diferencia extrema de ranking: solo descartar h2h, permitir mercados alternativos
    if ctx_deportivo.get("diferencia_extrema") and meta["deporte"] == "Tenis" and market_key == "h2h":
        log.info(f"Tenis h2h descartado por ranking extremo: {home} vs {away} — buscar spreads/totals")
        return [], []

    if ctx_deportivo.get("descartar_esports") and meta["deporte"] == "Esports":
        log.info(f"Esports descartado — equipos desconocidos: {home} vs {away}")
        return [], []

    # Béisbol h2h: bloqueado hasta activar pitcher data en context.py.
    # En MLB, el pitcher abridor determina ~60-70% del resultado. Sin esa info,
    # el modelo está ciego y genera picks de pérdida esperada (causa principal
    # del -19% del día 2). Permitimos totals/spreads cuando los habilitemos,
    # pero NO h2h hasta tener pitcher data.
    if meta.get("tipo") == "baseball" and market_key == "h2h":
        log.debug(f"MLB h2h bloqueado por falta de pitcher data: {home} vs {away}")
        return [], []

    outcomes_set = set()
    outcomes_map = {}  # original → display
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt["outcomes"]:
                    nombre_orig = out["name"]
                    nombre_rico = enriquecer_outcome(nombre_orig, mkt["outcomes"])
                    outcomes_set.add(nombre_orig)
                    outcomes_map[nombre_orig] = nombre_rico

    for outcome_name in outcomes_set:
        outcome_display = outcomes_map.get(outcome_name, outcome_name)
        p_pinn = prob_pinnacle(bookmakers, outcome_name, market_key)
        p_cons = prob_consensus(bookmakers, outcome_name, market_key)
        if p_cons is None: continue

        n_casas = odds_count(bookmakers, outcome_name, market_key)
        mejor, _, fue_outlier = best_odds_filtered(bookmakers, outcome_name, market_key, max_dev_pct=0.05)
        if mejor <= 1.20: continue  # Evitar favoritos extremos sin valor real
        if fue_outlier:
            log.info(f"Anti-outlier: descartada mejor cuota de {home} vs {away} · {outcome_name} (era >5% del promedio del consenso)")
        # En tenis h2h: si la cuota es muy baja Y no hay Pinnacle, descartar
        # (partidos muy desequilibrados donde el consenso puede estar equivocado)
        if market_key == "h2h" and meta.get("tipo") == "tennis" and mejor < 1.45 and not tiene_pinnacle:
            continue

        ctx    = detect_context(outcome_name, meta["deporte"])
        penali = ctx.get("penalizacion", 0.0)
        p_mod  = calc_modelo_prob(p_pinn, p_cons, penali, n_casas)

        # Ajustar con contexto deportivo real
        if ctx_deportivo:
            p_mod, ctx_señales = ajustar_prob_con_contexto(p_mod, ctx_deportivo)
        else:
            ctx_señales = []

        # Sure Pick — picks de alta confianza (≥75% prob modelo).
        # Respeta el cap de cuota @2.50 (política conservadora del usuario).
        # NO son "infalibles": en 100 picks a 75%, perdés 25. El stake usa Kelly.
        if not es_vivo and p_mod >= MIN_SURE_PROB and not ctx.get("descartar") and 1.10 <= mejor <= ODDS_GOLD_MAX:
            stake_s = kelly_stake(p_mod, mejor, KELLY_FRAC, 0.05)
            gan_s   = round(stake_s * (mejor - 1), 2)
            sure_picks.append(SurePick(
                id=f"sure-{ev['id']}-{market_key}-{outcome_display}".replace(" ","_"),
                tipo="sure", evento=f"{home} vs {away}",
                deporte=meta["deporte"], liga=meta["nombre"],
                mercado=mercado_lbl, equipo_pick=outcome_display,
                odds_ref=mejor, prob_modelo=round(p_mod,4),
                prob_pinnacle=round(p_pinn,4) if p_pinn else round(p_cons,4),
                prob_consensus=round(p_cons,4),
                confianza_pct=round(p_mod*100,1),
                nivel_confianza=nivel_label(p_mod),
                señales=señales_texto(p_pinn, p_cons, n_casas, horas, tiene_pinnacle),
                stake_usd=stake_s, stake_pct=round(stake_s/BANKROLL*100,2),
                ganancia_pot=gan_s, roi_pct=round(gan_s/BANKROLL*100,2),
                contexto_id=ctx["id"], contexto_desc=ctx["descripcion"],
                hora_local=hora_local, horas_para_inicio=round(horas,1),
            ))

        # Value Pick
        edge    = p_mod * mejor - 1
        kf      = ctx.get("kelly_override", KELLY_FRAC)
        msp     = ctx.get("max_stake_override", MAX_STAKE_PCT)
        stake   = kelly_stake(p_mod, mejor, kf, msp)
        gan     = round(stake * (mejor - 1), 2)
        gscore  = gold_score_fn(p_mod, mejor, edge, horas, tiene_pinnacle)

        # Límite edge anómalo — realista según teoría de mercados eficientes.
        # En mercados líquidos (NBA/NHL/EPL/MLB), edge >15% prácticamente no
        # existe. Pinnacle margin ~2%, casas retail ~5-7%. Un edge real puede ir
        # hasta 10-12%. Más allá = error de datos, casa rara o mal cálculo.
        # Con Pinnacle real (más confiable): permitimos hasta 15%
        # Sin Pinnacle: más estricto, solo hasta 12%
        limite  = 0.15 if tiene_pinnacle else 0.12
        anomalo = edge > limite

        # Categoría por cuota
        if mejor <= ODDS_SEGURO_MAX:
            categoria = "seguro"
        elif mejor <= ODDS_ALTO_MAX:
            categoria = "alto_valor"
        else:
            categoria = "especulativo"

        # Filtro de cuota máxima: descarta picks con cuota >2.50 (preferencia
        # del usuario por probabilidades implícitas >40%). Estos picks se ven
        # en la sección "Descartados" para referencia pero NUNCA como Gold Tips.
        cuota_alta = mejor > ODDS_GOLD_MAX

        desc  = ctx.get("descartar",False) or edge < min_edge_ok or anomalo or mejor < 1.15 or cuota_alta
        razon = None
        if ctx.get("descartar"):    razon = ctx["descripcion"]
        elif anomalo:               razon = f"Edge {edge*100:.1f}% anómalo"
        elif cuota_alta:            razon = f"Cuota @{mejor:.2f} > máximo @{ODDS_GOLD_MAX:.2f} (preferencia conservadora)"
        elif mejor < 1.15:          razon = "Cuota muy baja"
        elif edge < min_edge_ok:    razon = f"Edge {edge*100:.1f}% bajo mínimo ({min_edge_ok*100:.0f}%)"

        value_picks.append(ValuePick(
            id=f"{'vivo-' if es_vivo else ''}{ev['id']}-{market_key}-{outcome_display}".replace(" ","_"),
            tipo="value", evento=f"{home} vs {away}",
            deporte=meta["deporte"], liga=meta["nombre"],
            mercado=mercado_lbl, equipo_pick=outcome_display,
            odds_ref=mejor, prob_ajustada=round(p_mod,4),
            edge=round(edge,4), gold_score=gscore,
            stake_usd=stake, ganancia_pot=gan,
            roi_diario_pct=round(gan/BANKROLL*100,2),
            es_gold=False, es_vivo=es_vivo,
            categoria=categoria, tiene_pinnacle=tiene_pinnacle,
            contexto_id=ctx["id"], contexto_desc=ctx["descripcion"],
            descartado=desc, razon_descarte=razon,
            hora_local=hora_local, horas_para_inicio=round(horas,1),
        ))

    return value_picks, sure_picks

# ── Scanner principal ──────────────────────────────────────────────────────────

def escanear_mercado(bankroll_usuario: float = None) -> dict:
    if not API_KEY:
        raise ValueError("Sin API key configurada")
    global BANKROLL
    if bankroll_usuario and bankroll_usuario > 0:
        BANKROLL = bankroll_usuario

    all_value:   list[ValuePick] = []
    all_sure:    list[SurePick]  = []
    all_vivo:    list[ValuePick] = []
    descartados: list[ValuePick] = []
    en_curso = []
    total = 0

    # OddsPapi: ya no pre-cargamos al inicio. Lo pedimos dentro del loop, solo
    # cuando hay partidos en próximas 12hs (regla dinámica para ahorrar quota).
    # Estado consumido se loguea al final del scan.

    for sport_key, meta in SPORTS_ACTIVE.items():
        tipo_sport  = meta.get("tipo", "soccer")
        markets     = MARKETS_BY_SPORT.get(tipo_sport, ["h2h"])
        markets_str = ",".join(markets)

        try:
            r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey": API_KEY, "regions": "eu,uk,us,au",
                "markets": markets_str, "oddsFormat": "decimal",
            }, timeout=20)

            # 422: mercado no soportado por este sport. Fallback a solo h2h.
            # Antes esto saltaba el deporte silenciosamente y perdíamos picks.
            if r.status_code == 422 and markets != ["h2h"]:
                log.warning(f"{meta['nombre']}: mercados {markets_str} no soportados, reintentando con solo h2h")
                markets = ["h2h"]
                markets_str = "h2h"
                r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                    "apiKey": API_KEY, "regions": "eu,uk,us,au",
                    "markets": markets_str, "oddsFormat": "decimal",
                }, timeout=20)

            if r.status_code == 422:
                log.warning(f"{meta['nombre']} ({sport_key}): 422 — sport key inválido o fuera de temporada")
                continue
            r.raise_for_status()
            eventos = r.json()
            if not eventos:
                log.info(f"{meta['nombre']}: 0 eventos (sport activo pero sin partidos próximos)")
                continue

            remaining = r.headers.get("x-requests-remaining","?")
            log.info(f"{meta['nombre']}: {len(eventos)} eventos · {remaining} requests restantes")

            # OddsPapi: solo pedir si este sport está mapeado y hay partidos
            # próximos. La función decide internamente (regla 12hs).
            op_eventos = get_oddspapi_bookmakers(sport_key, eventos)

            for ev in eventos:
                total += 1
                horas = horas_hasta(ev.get("commence_time",""))

                if horas <= 0:
                    en_curso.append({
                        "evento":  f"{ev['home_team']} vs {ev['away_team']}",
                        "deporte": meta["deporte"], "liga": meta["nombre"],
                        "hora_local": format_hora(ev.get("commence_time",""), horas),
                    })
                    for mk in markets:
                        vp, _ = _analizar(ev, meta, mk, es_vivo=True, oddspapi_eventos=op_eventos)
                        all_vivo.extend(p for p in vp if not p.descartado)
                elif horas <= VENTANA_HORAS:
                    for mk in markets:
                        vp, sp = _analizar(ev, meta, mk, es_vivo=False, oddspapi_eventos=op_eventos)
                        for p in vp:
                            (descartados if p.descartado else all_value).append(p)
                        all_sure.extend(sp)

            time.sleep(0.4)

        except requests.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code == 401: log.error("API key inválida"); break
            log.warning(f"{sport_key} HTTP {code}")
        except Exception as e:
            log.warning(f"{sport_key}: {e}")

    # ── Value Picks — dedup Over/Under y Spreads opuestos por evento+línea ─────
    # Cuando hay Over y Under (o Spreads contrarios) del mismo total/punto en el
    # mismo evento, solo puede tener edge real UNO. Nos quedamos con el de
    # mayor edge y descartamos el opuesto como ruido del modelo.
    def _linea_key(p):
        """Extrae la línea numérica de un pick para detectar opuestos del mismo mercado.
        Ej: 'Over 174.5' y 'Under 174.5' → misma key 'OU|174.5'
            'Lakers (+5.5)' y 'Celtics (-5.5)' → misma key 'SPR|5.5'
        Retorna None si el pick no tiene línea (h2h normal, btts, etc)."""
        ep = (p.equipo_pick or "").strip()
        # Over/Under: "Over 174.5", "Under 174.5"
        m = re.match(r"^(Over|Under)\s+([\d.]+)$", ep, re.IGNORECASE)
        if m:
            return f"OU|{m.group(2)}"
        # Spread: "Lakers (+5.5)", "Celtics (-5.5)" → misma línea absoluta
        m = re.search(r"\(([+-]?[\d.]+)\)$", ep)
        if m:
            try:
                val = abs(float(m.group(1)))
                return f"SPR|{val}"
            except ValueError:
                return None
        return None

    def _dedup_opuestos(picks):
        """Para cada (evento, línea) deja solo el pick con mayor edge."""
        picks_sorted = sorted(picks, key=lambda p: p.edge, reverse=True)
        seen, out = set(), []
        for p in picks_sorted:
            lk = _linea_key(p)
            if lk is None:
                out.append(p); continue
            key = f"{p.evento}|{p.mercado}|{lk}"
            if key in seen:
                log.info(f"Dedup opuesto: descartado {p.evento} · {p.equipo_pick} @ {p.odds_ref} (edge {p.edge*100:.1f}% < ganador)")
                continue
            seen.add(key)
            out.append(p)
        return out

    all_value = _dedup_opuestos(all_value)
    all_vivo  = _dedup_opuestos(all_vivo)

    # ── Sure Picks — deduplicar por evento+mercado ─────────────────────────────
    all_sure.sort(key=lambda s: s.prob_modelo, reverse=True)
    seen, sure_ok = set(), []
    for s in all_sure:
        key = f"{s.evento}-{s.mercado}"
        if key not in seen:
            seen.add(key); sure_ok.append(s)
    all_sure = sure_ok[:10]

    # ── Value Picks — Gold Tips ────────────────────────────────────────────────
    all_value.sort(key=lambda p: p.gold_score, reverse=True)

    # Candidatos: edge ≥ MIN_EDGE, cuota en rango útil
    # Política conservadora: SOLO seguros (≤@2.10) y alto_valor (≤@2.50).
    # Los especulativos (>@2.50) ya quedaron descartados por el filtro de cuota
    # alta en _analizar(), pero igual los excluimos del pool por seguridad.
    candidatos_seguros   = [p for p in all_value if not p.descartado and p.categoria == "seguro"]
    candidatos_alto      = [p for p in all_value if not p.descartado and p.categoria == "alto_valor"]
    # candidatos_especul → eliminados del flujo. Solo aparecen en "descartados".

    # Marcar Gold: primero seguros, luego alto valor. Sin completar con especulativos
    # (preferencia del usuario por cuotas razonables, máximo @2.50).
    gold_target = MAX_GOLD_TIPS
    seleccionados = []
    seen_ev = set()

    for pool in [candidatos_seguros, candidatos_alto]:
        for p in pool:
            if len(seleccionados) >= gold_target: break
            if p.evento not in seen_ev:
                seen_ev.add(p.evento)
                p.es_gold = True
                seleccionados.append(p)

    gold_picks = seleccionados

    # ── En vivo — deduplicar por evento, top 5 ────────────────────────────────
    all_vivo.sort(key=lambda p: p.gold_score, reverse=True)
    seen_vivo, vivo_ok = set(), []
    for p in all_vivo:
        if p.evento not in seen_vivo:
            seen_vivo.add(p.evento)
            vivo_ok.append(p)
    vivo_top = vivo_ok[:5]

    roi_gold = round(sum(p.ganancia_pot for p in gold_picks) / BANKROLL * 100, 2) if BANKROLL else 0
    roi_sure = round(sum(s.ganancia_pot for s in all_sure[:5]) / BANKROLL * 100, 2) if BANKROLL else 0

    con_pinnacle = sum(1 for p in gold_picks if p.tiene_pinnacle)
    log.info(f"Scan OK — {len(gold_picks)} Gold ({con_pinnacle} con Pinnacle) · {len(all_sure)} Sure · {len(vivo_top)} Vivo")

    # Status de quota OddsPapi (visible en logs de Railway)
    if ODDSPAPI_KEY:
        quota = _oddspapi_quota_status()
        log.info(f"OddsPapi quota: {quota['requests_consumidos_mes']}/250 req/mes · cache: {quota['cache_entries']} torneos" +
                 (f" · último error: {quota['ultimo_error']}" if quota['ultimo_error'] else ""))

    return {
        "timestamp":          datetime.now().isoformat(),
        "total_eventos":      total,
        "ventana_horas":      VENTANA_HORAS,
        "con_pinnacle":       con_pinnacle,
        "picks_validos":      [asdict(p) for p in gold_picks],
        "picks_descartados":  [asdict(p) for p in descartados[:50]],
        "gold_tips":          [asdict(p) for p in gold_picks],
        "sure_bets":          [asdict(s) for s in all_sure],
        "roi_gold_potencial": roi_gold,
        "expo_gold_usd":      round(sum(p.stake_usd for p in gold_picks), 2),
        "roi_sure_potencial": roi_sure,
        "picks_vivo":         [asdict(p) for p in vivo_top],
        "en_curso":           en_curso[:20],
        "bankroll":           BANKROLL,
        "oddspapi_quota":     _oddspapi_quota_status() if ODDSPAPI_KEY else None,
    }
