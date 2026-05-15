import os, requests, logging, time
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("stakebot.context")

API_SPORTS_KEY = os.getenv("API_SPORTS_KEY", "")
BASE = "https://v3.football.api-sports.io"
# NOTA: API-Sports NO tiene endpoint dedicado de tenis. Lo verificamos en
# la documentación oficial el 15/05/2026. El subdominio v1.tennis.api-sports.io
# devuelve ERR_NAME_NOT_RESOLVED. Para tenis enriquecido necesitaríamos otra
# API (ej: api-tennis.com, allsportsapi). Por ahora deshabilitado.
BASE_TENNIS = None    # ← API no disponible
BASE_BASEBALL = "https://v1.baseball.api-sports.io"  # ← corregido v2→v1
BASE_BASKETBALL = "https://v1.basketball.api-sports.io"
BASE_MMA = "https://v1.mma.api-sports.io"

# ── MODO CONSERVADOR (Plan Free 100 req/día) ─────────────────────────────────
# Cuando MODO_CONSERVADOR=true:
# - Cache de 12hs (en vez de 1h) para minimizar requests
# - Solo enriquecer partidos en próximas 12hs (no todos los 48hs)
# - Solo deporte tenis prioritario (más impacto Roland Garros)
# - Límite duro de 80 req/día para dejar margen (~20 req de seguridad)
# Cuando MODO_CONSERVADOR=false (plan Pro):
# - Cache de 6hs (datos más frescos)
# - Todos los deportes habilitados
# - Sin límite duro (confiamos en el plan)
MODO_CONSERVADOR = os.getenv("MODO_CONSERVADOR", "true").lower() == "true"

CACHE_TTL          = 43200 if MODO_CONSERVADOR else 21600   # 12hs free / 6hs pro
MAX_HORAS_ENRICH   = 12    if MODO_CONSERVADOR else 24      # ventana de enriquecimiento
LIMITE_DIARIO_SOFT = 80    if MODO_CONSERVADOR else 7000    # corte preventivo

# Estado global de uso (visible en logs)
_estado = {
    "requests_dia":    0,
    "fecha_actual":    datetime.now().strftime("%Y-%m-%d"),
    "cache_hits":      0,
    "cache_miss":      0,
    "ultimo_error":    None,
    "cortado":         False,  # True cuando llegamos al límite soft
}

_cache = {}  # key: hash(url+params), value: {"data": [...], "ts": timestamp}


def _check_dia_actual():
    """Reset diario del contador si cambió el día."""
    hoy = datetime.now().strftime("%Y-%m-%d")
    if _estado["fecha_actual"] != hoy:
        log.info(f"API-Sports: nuevo día, reset contador (eran {_estado['requests_dia']} req)")
        _estado["requests_dia"] = 0
        _estado["fecha_actual"] = hoy
        _estado["cortado"] = False


def quota_status() -> dict:
    """Estado del uso de API-Sports (visible en logs/admin)."""
    _check_dia_actual()
    return {
        "modo":           "conservador" if MODO_CONSERVADOR else "pro",
        "requests_hoy":   _estado["requests_dia"],
        "limite_soft":    LIMITE_DIARIO_SOFT,
        "cache_hits":     _estado["cache_hits"],
        "cache_miss":     _estado["cache_miss"],
        "cache_entries":  len(_cache),
        "cortado":        _estado["cortado"],
        "ultimo_error":   _estado["ultimo_error"],
    }


def _headers():
    return {"x-apisports-key": API_SPORTS_KEY}


