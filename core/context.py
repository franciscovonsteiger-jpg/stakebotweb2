import os, requests, logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("stakebot.context")

API_SPORTS_KEY = os.getenv("API_SPORTS_KEY", "")
BASE = "https://v3.football.api-sports.io"
BASE_TENNIS = "https://v1.tennis.api-sports.io"
BASE_BASEBALL = "https://v2.baseball.api-sports.io"
BASE_BASKETBALL = "https://v2.basketball.api-sports.io"

_cache = {}
CACHE_TTL = 3600  # 1 hora

def _headers():
    return {"x-apisports-key": API_SPORTS_KEY}

def _get(url, params=None, base=None):
    """Request con cache."""
    key = url + str(params)
    now = datetime.now().timestamp()
    if key in _cache and now - _cache[key]["ts"] < CACHE_TTL:
        return _cache[key]["data"]
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=10)
        if r.status_code == 200:
            data = r.json().get("response", [])
            _cache[key] = {"data": data, "ts": now}
            return data
    except Exception as e:
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
    """Stats de un tenista, opcionalmente filtradas por superficie."""
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
    """Head to head entre dos tenistas."""
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
    """Obtiene el ranking ATP/WTA de un jugador."""
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

def ajustar_prob_con_contexto(p_base: float, contexto: dict) -> tuple[float, list]:
    """
    Ajusta la probabilidad del modelo con datos de contexto real.
    Retorna (prob_ajustada, lista_de_señales)
    """
    prob = p_base
    señales = []

    # Forma reciente
    win_rate = contexto.get("win_rate")
    if win_rate is not None:
        if win_rate > 0.70:
            prob = min(0.99, prob * 1.08)
            señales.append(f"🔥 Forma excelente ({win_rate*100:.0f}% wins)")
        elif win_rate < 0.35:
            prob = max(0.01, prob * 0.90)
            señales.append(f"❄️ Mala forma ({win_rate*100:.0f}% wins)")

    # H2H
    h2h_rate = contexto.get("h2h_win_rate")
    if h2h_rate is not None:
        if h2h_rate > 0.70:
            prob = min(0.99, prob * 1.05)
            señales.append(f"📊 H2H favorable ({h2h_rate*100:.0f}%)")
        elif h2h_rate < 0.30:
            prob = max(0.01, prob * 0.95)
            señales.append(f"📊 H2H desfavorable ({h2h_rate*100:.0f}%)")

    # Lesiones clave
    lesiones = contexto.get("lesiones", [])
    if lesiones:
        prob = max(0.01, prob * 0.93)
        señales.append(f"🤕 Lesiones: {', '.join(l['jugador'] for l in lesiones[:2])}")

    # Ranking en tenis
    ranking_diff = contexto.get("ranking_diff")
    if ranking_diff is not None:
        if ranking_diff > 50:   # rival mucho mejor rankeado
            prob = max(0.01, prob * 0.88)
            señales.append(f"⚠️ Diferencia de ranking: {ranking_diff} puestos")
        elif ranking_diff < -50:  # favorito claro
            prob = min(0.99, prob * 1.10)
            señales.append(f"✓ Ranking superior: {abs(ranking_diff)} puestos")

    return round(prob, 4), señales

# ── Función principal de enriquecimiento ─────────────────────────────────────

def enriquecer_evento(home: str, away: str, deporte: str, liga: str) -> dict:
    """
    Enriquece un evento con datos de contexto de API-Sports.
    Retorna diccionario con contexto para ajustar probabilidades.
    """
    if not API_SPORTS_KEY:
        return {}

    ctx = {}
    try:
        if deporte == "Tenis":
            # Ranking de ambos jugadores
            r1 = get_ranking_tenis(home)
            r2 = get_ranking_tenis(away)
            if r1 and r2:
                ctx["ranking_home"] = r1
                ctx["ranking_away"] = r2
                ctx["ranking_diff"] = r2 - r1  # positivo = rival mejor rankeado
                if abs(r1 - r2) > 100:
                    ctx["diferencia_extrema"] = True

    except Exception as e:
        log.warning(f"Contexto {home} vs {away}: {e}")

    return ctx
