import os, time, logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import requests

log = logging.getLogger("stakebot")

# ── Configuración ──────────────────────────────────────────────────────────────
API_KEY        = os.getenv("ODDS_API_KEY", "")
ODDSPAPI_KEY   = os.getenv("ODDSPAPI_KEY", "")       # Nueva API con Pinnacle real
BANKROLL       = float(os.getenv("BANKROLL_USD", 1000))
KELLY_FRAC     = float(os.getenv("KELLY_FRACTION", 0.25))   # 1/4 Kelly — más conservador
MAX_STAKE_PCT  = float(os.getenv("MAX_STAKE_PCT", 0.03))    # Cap 3% bankroll
MIN_EDGE       = float(os.getenv("MIN_EDGE_PCT", 0.07))     # Edge mínimo 7%
MIN_EDGE_VIVO  = 0.10                                        # En vivo más estricto
MAX_GOLD_TIPS  = int(os.getenv("MAX_GOLD_TIPS", 8))
VENTANA_HORAS  = int(os.getenv("VENTANA_HORAS", 48))
MIN_SURE_PROB  = float(os.getenv("MIN_SURE_PROB", 0.82))    # Slightly lower para más picks
TZ_OFFSET      = int(os.getenv("TZ_OFFSET", -3))
BASE_URL       = "https://api.the-odds-api.com/v4"
ODDSPAPI_URL   = "https://api.oddspapi.com"

# ── Mercados por deporte ───────────────────────────────────────────────────────
# Béisbol: solo h2h por ahora (más predecible sin Pinnacle)
# Con OddsPapi habilitamos totals y spreads en béisbol también
MARKETS_BY_SPORT = {
    "soccer":     ["h2h", "btts", "totals"],
    "tennis":     ["h2h"],
    "basketball": ["h2h", "totals"],
    "mma":        ["h2h"],
    "baseball":   ["h2h"],          # Solo h2h hasta tener Pinnacle confirmado
    "hockey":     ["h2h"],
    "esports":    ["h2h"],
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
}

CONTEXT_RULES = [
    {"id":"champion_early","descripcion":"Campeón anticipado","penalizacion":0.22,"descartar":True,
     "equipos":["Bayern Munich","FC Bayern","Bayern München"]},
    {"id":"relegated","descripcion":"Equipo ya descendido","penalizacion":0.20,"descartar":True,"equipos":[]},
    {"id":"esport","descripcion":"Esport — Kelly reducido","penalizacion":0.0,"descartar":False,
     "kelly_override":0.15,"max_stake_override":0.01,"deportes":["Esports"]},
]

MARKET_LABELS = {
    "h2h":"Resultado (1X2)","btts":"Ambos anotan",
    "totals":"Over/Under","spreads":"Hándicap","sets":"Sets totales",
}

# ── Categorías de picks por cuota ─────────────────────────────────────────────
# Seguro: @1.30-@2.10 — mayor win rate, menor varianza
# Alto Valor: @2.10-@3.00 — mayor ganancia, más arriesgado
ODDS_SEGURO_MAX = 2.10
ODDS_ALTO_MAX   = 3.50

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

def calc_modelo_prob(p_pinn, p_cons, penali, n_casas) -> float:
    if p_pinn is not None:
        # Con Pinnacle real: peso mayor a Pinnacle
        p_base = 0.70 * p_pinn + 0.30 * p_cons
    else:
        # Sin Pinnacle: solo consensus con descuento de confianza
        p_base = p_cons * 0.97  # descuento por incertidumbre
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
    # Bonificación por tiempo
    if 0 < horas <= 6:        score *= 1.10
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

def get_oddspapi_bookmakers(sport_key: str, event_id: str = None) -> list:
    """Obtiene odds de OddsPapi (incluye Pinnacle real) para enriquecer el análisis."""
    if not ODDSPAPI_KEY:
        return []
    try:
        # OddsPapi usa sport slugs diferentes — mapeo básico
        sport_map = {
            "baseball_mlb": "baseball_mlb",
            "basketball_nba": "basketball_nba",
            "icehockey_nhl": "icehockey_nhl",
            "soccer_epl": "soccer_epl",
            "soccer_spain_la_liga": "soccer_spain_la_liga",
            "tennis_atp_french_open": "tennis_atp_french_open",
        }
        sp = sport_map.get(sport_key, sport_key)
        r = requests.get(f"{ODDSPAPI_URL}/v1/odds", params={
            "apiKey": ODDSPAPI_KEY,
            "sport": sp,
            "regions": "eu,uk,us",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "bookmakers": "pinnacle,bet365,betfair,williamhill",
        }, timeout=15)
        if r.status_code == 200:
            return r.json() or []
    except Exception as e:
        log.debug(f"OddsPapi {sport_key}: {e}")
    return []