def _get(url, params=None, base=None):
    """Request con cache + tracking + corte automático.

    Devuelve [] si:
    - No hay API key
    - Llegamos al límite soft del día
    - La request falla
    """
    if not API_SPORTS_KEY:
        return []

    _check_dia_actual()

    key = url + str(sorted((params or {}).items()))
    now = time.time()

    # Cache hit
    if key in _cache and now - _cache[key]["ts"] < CACHE_TTL:
        _estado["cache_hits"] += 1
        return _cache[key]["data"]

    # Corte preventivo: si llegamos al límite soft, no consultar más hoy
    if _estado["requests_dia"] >= LIMITE_DIARIO_SOFT:
        if not _estado["cortado"]:
            log.warning(f"API-Sports: alcanzado límite soft ({LIMITE_DIARIO_SOFT} req/día). Pausa hasta mañana.")
            _estado["cortado"] = True
        return []

    _estado["cache_miss"] += 1
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=10)
        _estado["requests_dia"] += 1

        if r.status_code == 200:
            data = r.json().get("response", [])
            _cache[key] = {"data": data, "ts": now}
            _estado["ultimo_error"] = None
            return data
        elif r.status_code == 429:
            _estado["ultimo_error"] = "RATE_LIMIT"
            _estado["cortado"] = True
            log.error(f"API-Sports: rate limit (429). Pausando hasta mañana.")
        elif r.status_code == 401:
            _estado["ultimo_error"] = "INVALID_KEY"
            log.error("API-Sports: API key inválida (401)")
        else:
            _estado["ultimo_error"] = f"HTTP_{r.status_code}"
            log.warning(f"API-Sports {url}: HTTP {r.status_code}")
    except Exception as e:
        _estado["ultimo_error"] = str(e)[:100]
        log.warning(f"API-Sports error: {e}")
    return []

# ── FÚTBOL ────────────────────────────────────────────────────────────────────

def get_forma_futbol(team_id: int, league_id: int, season: int = 2025) -> dict:
    """Últimos 5 partidos de un equipo — forma reciente."""
    data = _get(f"{BASE}/fixtures", {"team": team_id, "league": league_id,
                                      "season": season, "last": 5})
    if not data:
        return {}
    wins = draws = losses = 0
    goles_favor = goles_contra = 0
    for f in data:
        home_id  = f["teams"]["home"]["id"]
        home_g   = f["goals"]["home"] or 0
        away_g   = f["goals"]["away"] or 0
        es_local = home_id == team_id
        gf = home_g if es_local else away_g
        gc = away_g if es_local else home_g
        goles_favor += gf
        goles_contra += gc
        if gf > gc:   wins += 1
        elif gf == gc: draws += 1
        else:          losses += 1
    total = wins + draws + losses
    return {
        "wins": wins, "draws": draws, "losses": losses,
        "win_rate": round(wins/total, 3) if total else 0,
        "goles_favor_avg": round(goles_favor/total, 2) if total else 0,
        "goles_contra_avg": round(goles_contra/total, 2) if total else 0,
        "forma_str": "W"*wins + "D"*draws + "L"*losses,
    }

def get_h2h_futbol(team1_id: int, team2_id: int, last: int = 5) -> dict:
    """Head to head entre dos equipos."""
    data = _get(f"{BASE}/fixtures/headtohead", {"h2h": f"{team1_id}-{team2_id}", "last": last})
    if not data:
        return {}
    t1_wins = t2_wins = draws = 0
    for f in data:
        home_id = f["teams"]["home"]["id"]
        home_g  = f["goals"]["home"] or 0
        away_g  = f["goals"]["away"] or 0
        if home_g > away_g:
            if home_id == team1_id: t1_wins += 1
            else: t2_wins += 1
        elif home_g < away_g:
            if home_id == team1_id: t2_wins += 1
            else: t1_wins += 1
        else:
            draws += 1
    total = t1_wins + t2_wins + draws
    return {
        "t1_wins": t1_wins, "t2_wins": t2_wins, "draws": draws,
        "t1_win_rate": round(t1_wins/total, 3) if total else 0,
    }

