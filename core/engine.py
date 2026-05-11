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
BASE_URL      = "https://api.the-odds-api.com/v4"

# Mercados a analizar por deporte
MARKETS_BY_SPORT = {
    "soccer": ["h2h", "btts", "totals", "spreads"],
    "tennis": ["h2h", "sets"],
    "basketball": ["h2h", "totals", "spreads"],
    "mma": ["h2h"],
    "baseball": ["h2h", "totals", "spreads"],
    "esports": ["h2h"],
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
    "totals":  "Over/Under goles",
    "spreads": "Hándicap",
    "sets":    "Sets totales",
}

@dataclass
class ValuePick:
    id:               str
    tipo:             str
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
class SureBet:
    id:                   str
    tipo:                 str
    evento:               str
    deporte:              str
    liga:                 str
    mercado:              str
    hora_local:           Optional[str]
    horas_para_inicio:    float
    pick_a:               str
    odds_a:               float
    casa_a:               str
    stake_a:              float
    pick_b:               str
    odds_b:               float
    casa_b:               str
    stake_b:              float
    ganancia_garantizada: float
    roi_garantizado:      float
    inversion_total:      float
    prob_suma:            float

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

def pinnacle_prob(bookmakers: list, outcome_name: str, market_key: str) -> Optional[float]:
    """Probabilidad limpia usando Pinnacle como referencia o mediana del mercado."""
    pinnacle = next((b for b in bookmakers if b.get("key") == "pinnacle"), None)
    source   = pinnacle if pinnacle else None

    def prob_from_bm(bm):
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            outcomes = mkt["outcomes"]
            odds_t   = next((o["price"] for o in outcomes if o["name"] == outcome_name), None)
            if not odds_t or odds_t <= 1.0:
                return None
            total = sum(1/o["price"] for o in outcomes if o["price"] > 1.0)
            return (1/odds_t) / total if total > 0 else None

    if source:
        p = prob_from_bm(source)
        if p:
            return p

    # Fallback: mediana de todas las casas
    probs = []
    for bm in bookmakers:
        p = prob_from_bm(bm)
        if p:
            probs.append(p)
    if not probs:
        return None
    probs.sort()
    n = len(probs)
    return probs[n//2] if n % 2 else (probs[n//2-1] + probs[n//2]) / 2

def best_odds_for_outcome(bookmakers: list, outcome_name: str, market_key: str) -> tuple[float, str]:
    """Mejor cuota disponible y qué casa la ofrece."""
    best_odds = 0.0
    best_casa = ""
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            odds_t = next((o["price"] for o in mkt["outcomes"] if o["name"] == outcome_name), None)
            if odds_t and odds_t > best_odds:
                best_odds = odds_t
                best_casa = bm.get("title", bm.get("key", ""))
    return best_odds, best_casa

def kelly_stake(prob, odds, bankroll=None, kf=None, maxp=None):
    if bankroll is None: bankroll = BANKROLL
    if kf is None:       kf = KELLY_FRAC
    if maxp is None:     maxp = MAX_STAKE_PCT
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    capped = min(k * kf, maxp)
    return round(bankroll * capped, 2)

def gold_score(prob, odds, edge, horas):
    ev    = prob * (odds - 1)
    score = ev * edge
    if 1.60 <= odds <= 2.50: score *= 1.2
    elif odds > 4.0:          score *= 0.6
    elif odds < 1.30:         score *= 0.7
    if 0 <= horas <= 6:       score *= 1.15
    elif horas <= 12:         score *= 1.05
    return round(score, 6)

# ── Value Bets detector ────────────────────────────────────────────────────────

def analizar_value(ev: dict, meta: dict, market_key: str) -> list[ValuePick]:
    picks      = []
    home, away = ev["home_team"], ev["away_team"]
    commence   = ev.get("commence_time", "")
    bookmakers = ev.get("bookmakers", [])
    if not bookmakers:
        return []

    en_v, horas = evento_en_ventana(commence)
    if not en_v:
        return []
    hora_local = format_hora(commence, horas)

    # Recolectar todos los outcomes únicos de este mercado
    outcomes_set = set()
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt["outcomes"]:
                    outcomes_set.add(out["name"])

    for outcome_name in outcomes_set:
        prob_base = pinnacle_prob(bookmakers, outcome_name, market_key)
        if prob_base is None:
            continue

        best_odds, best_casa = best_odds_for_outcome(bookmakers, outcome_name, market_key)
        if best_odds <= 1.0:
            continue

        ctx     = detect_context(outcome_name, meta["deporte"])
        penali  = ctx.get("penalizacion", 0.0)
        prob_aj = max(0.01, prob_base - penali)
        edge    = prob_aj * best_odds - 1

        kf  = ctx.get("kelly_override", KELLY_FRAC)
        msp = ctx.get("max_stake_override", MAX_STAKE_PCT)
        stake_usd    = kelly_stake(prob_aj, best_odds, BANKROLL, kf, msp)
        ganancia_pot = round(stake_usd * (best_odds - 1), 2)
        roi_pct      = round(ganancia_pot / BANKROLL * 100, 2) if BANKROLL else 0
        gscore       = gold_score(prob_aj, best_odds, edge, horas)

        descartado = ctx.get("descartar", False) or edge < MIN_EDGE
        razon = None
        if ctx.get("descartar"):
            razon = ctx["descripcion"]
        elif edge < MIN_EDGE:
            razon = f"Edge {edge*100:.1f}% bajo mínimo"

        mercado_label = MARKET_LABELS.get(market_key, market_key)

        picks.append(ValuePick(
            id               = f"{ev['id']}-{market_key}-{outcome_name}".replace(" ", "_"),
            tipo             = "value",
            evento           = f"{home} vs {away}",
            deporte          = meta["deporte"],
            liga             = meta["nombre"],
            mercado          = mercado_label,
            equipo_pick      = outcome_name,
            odds_ref         = best_odds,
            prob_ajustada    = round(prob_aj, 4),
            edge             = round(edge, 4),
            gold_score       = gscore,
            stake_usd        = stake_usd,
            ganancia_pot     = ganancia_pot,
            roi_diario_pct   = roi_pct,
            es_gold          = False,
            contexto_id      = ctx["id"],
            contexto_desc    = ctx["descripcion"],
            descartado       = descartado,
            razon_descarte   = razon,
            hora_local       = hora_local,
            horas_para_inicio= horas,
        ))
    return picks

# ── Sure Bets detector ─────────────────────────────────────────────────────────

def detectar_sure_bets(ev: dict, meta: dict, market_key: str) -> list[SureBet]:
    """
    Detecta sure bets comparando las mejores cuotas de TODAS las casas.
    Una sure bet existe cuando: 1/odds_A + 1/odds_B < 1
    Garantiza ganancia matemática sin importar el resultado.
    """
    sure_bets  = []
    home, away = ev["home_team"], ev["away_team"]
    commence   = ev.get("commence_time", "")
    bookmakers = ev.get("bookmakers", [])
    if not bookmakers:
        return []

    en_v, horas = evento_en_ventana(commence)
    if not en_v:
        return []
    hora_local = format_hora(commence, horas)

    # Construir mapa: outcome -> mejor cuota + casa
    outcome_best: dict[str, tuple[float, str]] = {}
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for out in mkt["outcomes"]:
                name  = out["name"]
                price = out["price"]
                if price <= 1.0:
                    continue
                if name not in outcome_best or price > outcome_best[name][0]:
                    outcome_best[name] = (price, bm.get("title", bm.get("key", "")))

    outcomes = list(outcome_best.keys())
    if len(outcomes) < 2:
        return []

    # Para mercados binarios (h2h sin empate, btts, over/under)
    # comparamos pares de outcomes opuestos
    pares = []
    if market_key == "h2h" and len(outcomes) == 2:
        pares = [(outcomes[0], outcomes[1])]
    elif market_key == "h2h" and len(outcomes) == 3:
        # Fútbol con empate: buscar el par con menor suma de impl prob
        for i in range(len(outcomes)):
            for j in range(i+1, len(outcomes)):
                pares.append((outcomes[i], outcomes[j]))
    elif market_key in ("btts", "totals", "spreads"):
        if len(outcomes) == 2:
            pares = [(outcomes[0], outcomes[1])]

    mercado_label = MARKET_LABELS.get(market_key, market_key)

    for pick_a_name, pick_b_name in pares:
        odds_a, casa_a = outcome_best[pick_a_name]
        odds_b, casa_b = outcome_best[pick_b_name]

        prob_suma = (1/odds_a) + (1/odds_b)

        # Sure bet confirmada si prob_suma < 1 (ganancia garantizada)
        # Para Gold Tips queremos < 0.97 (ROI > 3% garantizado)
        if prob_suma >= 1.0:
            continue

        # Calcular stakes óptimos para garantizar misma ganancia en ambos lados
        # stake_a / stake_b = odds_b / odds_a
        inversion = BANKROLL * MAX_STAKE_PCT * 2  # usamos 10% del bankroll total
        stake_a = round(inversion * (1/odds_a) / prob_suma, 2)
        stake_b = round(inversion * (1/odds_b) / prob_suma, 2)
        inversion_total = stake_a + stake_b

        ganancia_a = stake_a * odds_a - inversion_total
        ganancia_b = stake_b * odds_b - inversion_total
        ganancia   = round(min(ganancia_a, ganancia_b), 2)
        roi        = round(ganancia / inversion_total * 100, 2) if inversion_total > 0 else 0

        if roi < 1.0:  # mínimo 1% de ROI garantizado
            continue

        sure_bets.append(SureBet(
            id                   = f"sure-{ev['id']}-{market_key}-{pick_a_name}-{pick_b_name}".replace(" ", "_"),
            tipo                 = "sure",
            evento               = f"{home} vs {away}",
            deporte              = meta["deporte"],
            liga                 = meta["nombre"],
            mercado              = mercado_label,
            hora_local           = hora_local,
            horas_para_inicio    = horas,
            pick_a               = pick_a_name,
            odds_a               = odds_a,
            casa_a               = casa_a,
            stake_a              = stake_a,
            pick_b               = pick_b_name,
            odds_b               = odds_b,
            casa_b               = casa_b,
            stake_b              = stake_b,
            ganancia_garantizada = ganancia,
            roi_garantizado      = roi,
            inversion_total      = inversion_total,
            prob_suma            = round(prob_suma, 4),
        ))

    return sure_bets

# ── Scanner principal ──────────────────────────────────────────────────────────

def escanear_mercado() -> dict:
    if not API_KEY:
        raise ValueError("Sin API key configurada")

    value_picks: list[ValuePick] = []
    sure_bets:   list[SureBet]  = []
    descartados: list[ValuePick] = []
    total = 0

    for sport_key, meta in SPORTS_ACTIVE.items():
        tipo_sport = meta.get("tipo", "soccer")
        markets    = MARKETS_BY_SPORT.get(tipo_sport, ["h2h"])
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
            log.info(f"{meta['nombre']}: {len(eventos)} eventos · mercados: {markets_str} · requests: {remaining}")

            for ev in eventos:
                total += 1
                for market_key in markets:
                    # Value Bets
                    for pick in analizar_value(ev, meta, market_key):
                        if pick.descartado:
                            descartados.append(pick)
                        else:
                            value_picks.append(pick)

                    # Sure Bets
                    for sb in detectar_sure_bets(ev, meta, market_key):
                        sure_bets.append(sb)

            time.sleep(0.4)

        except requests.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code == 401:
                log.error("API key inválida")
                break
            log.warning(f"{sport_key} HTTP {code}")
        except Exception as e:
            log.warning(f"{sport_key}: {e}")

    # Ordenar value picks por gold_score
    value_picks.sort(key=lambda p: p.gold_score, reverse=True)

    # Sure bets ordenadas por ROI garantizado
    sure_bets.sort(key=lambda s: s.roi_garantizado, reverse=True)

    # Gold Tips — top N value picks con filtros de calidad
    candidatos = [
        p for p in value_picks
        if p.edge >= 0.05
        and 1.40 <= p.odds_ref <= 3.50
        and p.horas_para_inicio >= 0
    ]
    for i, p in enumerate(candidatos):
        if i < MAX_GOLD_TIPS:
            p.es_gold = True

    gold_picks    = [p for p in value_picks if p.es_gold]
    roi_gold      = round(sum(p.ganancia_pot for p in gold_picks) / BANKROLL * 100, 2) if BANKROLL else 0
    expo_gold     = round(sum(p.stake_usd for p in gold_picks), 2)

    # Sure bets Gold — top 5 por ROI garantizado
    sure_gold = sure_bets[:5]
    roi_sure  = round(sum(s.ganancia_garantizada for s in sure_gold) / BANKROLL * 100, 2) if BANKROLL else 0

    return {
        "timestamp":           datetime.now().isoformat(),
        "total_eventos":       total,
        "ventana_horas":       VENTANA_HORAS,

        # Value Bets
        "picks_validos":       [asdict(p) for p in value_picks],
        "picks_descartados":   [asdict(p) for p in descartados],
        "gold_tips":           [asdict(p) for p in gold_picks],
        "roi_gold_potencial":  roi_gold,
        "expo_gold_usd":       expo_gold,

        # Sure Bets
        "sure_bets":           [asdict(s) for s in sure_bets],
        "sure_gold":           [asdict(s) for s in sure_gold],
        "roi_sure_garantizado": roi_sure,

        "bankroll": BANKROLL,
    }
