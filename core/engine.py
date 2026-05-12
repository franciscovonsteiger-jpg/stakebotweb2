import os, time, logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import requests

log = logging.getLogger("stakebot")

API_KEY       = os.getenv("ODDS_API_KEY", "")
BANKROLL      = float(os.getenv("BANKROLL_USD", 1000))
KELLY_FRAC    = float(os.getenv("KELLY_FRACTION", 0.5))
MAX_STAKE_PCT = float(os.getenv("MAX_STAKE_PCT", 0.05))
MIN_EDGE      = float(os.getenv("MIN_EDGE_PCT", 0.03))
MAX_GOLD_TIPS = int(os.getenv("MAX_GOLD_TIPS", 5))
VENTANA_HORAS = int(os.getenv("VENTANA_HORAS", 36))
MIN_SURE_PROB = float(os.getenv("MIN_SURE_PROB", 0.85))  # 85% mínimo para Sure Bet
BASE_URL      = "https://api.the-odds-api.com/v4"

MARKETS_BY_SPORT = {
    "soccer":     ["h2h", "btts", "totals", "spreads"],
    "tennis":     ["h2h", "sets"],
    "basketball": ["h2h", "totals", "spreads"],
    "mma":        ["h2h"],
    "baseball":   ["h2h", "totals", "spreads"],
    "esports":    ["h2h"],
}