def get_lesiones_futbol(team_id: int, fixture_id: int = None) -> list:
    """Jugadores lesionados o suspendidos."""
    params = {"team": team_id}
    if fixture_id: params["fixture"] = fixture_id
    data = _get(f"{BASE}/injuries", params)
    return [{"jugador": p["player"]["name"], "razon": p["player"]["reason"]}
            for p in data[:5]] if data else []

def buscar_equipo_futbol(nombre: str, league_id: int = None) -> Optional[int]:
    """Busca el ID de un equipo por nombre."""
    params = {"search": nombre}
    if league_id: params["league"] = league_id
    data = _get(f"{BASE}/teams", params)
    return data[0]["team"]["id"] if data else None

# ── TENIS ─────────────────────────────────────────────────────────────────────

SURFACE_MAP = {
    "Roland Garros": "clay", "ATP Roma": "clay", "WTA Roma": "clay",
    "Wimbledon": "grass", "Australian Open": "hard", "US Open": "hard",
}

def get_stats_tenis(player_id: int, surface: str = None) -> dict:
    """Stats de un tenista. DISABLED: API-Sports no tiene endpoint de tenis."""
    if not BASE_TENNIS:
        return {}
    params = {"id": player_id}
    if surface: params["surface"] = surface
    data = _get(f"{BASE_TENNIS}/players/statistics", params)
    if not data: return {}
    s = data[0] if isinstance(data, list) else data
    return {
        "ranking":      s.get("ranking"),
        "win_rate":     round(s.get("wins",0) / max(s.get("wins",0)+s.get("losses",0),1), 3),
        "wins":         s.get("wins", 0),
        "losses":       s.get("losses", 0),
        "surface":      surface,
    }

def get_h2h_tenis(p1_id: int, p2_id: int) -> dict:
    """Head to head entre dos tenistas. DISABLED: API-Sports no tiene tenis."""
    if not BASE_TENNIS:
        return {}
    data = _get(f"{BASE_TENNIS}/players/headtohead", {"h2h": f"{p1_id}-{p2_id}"})
    if not data: return {}
    p1_wins = p2_wins = 0
    for m in data:
        winner = m.get("winner", {}).get("id")
        if winner == p1_id: p1_wins += 1
        elif winner == p2_id: p2_wins += 1
    total = p1_wins + p2_wins
    return {
        "p1_wins": p1_wins, "p2_wins": p2_wins,
        "p1_win_rate": round(p1_wins/total, 3) if total else 0.5,
    }

def get_ranking_tenis(player_name: str) -> Optional[int]:
    """Obtiene el ranking ATP/WTA de un jugador.
    DISABLED: API-Sports no tiene endpoint de tenis."""
    if not BASE_TENNIS:
        return None
    data = _get(f"{BASE_TENNIS}/players", {"search": player_name})
    if not data: return None
    return data[0].get("ranking")

# ── BÉISBOL ───────────────────────────────────────────────────────────────────

def get_stats_pitcher(team_id: int, game_id: int = None) -> dict:
    """Stats del pitcher probable — clave en béisbol."""
    params = {"team": team_id, "season": 2026}
    if game_id: params["game"] = game_id
    data = _get(f"{BASE_BASEBALL}/games/statistics", params)
    if not data: return {}
    return {
        "era":   data[0].get("statistics", [{}])[0].get("era"),
        "whip":  data[0].get("statistics", [{}])[0].get("whip"),
        "wins":  data[0].get("statistics", [{}])[0].get("wins"),
    }

def get_forma_beisbol(team_id: int, last: int = 10) -> dict:
    """Últimos partidos de un equipo MLB."""
    data = _get(f"{BASE_BASEBALL}/games", {"team": team_id, "season": 2026, "last": last})
    if not data: return {}
    wins = losses = runs_favor = runs_contra = 0
    for g in data:
        home_id  = g["teams"]["home"]["id"]
        home_r   = g["scores"]["home"]["total"] or 0
        away_r   = g["scores"]["away"]["total"] or 0
        es_local = home_id == team_id
        rf = home_r if es_local else away_r
        rc = away_r if es_local else home_r
        runs_favor += rf
        runs_contra += rc
        if rf > rc: wins += 1
        else: losses += 1
    total = wins + losses
    return {
        "wins": wins, "losses": losses,
        "win_rate": round(wins/total, 3) if total else 0,
        "runs_favor_avg": round(runs_favor/total, 2) if total else 0,
        "runs_contra_avg": round(runs_contra/total, 2) if total else 0,
    }

