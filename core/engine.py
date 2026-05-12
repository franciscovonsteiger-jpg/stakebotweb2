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
MIN_EDGE_VIVO = 0.08
MAX_GOLD_TIPS = int(os.getenv("MAX_GOLD_TIPS", 5))
VENTANA_HORAS = int(os.getenv("VENTANA_HORAS", 48))
MIN_SURE_PROB = float(os.getenv("MIN_SURE_PROB", 0.85))
TZ_OFFSET    = int(os.getenv("TZ_OFFSET", -3))   # Offset horario local (ej: -3 Argentina, -5 Colombia/Peru)
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
    # ── Fútbol Europa ──────────────────────────────────────────────────────────────
    "soccer_epl":                {"nombre": "Premier League",      "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_spain_la_liga":      {"nombre": "La Liga",             "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_germany_bundesliga": {"nombre": "Bundesliga",          "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_italy_serie_a":      {"nombre": "Serie A",             "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_uefa_champs_league": {"nombre": "Champions League",    "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_uefa_europa_league": {"nombre": "Europa League",       "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_france_ligue_one":   {"nombre": "Ligue 1",             "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_netherlands_eredivisie": {"nombre": "Eredivisie",      "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_portugal_primeira_liga": {"nombre": "Primeira Liga",   "deporte": "Fútbol", "tipo": "soccer"},
    # ── Fútbol Sudamérica (horario conveniente Argentina) ───────────────────────
    "soccer_conmebol_copa_libertadores":     {"nombre": "Copa Libertadores", "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_conmebol_copa_sudamericana":     {"nombre": "Copa Sudamericana",  "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_argentina_primera_division":     {"nombre": "Liga Argentina",     "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_brazil_campeonato":              {"nombre": "Brasileirão",        "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_chile_primera_division":         {"nombre": "Primera Chile",      "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_mexico_ligamx":                  {"nombre": "Liga MX",            "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_usa_mls":                        {"nombre": "MLS",                "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_brazil_serie_b":                 {"nombre": "Brasileirão B",      "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_colombia_primera_a":             {"nombre": "Liga Colombia",      "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_peru_primera_division":          {"nombre": "Liga Perú",          "deporte": "Fútbol", "tipo": "soccer"},
    "soccer_uruguay_primera_division":       {"nombre": "Liga Uruguay",       "deporte": "Fútbol", "tipo": "soccer"},
    # ── Tenis ───────────────────────────────────────────────────────────────────
    "tennis_atp_french_open":    {"nombre": "ATP Roland Garros",   "deporte": "Tenis",  "tipo": "tennis"},
    "tennis_wta_french_open":    {"nombre": "WTA Roland Garros",   "deporte": "Tenis",  "tipo": "tennis"},
    "tennis_atp":                {"nombre": "ATP Tour",             "deporte": "Tenis",  "tipo": "tennis"},
    "tennis_wta":                {"nombre": "WTA Tour",             "deporte": "Tenis",  "tipo": "tennis"},
    # ── Básquet ─────────────────────────────────────────────────────────────────
    "basketball_nba":            {"nombre": "NBA",                  "deporte": "Básquet","tipo": "basketball"},
    # ── Béisbol ─────────────────────────────────────────────────────────────────
    "baseball_mlb":              {"nombre": "MLB",                  "deporte": "Béisbol","tipo": "baseball"},
    # ── MMA ─────────────────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":    {"nombre": "MMA/UFC",              "deporte": "MMA",    "tipo": "mma"},
    # ── Esports ─────────────────────────────────────────────────────────────────
    "esports_lol":               {"nombre": "LoL LCK/LEC",          "deporte": "Esports","tipo": "esports"},
    "esports_csgo":              {"nombre": "CS2 Pro League",       "deporte": "Esports","tipo": "esports"},
}

CONTEXT_RULES = [
    {"id":"champion_early","descripcion":"Campeón anticipado","penalizacion":0.22,"descartar":True,
     "equipos":["Bayern Munich","FC Bayern","Bayern München"]},
    {"id":"relegated","descripcion":"Equipo ya descendido","penalizacion":0.20,"descartar":True,"equipos":[]},
    {"id":"esport","descripcion":"Esport — Kelly reducido","penalizacion":0.0,"descartar":False,
     "kelly_override":0.25,"max_stake_override":0.02,"deportes":["Esports"]},
]

MARKET_LABELS = {
    "h2h":"Resultado (1X2)","btts":"Ambos anotan",
    "totals":"Over/Under","spreads":"Hándicap","sets":"Sets totales",
}

@dataclass
class ValuePick:
    id: str; tipo: str; evento: str; deporte: str; liga: str
    mercado: str; equipo_pick: str; odds_ref: float
    prob_ajustada: float; edge: float; gold_score: float
    stake_usd: float; ganancia_pot: float; roi_diario_pct: float
    es_gold: bool; es_vivo: bool
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
        if horas < 0:    cuando = f"En curso"
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
    p_base = 0.60 * p_pinn + 0.40 * p_cons if p_pinn else p_cons
    bonus  = min(n_casas / 8, 1.0) * 0.02
    return max(0.01, min(0.99, p_base + bonus - penali))

def nivel_label(prob: float) -> str:
    if prob >= 0.92: return "EXTREMA"
    if prob >= 0.89: return "MUY ALTA"
    return "ALTA"

def señales_texto(p_pinn, p_cons, n_casas, horas) -> str:
    s = []
    if p_pinn and p_pinn > 0.82: s.append(f"Pinnacle confirma {p_pinn*100:.0f}%")
    if n_casas >= 8:              s.append(f"{n_casas} casas cubren el evento")
    if 0 < horas <= 6:            s.append("Menos de 6hs — odds estables")
    if p_pinn and abs(p_pinn - p_cons) < 0.03: s.append("Consensus concentrado")
    return " · ".join(s) if s else f"Consensus de {n_casas} casas"

def kelly_stake(prob, odds, kf=None, maxp=None) -> float:
    kf = kf or KELLY_FRAC; maxp = maxp or MAX_STAKE_PCT
    b = odds - 1
    k = max(0.0, (b * prob - (1-prob)) / b)
    return round(BANKROLL * min(k * kf, maxp), 2)

def gold_score_fn(prob, odds, edge, horas) -> float:
    score = prob * (odds - 1) * edge
    if 1.60 <= odds <= 2.50: score *= 1.2
    elif odds > 4.0:          score *= 0.6
    elif odds < 1.30:         score *= 0.7
    if 0 < horas <= 6:        score *= 1.15
    elif horas <= 12:         score *= 1.05
    return round(score, 6)

def _analizar(ev, meta, market_key, es_vivo=False):
    value_picks, sure_picks = [], []
    home, away  = ev["home_team"], ev["away_team"]
    commence    = ev.get("commence_time", "")
    bookmakers  = ev.get("bookmakers", [])
    if not bookmakers: return [], []

    horas       = horas_hasta(commence)
    hora_local  = format_hora(commence, horas)
    mercado_lbl = MARKET_LABELS.get(market_key, market_key)
    min_edge_ok = MIN_EDGE_VIVO if es_vivo else MIN_EDGE

    outcomes_set = set()
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt["outcomes"]:
                    outcomes_set.add(out["name"])

    # Enriquecer outcomes con punto para mercados Over/Under y Hándicap
    def enriquecer_outcome(outcome_name, mkt_outcomes):
        """Agrega el punto al nombre si es Over/Under o hándicap."""
        for o in mkt_outcomes:
            if o.get("name") == outcome_name:
                punto = o.get("point") or o.get("handicap")
                if punto is not None:
                    if outcome_name in ("Over", "Under"):
                        return f"{outcome_name} {punto}"
                    elif outcome_name not in ("Yes", "No"):
                        return f"{outcome_name} ({punto:+.1f})" if punto != 0 else outcome_name
        return outcome_name

    # Obtener outcomes con punto enriquecido
    outcomes_con_punto = {}
    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            if mkt["key"] == market_key:
                for out in mkt["outcomes"]:
                    nombre_orig = out["name"]
                    nombre_rico = enriquecer_outcome(nombre_orig, mkt["outcomes"])
                    outcomes_con_punto[nombre_orig] = nombre_rico

    for outcome_name in outcomes_set:
        outcome_display = outcomes_con_punto.get(outcome_name, outcome_name)
        p_pinn = prob_pinnacle(bookmakers, outcome_name, market_key)
        p_cons = prob_consensus(bookmakers, outcome_name, market_key)
        if p_cons is None: continue

        n_casas = odds_count(bookmakers, outcome_name, market_key)
        mejor, _ = best_odds(bookmakers, outcome_name, market_key)
        if mejor <= 1.05: continue

        ctx    = detect_context(outcome_name, meta["deporte"])
        penali = ctx.get("penalizacion", 0.0)
        p_mod  = calc_modelo_prob(p_pinn, p_cons, penali, n_casas)

        # Sure Pick — solo pre-partido, prob ≥ 85%, cuota razonable
        if not es_vivo and p_mod >= MIN_SURE_PROB and not ctx.get("descartar") and 1.10 <= mejor <= 5.0:
            stake_s = kelly_stake(p_mod, mejor, KELLY_FRAC, 0.08)
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
                señales=señales_texto(p_pinn, p_cons, n_casas, horas),
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
        gscore  = gold_score_fn(p_mod, mejor, edge, horas)
        # Límite más permisivo para spreads béisbol con cuotas altas
        limite = 0.55 if market_key in ("spreads","sets") and mejor_odds > 2.80 else 0.40
        anomalo = edge > limite

        desc  = ctx.get("descartar",False) or edge < min_edge_ok or anomalo or mejor < 1.10
        razon = None
        if ctx.get("descartar"):     razon = ctx["descripcion"]
        elif anomalo:                razon = f"Edge {edge*100:.1f}% anómalo"
        elif mejor < 1.10:           razon = "Cuota muy baja"
        elif edge < min_edge_ok:     razon = f"Edge {edge*100:.1f}% bajo mínimo"

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
            contexto_id=ctx["id"], contexto_desc=ctx["descripcion"],
            descartado=desc, razon_descarte=razon,
            hora_local=hora_local, horas_para_inicio=round(horas,1),
        ))

    return value_picks, sure_picks

def escanear_mercado(bankroll_usuario: float = None) -> dict:
    if not API_KEY:
        raise ValueError("Sin API key configurada")
    # Usar bankroll del usuario si se pasa, sino el del env
    global BANKROLL
    if bankroll_usuario and bankroll_usuario > 0:
        BANKROLL = bankroll_usuario

    all_value:   list[ValuePick] = []
    all_sure:    list[SurePick]  = []
    all_vivo:    list[ValuePick] = []
    descartados: list[ValuePick] = []
    en_curso = []
    total = 0

    for sport_key, meta in SPORTS_ACTIVE.items():
        tipo_sport  = meta.get("tipo", "soccer")
        markets     = MARKETS_BY_SPORT.get(tipo_sport, ["h2h"])
        markets_str = ",".join(markets)

        try:
            r = requests.get(f"{BASE_URL}/sports/{sport_key}/odds/", params={
                "apiKey": API_KEY, "regions": "eu,uk,us,au",
                "markets": markets_str, "oddsFormat": "decimal",
            }, timeout=20)
            if r.status_code == 422: continue
            r.raise_for_status()
            eventos = r.json()
            if not eventos: continue

            log.info(f"{meta['nombre']}: {len(eventos)} eventos · {r.headers.get('x-requests-remaining','?')} requests")

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
                        vp, _ = _analizar(ev, meta, mk, es_vivo=True)
                        all_vivo.extend(p for p in vp if not p.descartado)
                elif horas <= VENTANA_HORAS:
                    for mk in markets:
                        vp, sp = _analizar(ev, meta, mk, es_vivo=False)
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

    # ── Sure Picks — top por confianza, deduplicado por evento+mercado ─────────
    all_sure.sort(key=lambda s: s.prob_modelo, reverse=True)
    seen, sure_ok = set(), []
    for s in all_sure:
        key = f"{s.evento}-{s.mercado}"
        if key not in seen:
            seen.add(key); sure_ok.append(s)
    all_sure = sure_ok[:10]  # máximo 10 sure picks

    # ── Value Picks — solo Gold Tips (top 5-10) ────────────────────────────────
    all_value.sort(key=lambda p: p.gold_score, reverse=True)
    candidatos = [p for p in all_value if p.edge >= 0.05 and 1.30 <= p.odds_ref <= 3.50]
    for i, p in enumerate(candidatos):
        if i < MAX_GOLD_TIPS: p.es_gold = True

    gold_picks = [p for p in all_value if p.es_gold]  # Solo los Gold Tips

    # ── En vivo — top 5 ────────────────────────────────────────────────────────
    all_vivo.sort(key=lambda p: p.gold_score, reverse=True)
    vivo_top = all_vivo[:5]

    roi_gold = round(sum(p.ganancia_pot for p in gold_picks) / BANKROLL * 100, 2) if BANKROLL else 0
    roi_sure = round(sum(s.ganancia_pot for s in all_sure[:5]) / BANKROLL * 100, 2) if BANKROLL else 0

    log.info(f"Scan OK — {len(gold_picks)} Gold · {len(all_sure)} Sure · {len(vivo_top)} Vivo · {len(en_curso)} en curso")

    return {
        "timestamp":          datetime.now().isoformat(),
        "total_eventos":      total,
        "ventana_horas":      VENTANA_HORAS,
        "picks_validos":      [asdict(p) for p in gold_picks],   # Solo Gold Tips
        "picks_descartados":  [asdict(p) for p in descartados],
        "gold_tips":          [asdict(p) for p in gold_picks],
        "sure_bets":          [asdict(s) for s in all_sure],
        "roi_gold_potencial": roi_gold,
        "expo_gold_usd":      round(sum(p.stake_usd for p in gold_picks), 2),
        "roi_sure_potencial": roi_sure,
        "picks_vivo":         [asdict(p) for p in vivo_top],
        "en_curso":           en_curso[:20],
        "bankroll":           BANKROLL,
    }