# ── Analizador ────────────────────────────────────────────────────────────────

def _analizar(ev, meta, market_key, es_vivo=False, oddspapi_eventos=None):
    value_picks, sure_picks = [], []
    home, away   = ev["home_team"], ev["away_team"]
    commence     = ev.get("commence_time", "")
    bookmakers   = list(ev.get("bookmakers", []))
    if not bookmakers: return [], []

    # Enriquecer con datos de OddsPapi si están disponibles (Pinnacle real)
    if oddspapi_eventos:
        for op_ev in oddspapi_eventos:
            if (op_ev.get("home_team","").lower() == home.lower() and
                op_ev.get("away_team","").lower() == away.lower()):
                for bm in op_ev.get("bookmakers", []):
                    if bm.get("key") == "pinnacle":
                        # Agregar Pinnacle real si no está
                        if not any(b.get("key") == "pinnacle" for b in bookmakers):
                            bookmakers.append(bm)
                            log.debug(f"Pinnacle enriquecido: {home} vs {away}")
                break

    horas       = horas_hasta(commence)
    hora_local  = format_hora(commence, horas)
    mercado_lbl = MARKET_LABELS.get(market_key, market_key)
    min_edge_ok = MIN_EDGE_VIVO if es_vivo else MIN_EDGE
    tiene_pinnacle = any(b.get("key") == "pinnacle" for b in bookmakers)

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
        mejor, _ = best_odds(bookmakers, outcome_name, market_key)
        if mejor <= 1.10: continue

        ctx    = detect_context(outcome_name, meta["deporte"])
        penali = ctx.get("penalizacion", 0.0)
        p_mod  = calc_modelo_prob(p_pinn, p_cons, penali, n_casas)

        # Sure Pick
        if not es_vivo and p_mod >= MIN_SURE_PROB and not ctx.get("descartar") and 1.10 <= mejor <= 5.0:
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

        # Límite edge anómalo — más permisivo con Pinnacle
        limite  = 0.60 if tiene_pinnacle else 0.40
        anomalo = edge > limite

        # Categoría por cuota
        if mejor <= ODDS_SEGURO_MAX:
            categoria = "seguro"
        elif mejor <= ODDS_ALTO_MAX:
            categoria = "alto_valor"
        else:
            categoria = "especulativo"

        desc  = ctx.get("descartar",False) or edge < min_edge_ok or anomalo or mejor < 1.15
        razon = None
        if ctx.get("descartar"):    razon = ctx["descripcion"]
        elif anomalo:               razon = f"Edge {edge*100:.1f}% anómalo"
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

    # Pre-cargar datos de OddsPapi para los sports principales
    oddspapi_cache = {}
    if ODDSPAPI_KEY:
        for sport_key in ["baseball_mlb", "basketball_nba", "icehockey_nhl", "soccer_epl"]:
            datos = get_oddspapi_bookmakers(sport_key)
            if datos:
                oddspapi_cache[sport_key] = datos
                log.info(f"OddsPapi {sport_key}: {len(datos)} eventos con Pinnacle")
            time.sleep(0.2)

    for sport_key, meta in SPORTS_ACTIVE.items():
        tipo_sport  = meta.get("tipo", "soccer")
        markets     = MARKETS_BY_SPORT.get(tipo_sport, ["h2h"])
        markets_str = ",".join(markets)
        op_eventos  = oddspapi_cache.get(sport_key, [])

        try:
            r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey": API_KEY, "regions": "eu,uk,us,au",
                "markets": markets_str, "oddsFormat": "decimal",
            }, timeout=20)
            if r.status_code == 422: continue
            r.raise_for_status()
            eventos = r.json()
            if not eventos: continue

            remaining = r.headers.get("x-requests-remaining","?")
            log.info(f"{meta['nombre']}: {len(eventos)} eventos · {remaining} requests restantes")

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
    # Priorizar "seguro" (@1.30-@2.10) con Pinnacle confirmado
    candidatos_seguros   = [p for p in all_value if not p.descartado and p.categoria == "seguro"]
    candidatos_alto      = [p for p in all_value if not p.descartado and p.categoria == "alto_valor"]
    candidatos_especul   = [p for p in all_value if not p.descartado and p.categoria == "especulativo"]

    # Marcar Gold: primero seguros, luego alto valor, completar con especulativos
    gold_target = MAX_GOLD_TIPS
    seleccionados = []
    seen_ev = set()

    for pool in [candidatos_seguros, candidatos_alto, candidatos_especul]:
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
    }