# ── Ajuste de probabilidad con contexto ──────────────────────────────────────

def ajustar_prob_con_contexto(p_base: float, contexto: dict, apostado_es_home: bool = None) -> tuple[float, list]:
    """Ajusta la probabilidad del modelo con datos de contexto real.

    Args:
        p_base: probabilidad base del modelo (0-1)
        contexto: dict de enriquecer_evento()
        apostado_es_home: True si el equipo/jugador apostado es el "home" del
            partido. None si no aplica (ej: Over/Under). Cuando es None,
            los ajustes de forma/h2h NO se aplican (porque no sabemos a quién
            le toca).

    Retorna (prob_ajustada, lista_de_señales).
    Los ajustes son MULTIPLICATIVOS y conservadores (5-12% típicamente)
    para no sobreajustar — el modelo base sigue siendo la fuente principal.
    """
    prob = p_base
    señales = list(contexto.get("señales", []))  # reuso las del enriquecimiento

    # ── Tenis: Ranking (independiente de quién apostado) ──────────────────────
    ranking_diff = contexto.get("ranking_diff")
    if ranking_diff is not None and apostado_es_home is not None:
        # ranking_diff > 0 = away peor rankeado (home favorito)
        # Si apostamos al favorito → ajuste positivo
        # Si apostamos al underdog → ajuste negativo
        if abs(ranking_diff) > 50:
            factor = 0.10 if abs(ranking_diff) < 100 else 0.15
            beneficia_home = ranking_diff > 0
            si_apostamos_favorito = (apostado_es_home == beneficia_home)
            if si_apostamos_favorito:
                prob = min(0.99, prob * (1 + factor))
            else:
                prob = max(0.01, prob * (1 - factor))

    # ── Stats por superficie (tenis) ──────────────────────────────────────────
    wr_h = contexto.get("winrate_surface_home")
    wr_a = contexto.get("winrate_surface_away")
    if wr_h is not None and wr_a is not None and apostado_es_home is not None:
        diff = (wr_h - wr_a) if apostado_es_home else (wr_a - wr_h)
        if diff > 15:  # apostado tiene ≥15% mejor winrate en superficie
            prob = min(0.99, prob * 1.08)
        elif diff < -15:
            prob = max(0.01, prob * 0.92)

    # ── H2H (tenis y fútbol) ──────────────────────────────────────────────────
    h2h_h = contexto.get("h2h_wins_home", 0)
    h2h_a = contexto.get("h2h_wins_away", 0)
    h2h_total = h2h_h + h2h_a
    if h2h_total >= 3 and apostado_es_home is not None:
        wins_apostado = h2h_h if apostado_es_home else h2h_a
        rate = wins_apostado / h2h_total
        if rate > 0.70:
            prob = min(0.99, prob * 1.05)
        elif rate < 0.30:
            prob = max(0.01, prob * 0.95)

    # ── Forma reciente (fútbol y béisbol) ─────────────────────────────────────
    if apostado_es_home is not None:
        forma_apostado = contexto.get("forma_home") if apostado_es_home else contexto.get("forma_away")
        forma_rival    = contexto.get("forma_away") if apostado_es_home else contexto.get("forma_home")
        if forma_apostado is not None and forma_rival is not None:
            diff = forma_apostado - forma_rival
            if diff > 25:  # racha del apostado >> rival
                prob = min(0.99, prob * 1.06)
            elif diff < -25:
                prob = max(0.01, prob * 0.94)

    # ── Pitcher ERA (béisbol) ─────────────────────────────────────────────────
    era_h = contexto.get("pitcher_era_home")
    era_a = contexto.get("pitcher_era_away")
    if era_h is not None and era_a is not None and apostado_es_home is not None:
        era_apostado = era_h if apostado_es_home else era_a
        era_rival    = era_a if apostado_es_home else era_h
        # ERA baja = mejor pitcher. Si nuestro pitcher es 1+ punto mejor → ventaja
        diff = era_rival - era_apostado  # positivo = nuestro pitcher es mejor
        if diff > 1.0:
            prob = min(0.99, prob * 1.10)
        elif diff < -1.0:
            prob = max(0.01, prob * 0.90)

    # ── Lesiones (fútbol) ─────────────────────────────────────────────────────
    if apostado_es_home is not None:
        les_apostado = contexto.get("lesionados_home" if apostado_es_home else "lesionados_away", 0)
        les_rival    = contexto.get("lesionados_away" if apostado_es_home else "lesionados_home", 0)
        if les_apostado >= 3 and les_rival <= 1:
            prob = max(0.01, prob * 0.93)  # nuestro equipo más lesionado
        elif les_rival >= 3 and les_apostado <= 1:
            prob = min(0.99, prob * 1.05)  # rival más lesionado

    return round(prob, 4), señales