SPORTS_ACTIVE = {
    "soccer_epl":                {"nombre": "Premier League",    "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_spain_la_liga":      {"nombre": "La Liga",           "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_germany_bundesliga": {"nombre": "Bundesliga",        "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_italy_serie_a":      {"nombre": "Serie A",           "deporte": "Fútbol",  "tipo": "soccer"},
    "soccer_uefa_champs_league": {"nombre": "Champions League",  "deporte": "Fútbol",  "tipo": "soccer"},
    "tennis_atp_french_open":    {"nombre": "ATP Roland Garros", "deporte": "Tenis",   "tipo": "tennis"},
    "tennis_wta_french_open":    {"nombre": "WTA Roland Garros", "deporte": "Tenis",   "tipo": "tennis"},
    "basketball_nba":            {"nombre": "NBA",               "deporte": "Básquet", "tipo": "basketball"},
    "baseball_mlb":              {"nombre": "MLB",               "deporte": "Béisbol", "tipo": "baseball"},
    "mma_mixed_martial_arts":    {"nombre": "MMA/UFC",           "deporte": "MMA",     "tipo": "mma"},
    "esports_lol":               {"nombre": "LoL LCK/LEC",       "deporte": "Esports", "tipo": "esports"},
    "esports_csgo":              {"nombre": "CS2 Pro League",    "deporte": "Esports", "tipo": "esports"},
}

CONTEXT_RULES = [
    {
        "id": "champion_early",
        "descripcion": "Campeón anticipado — sin motivación",
        "penalizacion": 0.22, "descartar": True,
        "equipos": ["Bayern Munich", "FC Bayern", "Bayern München"],
    },
    {
        "id": "relegated",
        "descripcion": "Equipo ya descendido",
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

MARKET_LABELS = {
    "h2h":     "Resultado (1X2)",
    "btts":    "Ambos anotan",
    "totals":  "Over/Under",
    "spreads": "Hándicap",
    "sets":    "Sets totales",
}

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ValuePick:
    id:               str
    tipo:             str   # 'value'
    evento:           str
    deporte:          str
    liga:             str
    mercado:          str
    equipo_pick:      str
    odds_ref:         float
    prob_ajustada:    float
    edge:             float
    gold_score:       float
    stake_usd:        float
    ganancia_pot:     float
    roi_diario_pct:   float
    es_gold:          bool
    contexto_id:      str
    contexto_desc:    str
    descartado:       bool
    razon_descarte:   Optional[str]
    hora_local:       Optional[str]
    horas_para_inicio: float

@dataclass
class SurePick:
    """
    High Confidence Pick — el modelo estima probabilidad real ≥ 85%.
    No es arbitraje. Es análisis profundo de alta convicción.
    """
    id:               str
    tipo:             str   # 'sure'
    evento:           str
    deporte:          str
    liga:             str
    mercado:          str
    equipo_pick:      str
    odds_ref:         float
    prob_modelo:      float  # probabilidad estimada por el modelo (≥ 85%)
    prob_pinnacle:    float  # prob según Pinnacle (referencia sharp)
    prob_consensus:   float  # mediana de todas las casas
    confianza_pct:    float  # prob_modelo * 100
    nivel_confianza:  str    # "MUY ALTA" / "ALTA" / "EXTREMA"
    # Señales adicionales que aumentan la convicción
    señales:          str
    # Stake — más alto por la alta confianza
    stake_usd:        float
    stake_pct:        float
    ganancia_pot:     float
    roi_pct:          float
    # Contexto
    contexto_id:      str
    contexto_desc:    str
    hora_local:       Optional[str]
    horas_para_inicio: float

# ── Helpers ────────────────────────────────────────────────────────────────────

def evento_en_ventana(commence: str) -> tuple[bool, float]:
    try:
        dt    = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        ahora = datetime.now(timezone.utc)
        horas = (dt - ahora).total_seconds() / 3600
        return -2 <= horas <= VENTANA_HORAS, round(horas, 1)
    except Exception:
        return True, 0.0

def format_hora(commence: str, horas: float) -> str:
    try:
        dt     = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        dt_arg = dt.astimezone(timezone(timedelta(hours=-3)))
        fecha  = dt_arg.strftime("%d/%m")
        hora   = dt_arg.strftime("%H:%M")
        if horas < 0:    cuando = "En curso"
        elif horas < 1:  cuando = f"En {int(horas*60)}min"
        elif horas < 24: cuando = f"En {int(horas)}h"
        else:            cuando = f"En {int(horas/24)}d"
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
    return {"id": "clean", "descripcion": "Sin alertas", "penalizacion": 0.0, "descartar": False}

def prob_pinnacle(bookmakers: list, outcome_name: str, market_key: str) -> Optional[float]:
    """Probabilidad según Pinnacle — la casa más eficiente del mercado."""
    bm = next((b for b in bookmakers if b.get("key") == "pinnacle"), None)
    if not bm:
        return None
    for mkt in bm.get("markets", []):
        if mkt["key"] != market_key:
            continue
        outcomes = mkt["outcomes"]
        odds_t   = next((o["price"] for o in outcomes if o["name"] == outcome_name), None)
        if not odds_t or odds_t <= 1.0:
            return None
        total = sum(1/o["price"] for o in outcomes if o["price"] > 1.0)
        return (1/odds_t) / total if total > 0 else None

def prob_consensus(bookmakers: list, outcome_name: str, market_key: str) -> Optional[float]:
    """Mediana de probabilidades de TODAS las casas — elimina outliers."""
    probs = []
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            outcomes = mkt["outcomes"]
            odds_t   = next((o["price"] for o in outcomes if o["name"] == outcome_name), None)
            if not odds_t or odds_t <= 1.0:
                continue
            total = sum(1/o["price"] for o in outcomes if o["price"] > 1.0)
            if total > 0:
                probs.append((1/odds_t) / total)
    if not probs:
        return None
    probs.sort()
    n = len(probs)
    return probs[n//2] if n % 2 else (probs[n//2-1] + probs[n//2]) / 2

def odds_count(bookmakers: list, outcome_name: str, market_key: str) -> int:
    """Cuántas casas ofrecen este outcome — más casas = más liquidez = más confianza."""
    count = 0
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            if any(o["name"] == outcome_name and o["price"] > 1.0 for o in mkt["outcomes"]):
                count += 1
    return count

def best_odds(bookmakers: list, outcome_name: str, market_key: str) -> tuple[float, str]:
    """Mejor cuota disponible para este outcome."""
    best, casa = 0.0, ""
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == outcome_name), None)
            if odds_t and odds_t > best:
                best = odds_t
                casa = bm.get("title", bm.get("key", ""))
    return best, casa

def calc_modelo_prob(
    p_pinnacle: Optional[float],
    p_consensus: float,
    contexto_penalizacion: float,
    n_casas: int,
) -> float:
    """
    Calcula la probabilidad final del modelo combinando:
    1. Pinnacle (peso 60%) — la señal más confiable
    2. Consensus (peso 40%) — validación del mercado amplio
    3. Bonus por liquidez (más casas = mercado más eficiente)
    4. Penalización de contexto motivacional
    """
    if p_pinnacle is not None:
        p_base = 0.60 * p_pinnacle + 0.40 * p_consensus
    else:
        p_base = p_consensus

    # Bonus de liquidez: más casas cubriendo el evento = más confianza
    # máximo +2% si hay 8+ casas
    liquidez_bonus = min(n_casas / 8, 1.0) * 0.02
    p_ajustada = p_base + liquidez_bonus - contexto_penalizacion

    return max(0.01, min(0.99, p_ajustada))

def nivel_confianza_label(prob: float) -> str:
    if prob >= 0.92: return "EXTREMA"
    if prob >= 0.89: return "MUY ALTA"
    return "ALTA"

def detectar_señales(
    p_pinnacle: Optional[float],
    p_consensus: float,
    n_casas: int,
    horas: float,
) -> str:
    """Genera texto de señales que soportan la alta confianza."""
    señales = []
    if p_pinnacle and p_pinnacle > 0.82:
        señales.append(f"Pinnacle confirma {p_pinnacle*100:.0f}%")
    if n_casas >= 8:
        señales.append(f"{n_casas} casas cubren el evento")
    if horas <= 6:
        señales.append("Partido en menos de 6hs — odds estables")
    if abs((p_pinnacle or p_consensus) - p_consensus) < 0.03:
        señales.append("Consensus muy concentrado — mercado eficiente")
    if not señales:
        señales.append(f"Consensus de {n_casas} casas")
    return " · ".join(señales)

def kelly_stake_sure(prob: float, odds: float, bankroll: float) -> tuple[float, float]:
    """
    Para Sure Picks de alta confianza usamos Kelly más agresivo
    pero con cap del 8% (vs 5% para value bets normales).
    """
    b      = odds - 1
    k      = max(0.0, (b * prob - (1-prob)) / b)
    kf     = 0.5   # seguimos usando 1/2 Kelly
    maxp   = 0.08  # cap más alto por la alta confianza
    capped = min(k * kf, maxp)
    return round(bankroll * capped, 2), round(capped * 100, 2)

def kelly_stake_value(prob: float, odds: float, kf: float, maxp: float) -> float:
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    return round(BANKROLL * min(k * kf, maxp), 2)

def gold_score(prob: float, odds: float, edge: float, horas: float) -> float:
    ev    = prob * (odds - 1)
    score = ev * edge
    if 1.60 <= odds <= 2.50: score *= 1.2
    elif odds > 4.0:          score *= 0.6
    elif odds < 1.30:         score *= 0.7
    if 0 <= horas <= 6:       score *= 1.15
    elif horas <= 12:         score *= 1.05
    return round(score, 6)

# ── Analizadores ───────────────────────────────────────────────────────────────

def analizar_evento(ev: dict, meta: dict, market_key: str) -> tuple[list, list]:
    """
    Analiza un evento en un mercado específico.
    Retorna (value_picks, sure_picks).
    """
    value_picks = []
    sure_picks  = []
    home, away  = ev["home_team"], ev["away_team"]
    commence    = ev.get("commence_time", "")
    bookmakers  = ev.get("bookmakers", [])

    if not bookmakers:
        return [], []

    en_v, horas = evento_en_ventana(commence)
    if not en_v:
        return [], []

    hora_local    = format_hora(commence, horas)
    mercado_label = MARKET_LABELS.get(market_key, market_key)

    # Recolectar outcomes únicos
    outcomes_set = set()
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt["outcomes"]:
                    outcomes_set.add(out["name"])

    for outcome_name in outcomes_set:
        # Probabilidades desde diferentes fuentes
        p_pinn = prob_pinnacle(bookmakers, outcome_name, market_key)
        p_cons = prob_consensus(bookmakers, outcome_name, market_key)
        if p_cons is None:
            continue

        n_casas = odds_count(bookmakers, outcome_name, market_key)
        mejor_odds, _ = best_odds(bookmakers, outcome_name, market_key)
        if mejor_odds <= 1.0:
            continue

        # Contexto motivacional
        ctx    = detect_context(outcome_name, meta["deporte"])
        penali = ctx.get("penalizacion", 0.0)

        # Probabilidad final del modelo
        p_modelo = calc_modelo_prob(p_pinn, p_cons, penali, n_casas)

        # ── SURE PICK: probabilidad ≥ 85% ──────────────────────────────────────
        if p_modelo >= MIN_SURE_PROB and not ctx.get("descartar", False):
            stake, stake_pct = kelly_stake_sure(p_modelo, mejor_odds, BANKROLL)
            ganancia         = round(stake * (mejor_odds - 1), 2)
            roi              = round(ganancia / BANKROLL * 100, 2)
            señales          = detectar_señales(p_pinn, p_cons, n_casas, horas)

            sure_picks.append(SurePick(
                id               = f"sure-{ev['id']}-{market_key}-{outcome_name}".replace(" ","_"),
                tipo             = "sure",
                evento           = f"{home} vs {away}",
                deporte          = meta["deporte"],
                liga             = meta["nombre"],
                mercado          = mercado_label,
                equipo_pick      = outcome_name,
                odds_ref         = mejor_odds,
                prob_modelo      = round(p_modelo, 4),
                prob_pinnacle    = round(p_pinn, 4) if p_pinn else round(p_cons, 4),
                prob_consensus   = round(p_cons, 4),
                confianza_pct    = round(p_modelo * 100, 1),
                nivel_confianza  = nivel_confianza_label(p_modelo),
                señales          = señales,
                stake_usd        = stake,
                stake_pct        = stake_pct,
                ganancia_pot     = ganancia,
                roi_pct          = roi,
                contexto_id      = ctx["id"],
                contexto_desc    = ctx["descripcion"],
                hora_local       = hora_local,
                horas_para_inicio= horas,
            ))

        # ── VALUE PICK: edge positivo ───────────────────────────────────────────
        edge   = p_modelo * mejor_odds - 1
        kf     = ctx.get("kelly_override", KELLY_FRAC)
        msp    = ctx.get("max_stake_override", MAX_STAKE_PCT)
        stake  = kelly_stake_value(p_modelo, mejor_odds, kf, msp)
        gan    = round(stake * (mejor_odds - 1), 2)
        roi_v  = round(gan / BANKROLL * 100, 2)
        gscore = gold_score(p_modelo, mejor_odds, edge, horas)

        # Edge mayor a 40% es casi siempre un error de datos — descartar
        edge_anomalo = edge > 0.40
        descartado = ctx.get("descartar", False) or edge < MIN_EDGE or edge_anomalo or mejor_odds < 1.05
        razon = None
        if ctx.get("descartar"):
            razon = ctx["descripcion"]
        elif edge_anomalo:
            razon = f"Edge {edge*100:.1f}% anómalo — posible error de datos"
        elif mejor_odds < 1.05:
            razon = "Cuota demasiado baja — sin valor"
        elif edge < MIN_EDGE:
            razon = f"Edge {edge*100:.1f}% bajo mínimo"

        value_picks.append(ValuePick(
            id               = f"{ev['id']}-{market_key}-{outcome_name}".replace(" ","_"),
            tipo             = "value",
            evento           = f"{home} vs {away}",
            deporte          = meta["deporte"],
            liga             = meta["nombre"],
            mercado          = mercado_label,
            equipo_pick      = outcome_name,
            odds_ref         = mejor_odds,
            prob_ajustada    = round(p_modelo, 4),
            edge             = round(edge, 4),
            gold_score       = gscore,
            stake_usd        = stake,
            ganancia_pot     = gan,
            roi_diario_pct   = roi_v,
            es_gold          = False,
            contexto_id      = ctx["id"],
            contexto_desc    = ctx["descripcion"],
            descartado       = descartado,
            razon_descarte   = razon,
            hora_local       = hora_local,
            horas_para_inicio= horas,
        ))

    return value_picks, sure_picks

# ── Scanner principal ──────────────────────────────────────────────────────────

def escanear_mercado() -> dict:
    if not API_KEY:
        raise ValueError("Sin API key configurada")

    all_value:  list[ValuePick] = []
    all_sure:   list[SurePick]  = []
    descartados: list[ValuePick] = []
    total = 0

    for sport_key, meta in SPORTS_ACTIVE.items():
        tipo_sport  = meta.get("tipo", "soccer")
        markets     = MARKETS_BY_SPORT.get(tipo_sport, ["h2h"])
        markets_str = ",".join(markets)

        try:
            r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey":     API_KEY,
                "regions":    "eu,uk,us,au",
                "markets":    markets_str,
                "oddsFormat": "decimal",
            }, timeout=20)

            if r.status_code == 422:
                continue
            r.raise_for_status()

            eventos = r.json()
            if not eventos:
                continue

            remaining = r.headers.get("x-requests-remaining", "?")
            log.info(f"{meta['nombre']}: {len(eventos)} eventos · {markets_str} · requests: {remaining}")

            for ev in eventos:
                total += 1
                for market_key in markets:
                    vp, sp = analizar_evento(ev, meta, market_key)
                    for p in vp:
                        (descartados if p.descartado else all_value).append(p)
                    all_sure.extend(sp)

            time.sleep(0.4)

        except requests.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code == 401:
                log.error("API key inválida")
                break
            log.warning(f"{sport_key} HTTP {code}")
        except Exception as e:
            log.warning(f"{sport_key}: {e}")

    # ── Ordenar y seleccionar ──────────────────────────────────────────────────

    # Sure Picks: ordenados por confianza del modelo (mayor prob primero)
    all_sure.sort(key=lambda s: s.prob_modelo, reverse=True)

    # Deduplicar sure picks (mismo evento + mercado puede aparecer varias veces)
    sure_ids = set()
    sure_dedup = []
    for s in all_sure:
        key = f"{s.evento}-{s.mercado}-{s.equipo_pick}"
        if key not in sure_ids:
            sure_ids.add(key)
            sure_dedup.append(s)
    all_sure = sure_dedup

    # Value Picks: ordenados por gold_score
    all_value.sort(key=lambda p: p.gold_score, reverse=True)

    # Gold Tips — top N value picks con filtros
    candidatos = [
        p for p in all_value
        if p.edge >= 0.05
        and 1.30 <= p.odds_ref <= 3.50
        and p.horas_para_inicio >= 0
    ]
    for i, p in enumerate(candidatos):
        if i < MAX_GOLD_TIPS:
            p.es_gold = True

    gold_picks = [p for p in all_value if p.es_gold]

    # Métricas agregadas
    roi_gold   = round(sum(p.ganancia_pot for p in gold_picks) / BANKROLL * 100, 2) if BANKROLL else 0
    expo_gold  = round(sum(p.stake_usd for p in gold_picks), 2)
    roi_sure   = round(sum(s.ganancia_pot for s in all_sure[:5]) / BANKROLL * 100, 2) if BANKROLL else 0
    expo_sure  = round(sum(s.stake_usd for s in all_sure[:5]), 2)

    log.info(f"Scan completo — {len(all_value)} value picks · {len(all_sure)} sure picks (≥{MIN_SURE_PROB*100:.0f}%)")

    return {
        "timestamp":           datetime.now().isoformat(),
        "total_eventos":       total,
        "ventana_horas":       VENTANA_HORAS,
        "min_sure_prob":       MIN_SURE_PROB,

        # Value Bets
        "picks_validos":       [asdict(p) for p in all_value],
        "picks_descartados":   [asdict(p) for p in descartados],
        "gold_tips":           [asdict(p) for p in gold_picks],
        "roi_gold_potencial":  roi_gold,
        "expo_gold_usd":       expo_gold,

        # Sure Picks (alta confianza ≥ 85%)
        "sure_bets":           [asdict(s) for s in all_sure],
        "sure_gold":           [asdict(s) for s in all_sure[:5]],
        "roi_sure_potencial":  roi_sure,
        "expo_sure_usd":       expo_sure,

        "bankroll": BANKROLL,
    }