# ── Función principal de enriquecimiento (Fase 2.3) ──────────────────────────

def enriquecer_evento(home: str, away: str, deporte: str, liga: str,
                       horas_hasta_inicio: float = 0) -> dict:
    """Enriquece un evento con datos de contexto de API-Sports.

    Retorna dict con campos como:
      - ranking_home, ranking_away, ranking_diff (tenis)
      - winrate_clay_home, winrate_clay_away (tenis sobre arcilla)
      - h2h_wins_home, h2h_wins_away (head-to-head)
      - forma_home, forma_away (% victorias últimos 5 — fútbol/MLB)
      - lesionados_home, lesionados_away (count — fútbol)
      - pitcher_era_home, pitcher_era_away (béisbol)
      - señales: lista de strings descriptivos para mostrar al usuario
      - diferencia_extrema, descartar (flags)
      - tiene_contexto: True si se obtuvo info real

    En MODO_CONSERVADOR solo enriquece partidos en próximas MAX_HORAS_ENRICH
    horas (default 12) para conservar quota de API-Sports.
    """
    if not API_SPORTS_KEY:
        return {}

    # En modo conservador: skip si el partido está lejos (priorizar quota)
    if MODO_CONSERVADOR and horas_hasta_inicio > MAX_HORAS_ENRICH:
        return {}

    ctx = {"señales": [], "tiene_contexto": False}

    try:
        if deporte == "Tenis":
            ctx.update(_enriquecer_tenis(home, away, liga))
        elif deporte == "Béisbol":
            ctx.update(_enriquecer_beisbol(home, away))
        elif deporte == "Fútbol":
            # Fútbol consume mucho — solo si NO estamos en modo conservador
            # o si el partido es muy próximo (próximas 6hs)
            if not MODO_CONSERVADOR or horas_hasta_inicio <= 6:
                ctx.update(_enriquecer_futbol(home, away, liga))
    except Exception as e:
        log.warning(f"Contexto {home} vs {away}: {e}")

    return ctx


def _enriquecer_tenis(home: str, away: str, liga: str) -> dict:
    """Tenis: API-Sports NO tiene endpoint de tenis (verificado 15/05/2026).
    Esta función queda como stub y retorna vacío sin gastar requests.

    TODO Fase 2.3.D: integrar otra API (api-tennis.com, sportsdata.io, etc)
    para tener stats por superficie + h2h + ranking real.

    Por ahora, el resto del sistema sigue funcionando porque:
    - El ranking de tenis ya estaba en código original con fallback graceful
    - Los picks de tenis se siguen generando por The Odds API
    - Solo perdemos las señales contextuales adicionales
    """
    return {"señales": []}


def _enriquecer_tenis_OBSOLETO(home: str, away: str, liga: str) -> dict:
    """OBSOLETO: este código asumía que existía v1.tennis.api-sports.io.
    Lo dejamos comentado por si en el futuro API-Sports lanza tenis o
    queremos migrar a otra API con interfaz similar.
    """
    ctx = {"señales": []}

    if not BASE_TENNIS:  # API de tenis no disponible
        return ctx

    # Detectar superficie según torneo
    liga_lower = (liga or "").lower()
    if "roland" in liga_lower or "french" in liga_lower or "monte carlo" in liga_lower or "roma" in liga_lower or "madrid" in liga_lower:
        superficie = "Clay"
    elif "wimbledon" in liga_lower:
        superficie = "Grass"
    elif "us open" in liga_lower or "australian" in liga_lower:
        superficie = "Hard"
    else:
        superficie = None

    # Rankings
    r1 = get_ranking_tenis(home)
    r2 = get_ranking_tenis(away)
    if r1 and r2:
        ctx["ranking_home"] = r1
        ctx["ranking_away"] = r2
        ctx["ranking_diff"] = r2 - r1   # positivo = rival peor rankeado
        ctx["tiene_contexto"] = True

        if abs(r1 - r2) > 100:
            ctx["diferencia_extrema"] = True
            ctx["señales"].append(f"⚠️ Diferencia de ranking extrema ({abs(r1-r2)} pos)")
        elif r1 < r2 - 30:
            ctx["señales"].append(f"✓ {home} mejor rankeado ({r1} vs {r2})")
        elif r2 < r1 - 30:
            ctx["señales"].append(f"✓ {away} mejor rankeado ({r2} vs {r1})")

    # Stats por superficie (solo si encontramos los IDs y hay superficie)
    if superficie and BASE_TENNIS:
        # Reusamos la búsqueda de ranking para obtener el player_id
        p1_data = _get(f"{BASE_TENNIS}/players", {"search": home})
        p2_data = _get(f"{BASE_TENNIS}/players", {"search": away})
        p1_id = p1_data[0].get("id") if p1_data else None
        p2_id = p2_data[0].get("id") if p2_data else None

        if p1_id:
            stats_p1 = get_stats_tenis(p1_id, superficie)
            if stats_p1.get("matches", 0) > 5:
                ctx["winrate_surface_home"] = stats_p1["winrate"]
                if stats_p1["winrate"] > 65:
                    ctx["señales"].append(f"🎾 {home} fuerte en {superficie} ({stats_p1['winrate']}%)")
                elif stats_p1["winrate"] < 40:
                    ctx["señales"].append(f"⚠️ {home} débil en {superficie} ({stats_p1['winrate']}%)")

        if p2_id:
            stats_p2 = get_stats_tenis(p2_id, superficie)
            if stats_p2.get("matches", 0) > 5:
                ctx["winrate_surface_away"] = stats_p2["winrate"]
                if stats_p2["winrate"] > 65:
                    ctx["señales"].append(f"🎾 {away} fuerte en {superficie} ({stats_p2['winrate']}%)")
                elif stats_p2["winrate"] < 40:
                    ctx["señales"].append(f"⚠️ {away} débil en {superficie} ({stats_p2['winrate']}%)")

        # H2H (solo si ya conseguimos ambos IDs)
        if p1_id and p2_id:
            h2h = get_h2h_tenis(p1_id, p2_id)
            if h2h.get("total", 0) >= 2:
                wins_h = h2h.get("wins_p1", 0)
                wins_a = h2h.get("wins_p2", 0)
                ctx["h2h_wins_home"] = wins_h
                ctx["h2h_wins_away"] = wins_a
                if wins_h > wins_a * 2 and wins_h >= 3:
                    ctx["señales"].append(f"💪 H2H favorable a {home} ({wins_h}-{wins_a})")
                elif wins_a > wins_h * 2 and wins_a >= 3:
                    ctx["señales"].append(f"💪 H2H favorable a {away} ({wins_a}-{wins_h})")

    return ctx


def _enriquecer_beisbol(home: str, away: str) -> dict:
    """Béisbol: pitcher data + forma reciente.

    Consumo: ~4 requests por partido. Si hay pitcher data confiable,
    el sistema permite MLB h2h (hoy bloqueado por falta de info).
    """
    ctx = {"señales": []}

    # Buscar IDs de equipos (MLB league=1 en API-Sports)
    home_data = _get(f"{BASE_BASEBALL}/teams", {"search": home})
    away_data = _get(f"{BASE_BASEBALL}/teams", {"search": away})
    home_id = home_data[0].get("id") if home_data else None
    away_id = away_data[0].get("id") if away_data else None

    if not (home_id and away_id):
        return ctx

    # Forma últimos 10 partidos
    forma_h = get_forma_beisbol(home_id, last=10)
    forma_a = get_forma_beisbol(away_id, last=10)

    if forma_h.get("partidos", 0) >= 5:
        ctx["forma_home"] = forma_h["winrate"]
        ctx["tiene_contexto"] = True
        if forma_h["winrate"] > 70:
            ctx["señales"].append(f"🔥 {home} en racha ({forma_h['wins']}/{forma_h['partidos']})")
        elif forma_h["winrate"] < 30:
            ctx["señales"].append(f"📉 {home} mala racha ({forma_h['wins']}/{forma_h['partidos']})")

    if forma_a.get("partidos", 0) >= 5:
        ctx["forma_away"] = forma_a["winrate"]
        ctx["tiene_contexto"] = True
        if forma_a["winrate"] > 70:
            ctx["señales"].append(f"🔥 {away} en racha ({forma_a['wins']}/{forma_a['partidos']})")
        elif forma_a["winrate"] < 30:
            ctx["señales"].append(f"📉 {away} mala racha ({forma_a['wins']}/{forma_a['partidos']})")

    # Pitcher data (próximo abridor) — clave para MLB h2h
    pitcher_h = get_stats_pitcher(home_id)
    pitcher_a = get_stats_pitcher(away_id)

    # Marcar que tenemos pitcher data (engine usa esto para permitir MLB h2h)
    if pitcher_h.get("era") is not None or pitcher_a.get("era") is not None:
        ctx["tiene_pitcher_data"] = True
        ctx["tiene_contexto"] = True

        if pitcher_h.get("era") is not None:
            era = pitcher_h["era"]
            ctx["pitcher_era_home"] = era
            if era < 3.0:
                ctx["señales"].append(f"⚾ Pitcher {home} elite (ERA {era})")
            elif era > 5.0:
                ctx["señales"].append(f"⚠️ Pitcher {home} flojo (ERA {era})")

        if pitcher_a.get("era") is not None:
            era = pitcher_a["era"]
            ctx["pitcher_era_away"] = era
            if era < 3.0:
                ctx["señales"].append(f"⚾ Pitcher {away} elite (ERA {era})")
            elif era > 5.0:
                ctx["señales"].append(f"⚠️ Pitcher {away} flojo (ERA {era})")

    return ctx


# Mapeo de ligas de The Odds API → league_id de API-Sports (fútbol)
LEAGUE_ID_MAP = {
    "EPL": 39, "Premier League": 39,
    "La Liga": 140, "LaLiga": 140,
    "Serie A - Italy": 135, "Serie A": 135,
    "Bundesliga - Germany": 78, "Bundesliga": 78,
    "Ligue 1 - France": 61, "Ligue 1": 61,
    "Primera División - Argentina": 128, "Primera División": 128,
    "Brazil Série A": 71, "Série A": 71,
    "UEFA Champions League": 2, "Champions League": 2,
    "UEFA Europa League": 3, "Europa League": 3,
    "UEFA Europa Conference League": 848, "Conference League": 848,
    "MLS": 253,
    "Primeira Liga - Portugal": 94, "Primeira Liga": 94,
    "Dutch Eredivisie": 88, "Eredivisie": 88,
    "Copa Libertadores": 13,
    "Copa Sudamericana": 11,
    "Liga MX": 262,
}


def _enriquecer_futbol(home: str, away: str, liga: str) -> dict:
    """Fútbol: forma + h2h + lesiones.

    Consumo MUY ALTO (~7 req/partido). Solo se llama en modo no-conservador
    o en partidos muy próximos (≤6hs).

    Filtra por ligas mapeadas: si la liga no está en LEAGUE_ID_MAP, retorna
    vacío para no gastar requests en ligas menores no soportadas.
    """
    ctx = {"señales": []}

    league_id = None
    for k, v in LEAGUE_ID_MAP.items():
        if k.lower() in (liga or "").lower():
            league_id = v
            break
    if not league_id:
        return ctx  # Liga no mapeada: no enriquecemos

    # Temporada actual (heurística simple: 2025-2026 = 2025)
    from datetime import datetime as _dt
    now = _dt.now()
    # Si es jul-dic, temporada actual = año. Si ene-jun, temporada = año-1
    season = now.year if now.month >= 7 else now.year - 1

    # IDs de equipos
    home_id = buscar_equipo_futbol(home, league_id)
    away_id = buscar_equipo_futbol(away, league_id)

    if not (home_id and away_id):
        return ctx

    ctx["tiene_contexto"] = True

    # Forma
    forma_h = get_forma_futbol(home_id, league_id, season)
    forma_a = get_forma_futbol(away_id, league_id, season)

    if forma_h.get("partidos", 0) >= 3:
        ctx["forma_home"] = forma_h["winrate"]
        if forma_h["winrate"] > 65:
            ctx["señales"].append(f"🔥 {home} en racha ({forma_h['wins']}V-{forma_h['draws']}E-{forma_h['losses']}D)")
        elif forma_h["winrate"] < 25:
            ctx["señales"].append(f"📉 {home} mala forma ({forma_h['wins']}V-{forma_h['draws']}E-{forma_h['losses']}D)")

    if forma_a.get("partidos", 0) >= 3:
        ctx["forma_away"] = forma_a["winrate"]
        if forma_a["winrate"] > 65:
            ctx["señales"].append(f"🔥 {away} en racha")
        elif forma_a["winrate"] < 25:
            ctx["señales"].append(f"📉 {away} mala forma")

    # H2H
    h2h = get_h2h_futbol(home_id, away_id, last=5)
    if h2h.get("total", 0) >= 3:
        ctx["h2h_wins_home"] = h2h.get("wins_home", 0)
        ctx["h2h_wins_away"] = h2h.get("wins_away", 0)
        if h2h.get("wins_home", 0) >= 3:
            ctx["señales"].append(f"💪 H2H domina {home} ({h2h['wins_home']}/{h2h['total']})")
        elif h2h.get("wins_away", 0) >= 3:
            ctx["señales"].append(f"💪 H2H domina {away} ({h2h['wins_away']}/{h2h['total']})")

    # Lesiones (solo home y away — 2 requests). En modo conservador: skip
    if not MODO_CONSERVADOR:
        les_h = get_lesiones_futbol(home_id)
        les_a = get_lesiones_futbol(away_id)
        n_h = len(les_h)
        n_a = len(les_a)
        if n_h >= 3:
            ctx["lesionados_home"] = n_h
            ctx["señales"].append(f"🤕 {home}: {n_h} lesionados")
        if n_a >= 3:
            ctx["lesionados_away"] = n_a
            ctx["señales"].append(f"🤕 {away}: {n_a} lesionados")

    return ctx
