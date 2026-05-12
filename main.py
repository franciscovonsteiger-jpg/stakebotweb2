import os, asyncio, logging
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stakebot")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MIN", 30)) * 60

cache = {"resultado": None, "ultimo_scan": None, "scanning": False,
         "error": None, "gold_enviados": set()}

async def run_scan_bg():
    if cache["scanning"]: return
    cache["scanning"] = True; cache["error"] = None
    try:
        from core.engine import escanear_mercado
        from core.notifier import notificar_usuarios_premium
        from core.database import get_all_users
        loop = asyncio.get_event_loop()
        # Obtener bankroll del admin para el scan del sistema
        from core.database import get_all_users
        usuarios_scan = await get_all_users()
        admin_user = next((u for u in usuarios_scan if u["plan"] == "admin"), None)
        bankroll_scan = float(admin_user["bankroll"]) if admin_user else float(os.getenv("BANKROLL_USD", 1000000))
        resultado = await loop.run_in_executor(None, lambda: escanear_mercado(bankroll_scan))
        cache["resultado"] = resultado
        cache["ultimo_scan"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        log.info(f"Scan OK — {len(resultado.get('gold_tips',[]))} Gold · {len(resultado.get('sure_bets',[]))} Sure · {len(resultado.get('picks_vivo',[]))} Vivo")
        usuarios = await get_all_users()
        cache["gold_enviados"] = notificar_usuarios_premium(resultado, usuarios, cache["gold_enviados"])
    except Exception as e:
        cache["error"] = str(e); log.error(f"Error scan: {e}")
    finally:
        cache["scanning"] = False

async def scanner_loop():
    while True:
        await run_scan_bg()
        await asyncio.sleep(SCAN_INTERVAL)

async def vencimiento_loop():
    """Verifica vencimientos cada hora y notifica por Telegram."""
    while True:
        await asyncio.sleep(3600)  # cada 1 hora
        try:
            from core.database import verificar_vencimientos
            from core.notifier import notificar_owner, send_message
            result = await verificar_vencimientos()
            TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","")

            # Notificar vencidos
            for u in result.get("vencidos", []):
                log.info(f"Plan vencido: {u['username']}")
                notificar_owner(f"⚠️ Plan vencido: @{u['username']} ({u['email']})")
                if u.get("tg_chat_id") and u.get("tg_activo"):
                    send_message(TG_TOKEN, u["tg_chat_id"],
                        f"⏰ <b>Tu acceso Premium a InvestiaBet venció.</b>\n\n"
                        f"Para renovar y seguir recibiendo Gold Tips y Sure Bets:\n"
                        f"👉 <b>stakebotweb2-production.up.railway.app/premium</b>\n\n"
                        f"Alias Mercado Pago: <b>franvons</b>\n"
                        f"AstroPay: <b>0000177500098073799130</b>\n\n"
                        f"Enviá el comprobante y te reactivamos en minutos 🚀")

            # Notificar por vencer en 2 días
            for u in result.get("por_vencer", []):
                log.info(f"Plan por vencer: {u['username']}")
                if u.get("tg_chat_id") and u.get("tg_activo"):
                    venc = u.get("fecha_vencimiento","")[:10] if u.get("fecha_vencimiento") else "pronto"
                    send_message(TG_TOKEN, u["tg_chat_id"],
                        f"⏳ <b>Tu Premium vence el {venc}</b>\n\n"
                        f"Renovalo ahora para no perder los Gold Tips:\n"
                        f"👉 Alias MP: <b>franvons</b> · AstroPay: <b>0000177500098073799130</b>\n"
                        f"Enviá el comprobante a @Stakegoldia_bot 💪")
        except Exception as e:
            log.error(f"Error vencimiento_loop: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.database import init_db
    await init_db()
    asyncio.create_task(scanner_loop())
    asyncio.create_task(vencimiento_loop())
    yield

app = FastAPI(title="InvestiaBet", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=True)

async def require_auth(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "): token = auth[7:]
    if not token: return None
    from core.database import get_user_by_token
    return await get_user_by_token(token)

def picks_para_usuario(user: dict) -> dict:
    r = cache["resultado"]
    if not r: return {"scanning": True}
    plan     = user["plan"]
    bankroll = float(user.get("bankroll", 1000))
    gold     = r.get("gold_tips", [])
    sure     = r.get("sure_bets", [])
    vivo     = r.get("picks_vivo", [])

    if plan in ("premium", "admin"):
        # Recalcular stake sobre el bankroll real del usuario con Kelly
        MAX_STAKE = 0.05

        def recalc(picks):
            result = []
            for p in picks:
                if p.get("odds_ref"):
                    prob     = p.get("prob_ajustada", 0.5)
                    odds     = p["odds_ref"]
                    b        = odds - 1
                    kelly    = max(0.0, (b * prob - (1 - prob)) / b) if b > 0 else 0
                    pct      = min(kelly * 0.5, MAX_STAKE)
                    stake    = round(bankroll * pct, 2)
                    ganancia = round(stake * b, 2)
                    roi      = round(ganancia / bankroll * 100, 2) if bankroll else 0
                    result.append({**p, "stake_usd": stake,
                                   "ganancia_pot": ganancia, "roi_diario_pct": roi})
                else:
                    result.append(p)
            return result
        gold_vis = recalc(gold); sure_vis = recalc(sure); vivo_vis = recalc(vivo)
    else:
        gold_vis = [{**p, "stake_usd": None, "ganancia_pot": None, "roi_diario_pct": None} for p in gold[:3]]
        sure_vis = sure[:2]; vivo_vis = []

    return {
        "timestamp": r["timestamp"], "ultimo_scan": cache["ultimo_scan"],
        "scanning": cache["scanning"], "total_eventos": r.get("total_eventos", 0),
        "ventana_horas": r.get("ventana_horas", 48),
        "picks_validos": gold_vis, "gold_tips": gold_vis,
        "sure_bets": sure_vis, "picks_vivo": vivo_vis,
        "en_curso": r.get("en_curso", []),
        "roi_gold_potencial": r.get("roi_gold_potencial", 0) if plan in ("premium","admin") else None,
        "expo_gold_usd": r.get("expo_gold_usd", 0) if plan in ("premium","admin") else None,
        "roi_sure_potencial": r.get("roi_sure_potencial", 0) if plan in ("premium","admin") else None,
        "picks_descartados": r.get("picks_descartados", []) if plan in ("premium","admin") else [],
        "bankroll": bankroll, "plan": plan,
        "picks_bloqueados": max(0, len(gold) - 3) if plan == "free" else 0,
    }

# ── API Auth ──────────────────────────────────────────────────────────────────

@app.get("/api/debug/hash")
async def debug_hash():
    import hashlib
    salt = os.getenv("SECRET_KEY", "stakebot_salt_2025")
    return {"salt": salt, "hash": hashlib.sha256(f"{salt}admin1234".encode()).hexdigest()}

@app.post("/api/auth/register")
async def register(request: Request):
    data = await request.json()
    from core.database import crear_usuario
    return JSONResponse(await crear_usuario(
        email=data.get("email",""), username=data.get("username",""),
        password=data.get("password",""), codigo=data.get("codigo",""),
    ))

@app.post("/api/auth/login")
async def do_login(request: Request, response: Response):
    data = await request.json()
    from core.database import login
    result = await login(data.get("email",""), data.get("password",""))
    if result["ok"]:
        token = result["token"]
        resp  = JSONResponse({"ok": True, "plan": result["user"]["plan"],
                              "username": result["user"]["username"], "token": token})
        resp.set_cookie("session_token", token, max_age=30*24*3600,
                        httponly=False, secure=True, samesite="none", path="/")
        return resp
    return JSONResponse(result, status_code=401)

@app.post("/api/auth/logout")
async def do_logout(request: Request, response: Response):
    token = request.cookies.get("session_token","")
    auth  = request.headers.get("Authorization","")
    if not token and auth.startswith("Bearer "): token = auth[7:]
    if token:
        from core.database import logout
        await logout(token)
    response.delete_cookie("session_token")
    return JSONResponse({"ok": True})

@app.get("/api/me")
async def get_me(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok":True,"id":user["id"],"email":user["email"],
        "username":user["username"],"plan":user["plan"],"bankroll":user["bankroll"],
        "moneda":user["moneda"],"perfil_riesgo":user["perfil_riesgo"],
        "tg_chat_id":user["tg_chat_id"],"tg_activo":user["tg_activo"],
        "fecha_vencimiento": user.get("fecha_vencimiento",""),
        "trial_usado": user.get("trial_usado", False)})

@app.post("/api/me/perfil")
async def update_perfil_ep(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    from core.database import update_perfil
    return JSONResponse(await update_perfil(user["id"], data))

@app.post("/api/me/bankroll")
async def ajustar_bankroll_ep(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    from core.database import ajustar_bankroll
    return JSONResponse(await ajustar_bankroll(
        user["id"], float(data.get("monto",0)),
        data.get("tipo","ajuste"), data.get("descripcion","")
    ))

@app.get("/api/picks")
async def get_picks(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok":False,"error":"No autenticado"}, status_code=401)
    return JSONResponse(picks_para_usuario(user))

@app.post("/api/scan")
async def trigger_scan(request: Request, background_tasks: BackgroundTasks):
    user = await require_auth(request)
    if not user or user["plan"] not in ("premium","admin"):
        return JSONResponse({"ok":False}, status_code=403)
    if cache["scanning"]: return JSONResponse({"mensaje":"Scan en progreso"})
    background_tasks.add_task(run_scan_bg)
    return JSONResponse({"mensaje":"Scan iniciado"})

# ── API Picks / Stats ─────────────────────────────────────────────────────────

@app.post("/api/picks/colocar")
async def colocar_pick(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok":False,"error":"No autenticado"}, status_code=401)
    try:
        data = await request.json()
        log.info(f"Guardando pick para user {user['id']}: {data.get('evento','?')} - {data.get('equipo_pick','?')}")
        from core.database import guardar_pick
        result = await guardar_pick(user["id"], data)
        log.info(f"Resultado guardar_pick: {result}")
        return JSONResponse(result)
    except Exception as e:
        log.error(f"Error en colocar_pick: {e}")
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)

@app.post("/api/trial")
async def activar_trial_ep(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok":False}, status_code=401)
    from core.database import activar_trial
    return JSONResponse(await activar_trial(user["id"]))

@app.post("/api/admin/usuario/{user_id}/premium")
async def admin_activar_premium(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import activar_premium
    return JSONResponse(await activar_premium(user_id, data.get("dias",30)))

@app.get("/premium", response_class=HTMLResponse)
async def premium_page(request: Request):
    return HTMLResponse(PREMIUM_HTML)

@app.post("/api/picks/manual")
async def pick_manual(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok":False}, status_code=401)
    try:
        data = await request.json()
        from core.database import guardar_pick_manual
        return JSONResponse(await guardar_pick_manual(user["id"], data))
    except Exception as e:
        log.error(f"Error pick_manual: {e}")
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)

@app.post("/api/picks/{pick_id}/resultado")
async def resultado_pick(pick_id: int, request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok":False}, status_code=401)
    data = await request.json()
    from core.database import actualizar_resultado
    return JSONResponse(await actualizar_resultado(pick_id, user["id"], data))

@app.get("/api/estadisticas")
async def get_stats(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok":False}, status_code=401)
    try:
        from core.database import get_estadisticas
        data = await get_estadisticas(user["id"])
        # Agregar ratio de conversión para que el frontend muestre en moneda correcta
        engine_bankroll = float(os.getenv("BANKROLL_USD", 1000))
        user_bankroll   = float(user.get("bankroll", 1000))
        data["moneda"] = user.get("moneda", "USD")
        data["ratio"]  = 1  # Stake ya calculado con bankroll_engine del usuario
        return JSONResponse(data)
    except Exception as e:
        log.error(f"Error estadisticas: {e}")
        return JSONResponse({
            "bankroll": user.get("bankroll", 1000),
            "moneda": user.get("moneda", "USD"),
            "todo": {"total_colocados":0,"total_resueltos":0,"ganados":0,"perdidos":0,"cashouts":0,"pendientes":0,"win_rate":0,"pnl_total":0,"invertido_total":0,"roi":0,"value_stats":{},"sure_stats":{},"gold_stats":{},"por_deporte":{}},
            "mes": {"total_colocados":0,"total_resueltos":0,"ganados":0,"perdidos":0,"cashouts":0,"pendientes":0,"win_rate":0,"pnl_total":0,"invertido_total":0,"roi":0,"value_stats":{},"sure_stats":{},"gold_stats":{},"por_deporte":{}},
            "pendientes": [],
            "historial": [],
            "bankroll_hist": [],
            "error": str(e)
        })

# ── API Admin ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/usuarios")
async def admin_usuarios(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    from core.database import get_all_users
    return JSONResponse(await get_all_users())

@app.post("/api/admin/usuario/{user_id}/plan")
async def admin_plan(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import set_user_plan
    await set_user_plan(user_id, data["plan"]); return JSONResponse({"ok":True})

@app.post("/api/admin/usuario/{user_id}/activo")
async def admin_activo(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import set_user_activo
    await set_user_activo(user_id, data["activo"]); return JSONResponse({"ok":True})

@app.post("/api/admin/invitacion")
async def admin_inv(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import crear_invitacion
    codigo = await crear_invitacion(plan=data.get("plan","premium"),
                                   max_usos=data.get("max_usos",1), creado_por=user["id"])
    return JSONResponse({"ok":True,"codigo":codigo})

@app.get("/api/admin/invitaciones")
async def admin_invs(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    from core.database import get_invitaciones
    return JSONResponse(await get_invitaciones())

# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = await require_auth(request)
    if not user: return RedirectResponse("/login")
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/login", response_class=HTMLResponse)
async def login_page(): return HTMLResponse(LOGIN_HTML)

@app.get("/estadisticas", response_class=HTMLResponse)
async def stats_page(request: Request):
    user = await require_auth(request)
    if not user: return RedirectResponse("/login")
    return HTMLResponse(STATS_HTML)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return RedirectResponse("/login")
    return HTMLResponse(ADMIN_HTML)

# ── HTML Pages ─────────────────────────────────────────────────────────────────


PREMIUM_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Activar Premium — InvestiaBet</title>
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131929;--border:#1e2a3d;--text:#e2e8f4;--text2:#7a8aaa;--blue:#4f8ef7;--violet:#7c5ff7;--teal:#00d4aa;--amber:#f59e0b;--red:#ef4444;--radius:14px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);background-image:radial-gradient(ellipse at 20% 50%,rgba(79,142,247,.06) 0%,transparent 60%),radial-gradient(ellipse at 80% 20%,rgba(124,95,247,.06) 0%,transparent 60%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding:20px}
.container{max-width:600px;margin:0 auto}
.logo{text-align:center;padding:30px 0 20px;font-size:26px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--violet),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:28px;text-align:center;margin-bottom:20px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--blue),var(--violet),var(--teal))}
.precio{font-size:48px;font-weight:700;color:var(--teal);margin:10px 0 4px}
.precio-sub{font-size:14px;color:var(--text2)}
.features{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:20px 0}
.feat{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text2)}
.feat-icon{color:var(--teal);font-size:16px;flex-shrink:0}
.metodos{margin-bottom:20px}
.metodo{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;overflow:hidden;cursor:pointer;transition:border-color .2s}
.metodo:hover{border-color:var(--teal)}
.metodo.active{border-color:var(--teal)}
.metodo-header{padding:16px 20px;display:flex;align-items:center;justify-content:space-between}
.metodo-name{font-size:15px;font-weight:600;display:flex;align-items:center;gap:10px}
.metodo-body{display:none;padding:0 20px 20px}
.metodo.active .metodo-body{display:block}
.dato-row{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--bg3);border-radius:10px;margin-bottom:8px}
.dato-label{font-size:12px;color:var(--text2)}
.dato-val{font-size:15px;font-weight:600;font-family:monospace;letter-spacing:.5px}
.copy-btn{padding:5px 12px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer;transition:all .15s}
.copy-btn:hover{color:var(--teal);border-color:var(--teal)}
.steps{margin-top:12px}
.step{display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;font-size:13px;color:var(--text2)}
.step-n{width:22px;height:22px;border-radius:50%;background:rgba(0,212,170,.15);color:var(--teal);font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px}
.notice{background:rgba(124,95,247,.08);border:1px solid rgba(124,95,247,.2);border-radius:12px;padding:16px 20px;font-size:13px;color:var(--text2);line-height:1.7;margin-bottom:20px}
.notice strong{color:var(--violet)}
.badge{display:inline-flex;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
.b-teal{background:rgba(0,212,170,.12);color:var(--teal);border:1px solid rgba(0,212,170,.2)}
.b-gray{background:var(--bg3);color:var(--text2)}
.btn-back{display:block;text-align:center;margin-top:20px;padding:12px;border-radius:10px;border:1px solid var(--border);background:var(--bg2);color:var(--text2);font-size:14px;cursor:pointer;text-decoration:none}
.btn-back:hover{border-color:var(--teal);color:var(--teal)}
</style></head><body>
<div class="container">
  <div class="logo">📈 InvestiaBet</div>

  <div class="hero">
    <div style="font-size:14px;color:var(--text2);margin-bottom:8px">Plan Premium — Acceso completo</div>
    <div class="precio">$30 USD</div>
    <div class="precio-sub">por mes · o $15 USD precio fundadores</div>
    <div class="features" style="margin-top:20px;text-align:left">
      <div class="feat"><span class="feat-icon">⭐</span>Gold Tips diarios</div>
      <div class="feat"><span class="feat-icon">🔒</span>Sure Bets ≥85%</div>
      <div class="feat"><span class="feat-icon">📊</span>ROI y stats reales</div>
      <div class="feat"><span class="feat-icon">📨</span>Alertas Telegram</div>
      <div class="feat"><span class="feat-icon">🔴</span>Picks en vivo</div>
      <div class="feat"><span class="feat-icon">♾️</span>Acceso ilimitado</div>
    </div>
  </div>

  <div class="notice">
    <strong>Precio especial fundadores:</strong> $15 USD/mes para los primeros 20 usuarios.<br>
    Una vez que confirmes el pago te activamos el acceso en menos de <strong>1 hora</strong>.
  </div>

  <div style="font-size:13px;color:var(--text2);margin-bottom:12px;font-weight:500">Elegí cómo pagar:</div>

  <div class="metodos">

    <div class="metodo active" onclick="toggle(this)">
      <div class="metodo-header">
        <div class="metodo-name">💙 Mercado Pago <span class="badge b-teal">Recomendado</span></div>
        <span style="color:var(--text2);font-size:18px" id="arr-mp">▲</span>
      </div>
      <div class="metodo-body">
        <div class="dato-row">
          <span class="dato-label">Alias</span>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="dato-val">franvons</span>
            <button class="copy-btn" onclick="copiar('franvons',this)">Copiar</button>
          </div>
        </div>
        <div class="steps">
          <div class="step"><div class="step-n">1</div><span>Abrí Mercado Pago → Enviar dinero</span></div>
          <div class="step"><div class="step-n">2</div><span>Buscá el alias <strong style="color:var(--text)">franvons</strong></span></div>
          <div class="step"><div class="step-n">3</div><span>Enviá el equivalente a $15 USD con asunto <strong style="color:var(--text)">"InvestiaBet Premium"</strong></span></div>
          <div class="step"><div class="step-n">4</div><span>Mandá el comprobante a <strong style="color:var(--teal)">@Stakegoldia_bot</strong> en Telegram</span></div>
        </div>
      </div>
    </div>

    <div class="metodo" onclick="toggle(this)">
      <div class="metodo-header">
        <div class="metodo-name">💜 AstroPay</div>
        <span style="color:var(--text2);font-size:18px" id="arr-ap">▼</span>
      </div>
      <div class="metodo-body">
        <div class="dato-row">
          <span class="dato-label">Número de cuenta</span>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="dato-val" style="font-size:12px">0000177500098073799130</span>
            <button class="copy-btn" onclick="copiar('0000177500098073799130',this)">Copiar</button>
          </div>
        </div>
        <div class="steps">
          <div class="step"><div class="step-n">1</div><span>Abrí tu app de AstroPay</span></div>
          <div class="step"><div class="step-n">2</div><span>Enviá <strong style="color:var(--text)">$15 USD</strong> al número de cuenta indicado</span></div>
          <div class="step"><div class="step-n">3</div><span>Mandá el comprobante a <strong style="color:var(--teal)">@Stakegoldia_bot</strong></span></div>
        </div>
      </div>
    </div>

    <div class="metodo" style="opacity:.5;cursor:default">
      <div class="metodo-header">
        <div class="metodo-name">🔶 Crypto (USDT) <span class="badge b-gray">Próximamente</span></div>
      </div>
    </div>

  </div>

  <div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;font-size:13px;color:var(--text2);line-height:1.8">
    ✅ Una vez que confirmemos tu pago por Telegram, activamos tu plan en menos de 1 hora.<br>
    ✅ Acceso completo por 30 días desde la activación.<br>
    ✅ Podés renovar antes de que venza para no perder los picks.
  </div>

  <a href="/" class="btn-back">← Volver al dashboard</a>
</div>
<script>
function toggle(el){
  document.querySelectorAll('.metodo').forEach(m=>{
    if(m!==el){m.classList.remove('active');}
  });
  el.classList.toggle('active');
}
function copiar(txt,btn){
  navigator.clipboard.writeText(txt).then(()=>{
    const orig=btn.textContent;
    btn.textContent='¡Copiado!';btn.style.color='var(--teal)';
    setTimeout(()=>{btn.textContent=orig;btn.style.color='';},2000);
  });
}
</script></body></html>"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InvestiaBet</title>
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131929;--border:#1e2a3d;--text:#e2e8f4;--text2:#7a8aaa;--blue:#4f8ef7;--violet:#7c5ff7;--teal:#00d4aa;--red:#ef4444;--radius:14px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);background-image:radial-gradient(ellipse at 20% 50%,rgba(79,142,247,.06) 0%,transparent 60%),radial-gradient(ellipse at 80% 20%,rgba(124,95,247,.06) 0%,transparent 60%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:40px 36px;width:100%;max-width:440px;box-shadow:0 24px 64px rgba(0,0,0,.5)}
.logo{text-align:center;margin-bottom:32px}
.logo-icon{font-size:48px;margin-bottom:10px;display:block}
.logo-title{font-size:26px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--violet),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.logo-sub{font-size:13px;color:var(--text2);margin-top:6px}
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:28px}
.tab{flex:1;padding:11px;text-align:center;font-size:14px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;transition:all .2s}
.tab.active{color:var(--blue);border-bottom-color:var(--blue);font-weight:500}
.field{margin-bottom:18px}
.field label{display:block;font-size:11px;color:var(--text2);margin-bottom:7px;letter-spacing:.5px;text-transform:uppercase}
.field input{width:100%;padding:11px 14px;background:var(--bg3);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none;transition:border-color .2s}
.field input:focus{border-color:var(--blue)}
.btn{width:100%;padding:13px;background:linear-gradient(135deg,var(--blue),var(--violet));border:none;border-radius:10px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.btn:hover{opacity:.9}.btn:disabled{opacity:.5;cursor:not-allowed}
.msg{padding:11px 14px;border-radius:10px;font-size:13px;margin-top:12px;text-align:center}
.msg-err{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.msg-ok{background:rgba(0,212,170,.1);color:var(--teal);border:1px solid rgba(0,212,170,.2)}
.plan-info{background:var(--bg3);border-radius:10px;padding:13px 15px;margin-bottom:18px;font-size:12px;color:var(--text2);line-height:1.8;border:1px solid var(--border)}
.plan-info strong{color:var(--teal)}.hidden{display:none}
</style></head><body>
<div class="card">
  <div class="logo">
    <span class="logo-icon">📈</span>
    <div class="logo-title">InvestiaBet</div>
    <div class="logo-sub">Inversión inteligente en deportes</div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('login')">Ingresar</div>
    <div class="tab" onclick="showTab('register')">Registrarse</div>
  </div>
  <div id="form-login">
    <div class="field"><label>Email</label><input type="email" id="l-email" placeholder="tu@email.com" autocomplete="email"></div>
    <div class="field"><label>Contraseña</label><input type="password" id="l-pass" placeholder="••••••••" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <button class="btn" onclick="doLogin()" id="btn-login">Ingresar</button>
    <div id="login-msg"></div>
  </div>
  <div id="form-register" class="hidden">
    <div class="plan-info">
      <strong>Gratis:</strong> 3 Gold Tips/día · 2 Sure Bets.<br>
      <strong>Premium:</strong> Todo completo + Telegram + ROI real.<br>
      Con código de invitación → Premium inmediato.
    </div>
    <div class="field"><label>Email</label><input type="email" id="r-email" placeholder="tu@email.com"></div>
    <div class="field"><label>Usuario</label><input type="text" id="r-user" placeholder="nombre de usuario"></div>
    <div class="field"><label>Contraseña</label><input type="password" id="r-pass" placeholder="mínimo 6 caracteres"></div>
    <div class="field"><label>Código de invitación (opcional)</label><input type="text" id="r-code" placeholder="XXXXXXXX" style="text-transform:uppercase"></div>
    <button class="btn" onclick="doRegister()" id="btn-register">Crear cuenta</button>
    <div id="register-msg"></div>
  </div>
</div>
<script>
function showTab(t){
  document.getElementById('form-login').classList.toggle('hidden',t!=='login');
  document.getElementById('form-register').classList.toggle('hidden',t!=='register');
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',(i===0&&t==='login')||(i===1&&t==='register')));
}
async function doLogin(){
  const btn=document.getElementById('btn-login');
  btn.disabled=true;btn.textContent='Ingresando...';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('l-email').value.trim().toLowerCase(),
        password:document.getElementById('l-pass').value})});
    const d=await r.json();
    if(d.ok){if(d.token)localStorage.setItem('sb_token',d.token);window.location.href=d.plan==='admin'?'/admin':'/';}
    else{const el=document.getElementById('login-msg');el.className='msg msg-err';el.textContent=d.error||'Email o contraseña incorrectos';}
  }catch(e){document.getElementById('login-msg').className='msg msg-err';document.getElementById('login-msg').textContent='Error de conexión';}
  btn.disabled=false;btn.textContent='Ingresar';
}
async function doRegister(){
  const btn=document.getElementById('btn-register');
  btn.disabled=true;btn.textContent='Creando cuenta...';
  const r=await fetch('/api/auth/register',{method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email:document.getElementById('r-email').value.trim(),
      username:document.getElementById('r-user').value.trim(),
      password:document.getElementById('r-pass').value,
      codigo:document.getElementById('r-code').value.toUpperCase().trim()})});
  const d=await r.json();btn.disabled=false;btn.textContent='Crear cuenta';
  const el=document.getElementById('register-msg');
  if(d.ok){el.className='msg msg-ok';el.textContent='Cuenta creada (plan '+d.plan+'). Iniciá sesión.';setTimeout(()=>showTab('login'),2000);}
  else{el.className='msg msg-err';el.textContent=d.error||'Error al registrar';}
}
</script></body></html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — InvestiaBet</title>
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131929;--border:#1e2a3d;--text:#e2e8f4;--text2:#7a8aaa;--blue:#4f8ef7;--violet:#7c5ff7;--teal:#00d4aa;--red:#ef4444;--radius:10px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.logo{font-size:16px;font-weight:600;background:linear-gradient(135deg,var(--blue),var(--violet));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.btn{padding:7px 16px;border-radius:8px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer}
.btn-main{background:linear-gradient(135deg,var(--blue),var(--violet));color:#fff;border:none;font-weight:600}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.metric-label{font-size:11px;color:var(--text2);margin-bottom:5px}
.metric-val{font-size:22px;font-weight:600}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;margin-bottom:16px}
.card-title{font-size:14px;font-weight:600;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;color:var(--text2);font-size:11px;border-bottom:1px solid var(--border)}
td{padding:10px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
.badge{display:inline-flex;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:500}
.b-blue{background:rgba(79,142,247,.15);color:var(--blue)}.b-violet{background:rgba(124,95,247,.15);color:var(--violet)}
.b-teal{background:rgba(0,212,170,.12);color:var(--teal)}.b-gray{background:var(--bg3);color:var(--text2)}
.inv-box{background:var(--bg3);border-radius:10px;padding:14px 16px;margin-top:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.inv-code{font-family:monospace;font-size:22px;font-weight:700;color:var(--teal);letter-spacing:3px}
.field{display:flex;flex-direction:column;gap:4px;flex:1;min-width:120px}
.field label{font-size:11px;color:var(--text2)}
select,input{padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px}
</style></head><body>
<div class="topbar">
  <div class="logo">📈 InvestiaBet — Admin</div>
  <div style="display:flex;gap:8px">
    <button class="btn" onclick="window.location.href='/'">Dashboard</button>
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>
<div class="container">
  <div class="metrics">
    <div class="metric"><div class="metric-label">Total usuarios</div><div class="metric-val" id="m-total">—</div></div>
    <div class="metric"><div class="metric-label">Premium</div><div class="metric-val" id="m-premium" style="color:var(--violet)">—</div></div>
    <div class="metric"><div class="metric-label">Freemium</div><div class="metric-val" id="m-free">—</div></div>
    <div class="metric"><div class="metric-label">Invitaciones</div><div class="metric-val" id="m-inv">—</div></div>
  </div>
  <div class="card">
    <div class="card-title">Generar código de invitación</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
      <div class="field"><label>Plan</label><select id="inv-plan"><option value="premium">Premium</option><option value="free">Free</option></select></div>
      <div class="field"><label>Usos máximos</label><input type="number" id="inv-usos" value="1" min="1" style="width:100px"></div>
      <button class="btn btn-main" onclick="generarInvitacion()">Generar código</button>
    </div>
    <div id="inv-result"></div>
  </div>
  <div class="card">
    <div class="card-title">Usuarios registrados</div>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>Usuario</th><th>Email</th><th>Plan</th><th>Bankroll</th><th>Telegram</th><th>Registro</th><th>Acciones</th></tr></thead>
      <tbody id="tabla-usuarios"></tbody>
    </table></div>
  </div>
  <div class="card">
    <div class="card-title">Códigos de invitación</div>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>Código</th><th>Plan</th><th>Usos</th><th>Estado</th><th>Fecha</th></tr></thead>
      <tbody id="tabla-invitaciones"></tbody>
    </table></div>
  </div>
</div>
<script>
function getToken(){return localStorage.getItem('sb_token')||'';}
function authH(){const t=getToken();return t?{'Authorization':'Bearer '+t,'Content-Type':'application/json'}:{'Content-Type':'application/json'};}
async function cargarDatos(){
  try{
    const[ru,ri]=await Promise.all([
      fetch('/api/admin/usuarios',{credentials:'include',headers:authH()}).then(r=>r.json()),
      fetch('/api/admin/invitaciones',{credentials:'include',headers:authH()}).then(r=>r.json())
    ]);
    const u=Array.isArray(ru)?ru:[];const i=Array.isArray(ri)?ri:[];
    document.getElementById('m-total').textContent=u.length;
    document.getElementById('m-premium').textContent=u.filter(x=>x.plan==='premium').length;
    document.getElementById('m-free').textContent=u.filter(x=>x.plan==='free').length;
    document.getElementById('m-inv').textContent=i.filter(x=>x.usado).length+'/'+i.length;
    document.getElementById('tabla-usuarios').innerHTML=u.map(x=>`<tr>
      <td><strong>${x.username}</strong></td>
      <td style="color:var(--text2);font-size:12px">${x.email}</td>
      <td><span class="badge ${x.plan==='premium'?'b-violet':x.plan==='admin'?'b-blue':'b-gray'}">${x.plan}</span></td>
      <td>$${x.bankroll||1000} ${x.moneda||'USD'}</td>
      <td>${x.tg_activo?'<span class="badge b-teal">✓</span>':'—'}</td>
      <td style="font-size:11px;color:var(--text2)">
        ${x.fecha_registro?String(x.fecha_registro).substring(0,10):''}
        ${x.fecha_vencimiento?'<br><span style="color:var(--amber)">Vence: '+String(x.fecha_vencimiento).substring(0,10)+'</span>':''}
      </td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <select onchange="cambiarPlan(${x.id},this.value)" style="font-size:11px;padding:4px 6px">
          <option ${x.plan==='free'?'selected':''} value="free">Free</option>
          <option ${x.plan==='premium'?'selected':''} value="premium">Premium</option>
          <option ${x.plan==='admin'?'selected':''} value="admin">Admin</option>
        </select>
        <button class="btn" style="font-size:11px;padding:4px 10px;color:${x.activo?'var(--red)':'var(--teal)'}"
          onclick="toggleActivo(${x.id},${!x.activo})">${x.activo?'Desactivar':'Activar'}</button>
        <button class="btn" style="font-size:11px;padding:4px 10px;color:var(--violet)"
          onclick="activarPremium(${x.id})">⭐ +30d</button>
      </td></tr>`).join('');
    document.getElementById('tabla-invitaciones').innerHTML=i.map(x=>`<tr>
      <td><code style="color:var(--teal);font-size:14px;letter-spacing:1px">${x.codigo}</code></td>
      <td><span class="badge ${x.plan==='premium'?'b-violet':'b-gray'}">${x.plan}</span></td>
      <td>${x.usos_actuales}/${x.max_usos}</td>
      <td>${x.usado?'<span class="badge b-gray">Agotado</span>':'<span class="badge b-teal">Disponible</span>'}</td>
      <td style="font-size:11px;color:var(--text2)">${x.fecha_creacion?String(x.fecha_creacion).substring(0,10):''}</td>
    </tr>`).join('');
  }catch(e){console.error(e);}
}
async function generarInvitacion(){
  const r=await fetch('/api/admin/invitacion',{method:'POST',credentials:'include',headers:authH(),
    body:JSON.stringify({plan:document.getElementById('inv-plan').value,max_usos:parseInt(document.getElementById('inv-usos').value)||1})});
  const d=await r.json();
  if(d.ok){
    document.getElementById('inv-result').innerHTML=`<div class="inv-box">
      <div><div style="font-size:11px;color:var(--text2);margin-bottom:5px">Código generado:</div>
      <div class="inv-code">${d.codigo}</div></div>
      <button class="btn" onclick="navigator.clipboard.writeText('${d.codigo}');this.textContent='¡Copiado!'">Copiar</button>
    </div>`;
    cargarDatos();
  }
}
async function activarPremium(id){
  const dias = prompt('¿Cuántos días de Premium?','30');
  if(!dias) return;
  const r=await fetch('/api/admin/usuario/'+id+'/premium',{method:'POST',credentials:'include',headers:authH(),body:JSON.stringify({dias:parseInt(dias)})});
  const d=await r.json();
  if(d.ok) alert('✓ Premium activado hasta '+d.vencimiento?.substring(0,10));
  cargarDatos();
}
async function cambiarPlan(id,plan){await fetch('/api/admin/usuario/'+id+'/plan',{method:'POST',credentials:'include',headers:authH(),body:JSON.stringify({plan})});cargarDatos();}
async function toggleActivo(id,activo){await fetch('/api/admin/usuario/'+id+'/activo',{method:'POST',credentials:'include',headers:authH(),body:JSON.stringify({activo})});cargarDatos();}
async function doLogout(){await fetch('/api/auth/logout',{method:'POST',credentials:'include',headers:authH()});localStorage.removeItem('sb_token');window.location.href='/login';}
cargarDatos();setInterval(cargarDatos,30000);
</script></body></html>"""

STATS_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mis Estadísticas — InvestiaBet</title>
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131929;--bg4:#171f2e;--border:#1e2a3d;--border2:#243347;--text:#e2e8f4;--text2:#7a8aaa;--text3:#3d4f6a;--blue:#4f8ef7;--blue-bg:rgba(79,142,247,.1);--blue-border:rgba(79,142,247,.25);--violet:#7c5ff7;--violet-bg:rgba(124,95,247,.1);--violet-border:rgba(124,95,247,.25);--teal:#00d4aa;--teal-bg:rgba(0,212,170,.08);--teal-border:rgba(0,212,170,.2);--green:#22c55e;--green-bg:rgba(34,197,94,.1);--red:#ef4444;--red-bg:rgba(239,68,68,.1);--amber:#f59e0b;--amber-bg:rgba(245,158,11,.1);--radius:12px;--radius-sm:8px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:rgba(13,18,32,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:13px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:17px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--violet),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.btn{padding:7px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer}
.btn:hover{background:var(--bg4)}
.btn-grad{background:linear-gradient(135deg,var(--blue),var(--violet));border:none;color:#fff;font-weight:600;padding:9px 18px;border-radius:var(--radius-sm);cursor:pointer;font-size:13px}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
.section-title{font-size:18px;font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:8px}
/* Bankroll card */
.bankroll-card{background:var(--bg2);border:1px solid var(--blue-border);border-radius:var(--radius);padding:20px;margin-bottom:20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px;position:relative;overflow:hidden}
.bankroll-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--blue),var(--violet),var(--teal))}
.bankroll-val{font-size:36px;font-weight:700;color:var(--teal)}
.bankroll-label{font-size:12px;color:var(--text2);margin-bottom:4px}
.bankroll-actions{display:flex;gap:8px;flex-wrap:wrap}
/* Pendientes */
.pendientes-section{margin-bottom:20px}
.pick-pendiente{background:var(--bg2);border:1px solid var(--amber-bg);border-left:3px solid var(--amber);border-radius:var(--radius);padding:16px;margin-bottom:10px}
.pick-pendiente-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.resultado-form{background:var(--bg3);border-radius:var(--radius-sm);padding:14px;margin-top:10px}
.resultado-form label{font-size:11px;color:var(--text2);display:block;margin-bottom:5px;text-transform:uppercase;letter-spacing:.3px}
.resultado-form input{width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;margin-bottom:10px}
.resultado-btns{display:flex;gap:8px;flex-wrap:wrap}
.rbtn{padding:8px 16px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:13px;cursor:pointer;transition:all .15s}
.rbtn:hover{background:var(--bg4)}
.rbtn.ganado{border-color:var(--teal);color:var(--teal)}
.rbtn.ganado:hover{background:var(--teal-bg)}
.rbtn.perdido{border-color:var(--red);color:var(--red)}
.rbtn.perdido:hover{background:var(--red-bg)}
.rbtn.cashout{border-color:var(--amber);color:var(--amber)}
.rbtn.cashout:hover{background:var(--amber-bg)}
.cashout-field{display:none;margin-top:10px}
/* Stats metrics */
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:20px;overflow-x:auto}
.tab{padding:9px 18px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab.active{color:var(--blue);border-bottom-color:var(--blue);font-weight:500}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px;position:relative;overflow:hidden}
.metric::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.metric.m-teal::before{background:linear-gradient(90deg,var(--teal),transparent)}
.metric.m-blue::before{background:linear-gradient(90deg,var(--blue),transparent)}
.metric.m-violet::before{background:linear-gradient(90deg,var(--violet),transparent)}
.metric.m-green::before{background:linear-gradient(90deg,var(--green),transparent)}
.metric.m-red::before{background:linear-gradient(90deg,var(--red),transparent)}
.metric-label{font-size:10px;color:var(--text2);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}
.metric-val{font-size:24px;font-weight:700}
.metric-sub{font-size:10px;color:var(--text3);margin-top:3px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:700px){.two-col{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:16px}
.card-title{font-size:14px;font-weight:600;margin-bottom:14px;color:var(--text2)}
.tipo-row{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--bg3);border-radius:var(--radius-sm);margin-bottom:8px;flex-wrap:wrap;gap:8px}
.tipo-label{font-size:13px;font-weight:500}
.tipo-stats{display:flex;gap:14px;font-size:12px;color:var(--text2);flex-wrap:wrap}
.tipo-stats strong{color:var(--text)}
.roi-bar-wrap{background:var(--border);border-radius:4px;height:5px;margin-top:8px}
.roi-bar{height:5px;border-radius:4px}
.badge{display:inline-flex;align-items:center;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:500}
.b-teal{background:var(--teal-bg);color:var(--teal);border:1px solid var(--teal-border)}
.b-violet{background:var(--violet-bg);color:var(--violet);border:1px solid var(--violet-border)}
.b-blue{background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}
.b-green{background:var(--green-bg);color:var(--green)}
.b-red{background:var(--red-bg);color:var(--red)}
.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-gray{background:var(--bg3);color:var(--text2)}
.hist-table{width:100%;border-collapse:collapse;font-size:13px}
.hist-table th{text-align:left;padding:8px 10px;color:var(--text2);font-size:11px;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.3px}
.hist-table td{padding:10px;border-bottom:1px solid var(--border);vertical-align:middle}
.hist-table tr:last-child td{border-bottom:none}
.hist-table tr:hover td{background:var(--bg3)}
.empty{text-align:center;padding:50px 20px;color:var(--text2)}
.empty-icon{font-size:36px;display:block;margin-bottom:12px;opacity:.3}
/* Modal ajuste bankroll */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.8);backdrop-filter:blur(4px);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:26px;width:100%;max-width:380px}
.mfield{margin-bottom:14px}
.mfield label{display:block;font-size:11px;color:var(--text2);margin-bottom:5px;text-transform:uppercase;letter-spacing:.3px}
.mfield input,.mfield select{width:100%;padding:9px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:13px;outline:none}
.mfield input:focus{border-color:var(--blue)}
</style></head><body>

<div class="topbar">
  <div class="logo">📈 InvestiaBet — Mis Stats</div>
  <div style="display:flex;gap:8px">
    <button class="btn-grad" onclick="showManual()" style="font-size:12px;padding:7px 14px">+ Pick manual</button>
    <button class="btn" onclick="window.location.href='/'">← Dashboard</button>
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>

<div class="container">

  <!-- Bankroll card -->
  <div class="bankroll-card">
    <div>
      <div class="bankroll-label">Bankroll actual</div>
      <div class="bankroll-val" id="bankroll-val">$—</div>
      <div style="font-size:12px;color:var(--text2);margin-top:4px" id="moneda-val">USD</div>
    </div>
    <div class="bankroll-actions">
      <button class="btn-grad" onclick="showAjuste('deposito')">+ Agregar fondos</button>
      <button class="btn" onclick="showAjuste('retiro')" style="color:var(--red);border-color:var(--red)">− Retirar</button>
      <button class="btn" onclick="showAjuste('ajuste')">✏️ Ajuste manual</button>
    </div>
  </div>

  <!-- Picks pendientes de resultado -->
  <div class="pendientes-section" id="pendientes-section" style="display:none">
    <div class="section-title">⏳ Picks esperando resultado</div>
    <div id="pendientes-lista"></div>
  </div>

  <!-- Tabs estadísticas -->
  <div class="tabs">
    <div class="tab active" onclick="showTab('mes',this)">Últimos 30 días</div>
    <div class="tab" onclick="showTab('todo',this)">Todo el historial</div>
    <div class="tab" onclick="showTab('historial',this)">Detalle de picks</div>
  </div>

  <div id="panel-mes"></div>
  <div id="panel-todo" style="display:none"></div>
  <div id="panel-historial" style="display:none"></div>

</div>

<!-- Modal ajuste bankroll -->
<div class="modal-bg" id="modal-ajuste" style="display:none" onclick="if(event.target===this)hideAjuste()">
  <div class="modal">
    <div style="font-size:16px;font-weight:600;margin-bottom:18px;color:var(--blue)" id="modal-title">Ajustar Bankroll</div>
    <div class="mfield"><label>Monto (USD)</label><input type="number" id="aj-monto" min="0" step="0.01" placeholder="ej: 500"></div>
    <div class="mfield"><label>Moneda</label>
      <select id="aj-moneda">
        <option value="USD">USD</option>
        <option value="ARS">ARS</option>
        <option value="USDT">USDT</option>
      </select>
    </div>
    <div class="mfield"><label>Descripción (opcional)</label><input type="text" id="aj-desc" placeholder="ej: Ganancia del día"></div>
    <div style="display:flex;gap:8px;margin-top:18px">
      <button class="btn" style="flex:1" onclick="hideAjuste()">Cancelar</button>
      <button class="btn-grad" style="flex:1" onclick="confirmarAjuste()">Confirmar</button>
    </div>
    <div id="ajuste-msg" style="font-size:12px;margin-top:8px;text-align:center"></div>
  </div>
</div>

<!-- Modal pick manual -->
<div class="modal-bg" id="modal-manual" style="display:none" onclick="if(event.target===this)hideManual()">
  <div class="modal" style="max-width:480px">
    <div style="font-size:16px;font-weight:600;margin-bottom:18px;color:var(--violet)">+ Agregar pick manual</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div class="mfield" style="grid-column:1/-1"><label>Evento *</label><input type="text" id="man-evento" placeholder="ej: Texas Rangers vs Arizona Diamondbacks"></div>
      <div class="mfield" style="grid-column:1/-1"><label>Pick colocado *</label><input type="text" id="man-pick" placeholder="ej: Arizona Diamondbacks"></div>
      <div class="mfield"><label>Deporte</label>
        <select id="man-deporte">
          <option value="Béisbol">Béisbol</option>
          <option value="Fútbol">Fútbol</option>
          <option value="Básquet">Básquet</option>
          <option value="Tenis">Tenis</option>
          <option value="MMA">MMA</option>
          <option value="Esports">Esports</option>
          <option value="Otro">Otro</option>
        </select>
      </div>
      <div class="mfield"><label>Liga</label><input type="text" id="man-liga" placeholder="ej: MLB"></div>
      <div class="mfield"><label>Cuota colocada *</label><input type="number" id="man-odds" step="0.01" min="1.01" placeholder="ej: 1.94"></div>
      <div class="mfield"><label>Stake (ARS o USD) *</label><input type="number" id="man-stake" step="100" min="0" placeholder="ej: 50000"></div>
      <div class="mfield" style="grid-column:1/-1"><label>Resultado</label>
        <select id="man-estado" onchange="document.getElementById('co-field').style.display=this.value==='cashout'?'block':'none'">
          <option value="pendiente">Pendiente</option>
          <option value="ganado">Ganó ✓</option>
          <option value="perdido">Perdió ✗</option>
          <option value="cashout">Cash Out 💸</option>
          <option value="void">Void —</option>
        </select>
      </div>
      <div class="mfield" style="grid-column:1/-1;display:none" id="co-field">
        <label>Cuota de Cash Out</label>
        <input type="number" id="man-odds-co" step="0.01" min="1.01" placeholder="ej: 2.10">
      </div>
    </div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn" style="flex:1" onclick="hideManual()">Cancelar</button>
      <button class="btn-grad" style="flex:1" onclick="guardarManual()">Guardar pick</button>
    </div>
    <div id="manual-msg" style="font-size:12px;margin-top:8px;text-align:center"></div>
  </div>
</div>

<script>
let DATA=null, currentTab='mes', ajusteTipo='deposito';
function getToken(){return localStorage.getItem('sb_token')||'';}
function authH(){const t=getToken();return t?{'Authorization':'Bearer '+t,'Content-Type':'application/json'}:{'Content-Type':'application/json'};}
async function aFetch(url,opts={}){opts.credentials='include';opts.headers={...authH(),...(opts.headers||{})};return fetch(url,opts);}
function fmt(n,d=2){return n!=null?Number(n).toFixed(d):'—';}
let _ratio = 1;
function fmtMiles(n){
  if(n==null||n===undefined) return '—';
  return Math.round(n * _ratio).toLocaleString('es-AR');
}
function fmtMilesRaw(n){
  if(n==null||n===undefined) return '—';
  return Math.round(n).toLocaleString('es-AR');
}
function fmtUSD(n,m='USD'){if(n==null)return'—';const s=n>=0?'+':'-';return s+'$'+Math.abs(n).toFixed(2)+' '+m;}
function fmtPct(n){return n!=null?(n>=0?'+':'')+Number(n).toFixed(1)+'%':'—';}
function fmtDate(d){if(!d)return'—';return new Date(d).toLocaleDateString('es-AR',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'});}

function estadoBadge(e){
  const m={ganado:'b-teal',perdido:'b-red',pendiente:'b-amber',void:'b-gray',cashout:'b-violet'};
  const l={ganado:'✓ Ganó',perdido:'✗ Perdió',pendiente:'⏳ Pendiente',void:'— Void',cashout:'💸 Cash Out'};
  return `<span class="badge ${m[e]||'b-gray'}">${l[e]||e}</span>`;
}
function tipoBadge(tipo,gold){
  if(gold) return '<span class="badge b-violet">⭐ Gold</span>';
  if(tipo==='sure') return '<span class="badge b-teal">🔒 Sure</span>';
  return '<span class="badge b-blue">📊 Value</span>';
}

function renderPendiente(p){
  const id = p.id;
  return `<div class="pick-pendiente" id="pend-${id}">
    <div class="pick-pendiente-top">
      <div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:5px">
          ${tipoBadge(p.tipo,p.es_gold)}
          <span class="badge b-gray" style="font-size:10px">${p.deporte||''}</span>
          <span style="font-size:10px;color:var(--text2)">${p.liga||''}</span>
        </div>
        <div style="font-size:14px;font-weight:600;margin-bottom:2px">${p.evento||'—'}</div>
        <div style="font-size:12px;color:var(--text2)">Pick: <strong style="color:var(--text)">${p.equipo_pick||'—'}</strong> · ${p.mercado||''}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:20px;font-weight:700;color:var(--blue)">@${fmt(p.odds_ref,2)}</div>
        <div style="font-size:11px;color:var(--text2)">Stake: <strong>$${fmt(p.stake_usd,2)}</strong></div>
        <div style="font-size:10px;color:var(--text3);margin-top:2px">${fmtDate(p.fecha_colocado)}</div>
      </div>
    </div>
    <div class="resultado-form">
      <label>Cuota real colocada en la casa</label>
      <input type="number" id="odds-real-${id}" step="0.01" min="1" placeholder="ej: 2.45" value="${p.odds_ref||''}">
      <div class="cashout-field" id="cashout-field-${id}">
        <label>Cuota de Cash Out</label>
        <input type="number" id="odds-cashout-${id}" step="0.01" min="1" placeholder="ej: 1.80">
      </div>
      <div class="resultado-btns">
        <button class="rbtn ganado" onclick="marcarResultado(${id},'ganado')">✓ Ganó</button>
        <button class="rbtn perdido" onclick="marcarResultado(${id},'perdido')">✗ Perdió</button>
        <button class="rbtn cashout" onclick="toggleCashout(${id})">💸 Cash Out</button>
        <button class="rbtn" onclick="marcarResultado(${id},'void')">— Void</button>
      </div>
    </div>
  </div>`;
}

function toggleCashout(id){
  const f=document.getElementById('cashout-field-'+id);
  f.style.display=f.style.display==='block'?'none':'block';
  if(f.style.display==='block'){
    setTimeout(()=>marcarResultado(id,'cashout'),100);
  }
}

async function marcarResultado(id, estado){
  const oddsReal    = parseFloat(document.getElementById('odds-real-'+id)?.value)||0;
  const oddsCashout = parseFloat(document.getElementById('odds-cashout-'+id)?.value)||0;
  if(estado==='cashout' && oddsCashout<=0){
    alert('Ingresá la cuota de cash out');return;
  }
  const r = await aFetch('/api/picks/'+id+'/resultado',{
    method:'POST', body:JSON.stringify({estado, odds_real:oddsReal, odds_cashout:oddsCashout})
  });
  const d = await r.json();
  if(d.ok){
    const pnlColor = d.pnl>=0?'var(--teal)':'var(--red)';
    const pnlText  = d.pnl>=0?'+$'+d.pnl.toFixed(2):'-$'+Math.abs(d.pnl).toFixed(2);
    const el = document.getElementById('pend-'+id);
    if(el) el.innerHTML=`<div style="padding:12px;text-align:center;color:${pnlColor};font-weight:600">
      ${estadoBadge(estado)} P&L: ${pnlText} · Nuevo bankroll: $${d.bankroll_nuevo?.toFixed(2)||'—'}
    </div>`;
    setTimeout(cargarDatos, 1500);
  } else {
    alert('Error: '+(d.error||'desconocido'));
  }
}

function renderStats(stats, moneda='USD'){
  if(!stats||!stats.total_colocados) return '<div class="empty"><span class="empty-icon">📊</span>Sin datos aún.<br><span style="font-size:12px">Colocá picks desde el dashboard para ver tus estadísticas.</span></div>';
  const roiColor = stats.roi>=0?'var(--teal)':'var(--red)';
  const pnlColor = stats.pnl_total>=0?'var(--teal)':'var(--red)';
  const wr = stats.win_rate||0;
  const wrColor = wr>=60?'var(--teal)':wr>=50?'var(--blue)':wr>=40?'var(--amber)':'var(--red)';
  return `
  <div class="metrics">
    <div class="metric m-${wr>=50?'teal':'red'}">
      <div class="metric-label">Win Rate</div>
      <div class="metric-val" style="color:${wrColor}">${fmt(wr,1)}%</div>
      <div class="roi-bar-wrap"><div class="roi-bar" style="width:${Math.min(wr,100)}%;background:${wrColor}"></div></div>
      <div class="metric-sub">${stats.ganados||0} ✓ · ${stats.perdidos||0} ✗ · ${stats.cashouts||0} 💸</div>
    </div>
    <div class="metric m-${stats.roi>=0?'green':'red'}">
      <div class="metric-label">ROI real</div>
      <div class="metric-val" style="color:${roiColor}">${fmtPct(stats.roi)}</div>
      <div class="metric-sub">sobre bankroll inicial</div>
    </div>
    <div class="metric m-${stats.pnl_total>=0?'teal':'red'}">
      <div class="metric-label">P&L total</div>
      <div class="metric-val" style="color:${pnlColor};font-size:18px">${fmtUSD(stats.pnl_total,moneda)}</div>
      <div class="metric-sub">ganado/perdido</div>
    </div>
    <div class="metric m-blue">
      <div class="metric-label">Picks colocados</div>
      <div class="metric-val" style="color:var(--blue)">${stats.total_colocados||0}</div>
      <div class="metric-sub">${stats.pendientes||0} pendientes</div>
    </div>
    <div class="metric m-violet">
      <div class="metric-label">En juego (pendiente)</div>
      <div class="metric-val" style="color:var(--amber);font-size:18px">${fmtMiles(stats.invertido_pendiente||0)}</div>
      <div class="metric-sub">${moneda} pendiente resultado</div>
    </div>
    <div class="metric m-blue">
      <div class="metric-label">Invertido (resuelto)</div>
      <div class="metric-val" style="color:var(--blue);font-size:18px">${fmtMiles(stats.invertido_resuelto||0)}</div>
      <div class="metric-sub">${moneda} ya jugado</div>
    </div>
  </div>
  <div class="two-col">
    <div class="card">
      <div class="card-title">📊 Por tipo de pick</div>
      ${renderTipoRow('💸 Cash Out',stats.gold_stats,'var(--amber)')}
      ${renderTipoRow('⭐ Gold Tips',stats.gold_stats,'var(--violet)')}
      ${renderTipoRow('🔒 Sure Picks',stats.sure_stats,'var(--teal)')}
      ${renderTipoRow('📊 Value Bets',stats.value_stats,'var(--blue)')}
    </div>
    <div class="card">
      <div class="card-title">🏆 Por deporte</div>
      ${Object.entries(stats.por_deporte||{}).map(([d,s])=>renderTipoRow(d,s,'var(--text2)')).join('')||'<div style="color:var(--text2);font-size:13px;text-align:center;padding:20px">Sin datos</div>'}
    </div>
  </div>`;
}

function renderTipoRow(label,s,color){
  if(!s||s.total===0) return `<div class="tipo-row" style="opacity:.35"><div class="tipo-label" style="color:${color}">${label}</div><span style="font-size:12px;color:var(--text3)">Sin picks</span></div>`;
  const rc = s.roi>=0?'var(--teal)':'var(--red)';
  return `<div class="tipo-row">
    <div class="tipo-label" style="color:${color}">${label}</div>
    <div class="tipo-stats">
      <span>${s.total} picks</span>
      <span>Win: <strong>${fmt(s.win_rate,1)}%</strong></span>
      <span>ROI: <strong style="color:${rc}">${fmtPct(s.roi)}</strong></span>
      <span>P&L: <strong style="color:${s.pnl>=0?'var(--teal)':'var(--red)'}">${s.pnl>=0?'+':''}${fmtMiles(Math.abs(s.pnl))}</strong></span>
    </div>
  </div>`;
}

function renderHistorial(picks){
  if(!picks||!picks.length) return '<div class="empty"><span class="empty-icon">📋</span>Sin historial de picks.<br><span style="font-size:12px">Colocá picks desde el dashboard.</span></div>';
  return `<div class="card" style="padding:0;overflow:hidden"><div style="overflow-x:auto">
    <table class="hist-table"><thead><tr>
      <th>Fecha</th><th>Evento</th><th>Pick</th><th>Tipo</th>
      <th>Cuota ref</th><th>Cuota real</th><th>Stake</th><th>Estado</th><th>P&L</th>
    </tr></thead><tbody>
    ${picks.filter(p=>p.estado!=='pendiente').map(p=>`<tr>
      <td style="color:var(--text2);font-size:11px;white-space:nowrap">${fmtDate(p.fecha_colocado)}</td>
      <td><div style="font-weight:500;font-size:13px">${p.evento||'—'}</div><div style="font-size:10px;color:var(--text2)">${p.liga||''} ${p.mercado?'· '+p.mercado:''}</div></td>
      <td style="font-weight:500">${p.equipo_pick||'—'}</td>
      <td>${tipoBadge(p.tipo,p.es_gold)}</td>
      <td style="color:var(--text2)">@${fmt(p.odds_ref,2)}</td>
      <td style="color:var(--blue);font-weight:600">@${p.odds_real?fmt(p.odds_real,2):'—'}</td>
      <td style="color:var(--violet)">${fmtMiles(p.stake_usd)}</td>
      <td>${estadoBadge(p.estado)}${p.es_cashout?'<div style="font-size:10px;color:var(--amber);margin-top:2px">@'+fmt(p.odds_cashout,2)+'</div>':''}</td>
      <td style="font-weight:700;color:${(p.pnl||0)>=0?'var(--teal)':'var(--red)'}">${p.pnl!=null?(p.pnl>=0?'+':'')+fmtMiles(Math.abs(p.pnl)):'—'}</td>
    </tr>`).join('')}
    </tbody></table></div></div>`;
}

function showTab(p,el){
  currentTab=p;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  if(el) el.classList.add('active');
  ['mes','todo','historial'].forEach(id=>{
    document.getElementById('panel-'+id).style.display=id===p?'block':'none';
  });
  if(DATA) renderAll();
}

function renderAll(){
  if(!DATA) return;
  const mon = DATA.moneda||'USD';
  _ratio = DATA.ratio || 1;
  document.getElementById('bankroll-val').textContent=fmtMilesRaw(DATA.bankroll);
  document.getElementById('moneda-val').textContent=mon+' (ratio: '+(_ratio).toFixed(0)+'x)';
  // Pendientes
  const pends = DATA.pendientes||[];
  const psec  = document.getElementById('pendientes-section');
  psec.style.display = pends.length?'block':'none';
  document.getElementById('pendientes-lista').innerHTML = pends.map(renderPendiente).join('');
  // Stats
  document.getElementById('panel-mes').innerHTML     = renderStats(DATA.mes, mon);
  document.getElementById('panel-todo').innerHTML    = renderStats(DATA.todo, mon);
  document.getElementById('panel-historial').innerHTML = renderHistorial(DATA.historial);
}

// Modal ajuste bankroll
function showAjuste(tipo){
  ajusteTipo=tipo;
  const titles={'deposito':'+ Agregar fondos al bankroll','retiro':'− Retirar fondos','ajuste':'✏️ Ajuste manual de bankroll'};
  document.getElementById('modal-title').textContent=titles[tipo]||'Ajustar Bankroll';
  document.getElementById('aj-monto').value='';
  document.getElementById('aj-desc').value='';
  document.getElementById('ajuste-msg').textContent='';
  document.getElementById('modal-ajuste').style.display='flex';
}
function hideAjuste(){document.getElementById('modal-ajuste').style.display='none';}

async function confirmarAjuste(){
  const monto = parseFloat(document.getElementById('aj-monto').value)||0;
  if(monto<=0){alert('Ingresá un monto válido');return;}
  const montoFinal = ajusteTipo==='retiro'?-monto:monto;
  const r = await aFetch('/api/me/bankroll',{method:'POST',body:JSON.stringify({
    monto:montoFinal, tipo:ajusteTipo,
    descripcion:document.getElementById('aj-desc').value
  })});
  const d=await r.json();
  const msg=document.getElementById('ajuste-msg');
  if(d.ok){
    msg.style.color='var(--teal)';
    msg.textContent='✓ Bankroll actualizado: $'+Number(d.bankroll_nuevo).toFixed(2);
    setTimeout(()=>{hideAjuste();cargarDatos();},1500);
  } else {
    msg.style.color='var(--red)';msg.textContent=d.error||'Error';
  }
}

// ── Modal pick manual ─────────────────────────────────────────────────────────
function showManual(){document.getElementById('modal-manual').style.display='flex';}
function hideManual(){document.getElementById('modal-manual').style.display='none';document.getElementById('manual-msg').textContent='';}

async function guardarManual(){
  const estado = document.getElementById('man-estado').value;
  const esCashout = estado === 'cashout';
  const data = {
    evento:       document.getElementById('man-evento').value,
    equipo_pick:  document.getElementById('man-pick').value,
    deporte:      document.getElementById('man-deporte').value,
    liga:         document.getElementById('man-liga').value,
    odds_real:    parseFloat(document.getElementById('man-odds').value)||0,
    odds_cashout: esCashout?parseFloat(document.getElementById('man-odds-co').value)||0:0,
    stake_usd:    parseFloat(document.getElementById('man-stake').value)||0,
    estado:       estado,
    tipo:         'value',
  };
  if(!data.evento||!data.equipo_pick||!data.odds_real||!data.stake_usd){
    document.getElementById('manual-msg').style.color='var(--red)';
    document.getElementById('manual-msg').textContent='Completá los campos obligatorios';
    return;
  }
  const r = await aFetch('/api/picks/manual',{method:'POST',body:JSON.stringify(data)});
  const d = await r.json();
  const msg = document.getElementById('manual-msg');
  if(d.ok){
    msg.style.color='var(--teal)';
    const pnlTxt = d.pnl!=null?(d.pnl>=0?'+$'+d.pnl.toFixed(2):'-$'+Math.abs(d.pnl).toFixed(2)):'pendiente';
    msg.textContent='✓ Pick guardado · P&L: '+pnlTxt+(d.bankroll_nuevo?' · Bankroll: $'+d.bankroll_nuevo.toFixed(2):'');
    setTimeout(()=>{hideManual();cargarDatos();},2000);
  } else {
    msg.style.color='var(--red)';
    msg.textContent=d.error||'Error al guardar';
  }
}

async function cargarDatos(){
  const r=await aFetch('/api/estadisticas');
  if(r.status===401){window.location.href='/login';return;}
  DATA=await r.json();
  renderAll();
}
async function doLogout(){await aFetch('/api/auth/logout',{method:'POST'});localStorage.removeItem('sb_token');window.location.href='/login';}
cargarDatos();
</script></body></html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InvestiaBet</title>
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131929;--bg4:#171f2e;--border:#1e2a3d;--border2:#243347;--text:#e2e8f4;--text2:#7a8aaa;--text3:#3d4f6a;--blue:#4f8ef7;--blue-bg:rgba(79,142,247,.1);--blue-border:rgba(79,142,247,.25);--violet:#7c5ff7;--violet-bg:rgba(124,95,247,.1);--violet-border:rgba(124,95,247,.25);--teal:#00d4aa;--teal-bg:rgba(0,212,170,.08);--teal-border:rgba(0,212,170,.2);--green:#22c55e;--green-bg:rgba(34,197,94,.1);--red:#ef4444;--red-bg:rgba(239,68,68,.1);--amber:#f59e0b;--amber-bg:rgba(245,158,11,.1);--radius:12px;--radius-sm:8px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);background-image:radial-gradient(ellipse at 10% 30%,rgba(79,142,247,.04) 0%,transparent 50%),radial-gradient(ellipse at 90% 70%,rgba(124,95,247,.04) 0%,transparent 50%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:rgba(13,18,32,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:13px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:17px;font-weight:700;display:flex;align-items:center;gap:10px}
.logo-text{background:linear-gradient(135deg,var(--blue),var(--violet),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.logo-dot{width:8px;height:8px;border-radius:50%;background:var(--teal);box-shadow:0 0 8px var(--teal)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:500;white-space:nowrap}
.b-blue{background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}
.b-violet{background:var(--violet-bg);color:var(--violet);border:1px solid var(--violet-border)}
.b-teal{background:var(--teal-bg);color:var(--teal);border:1px solid var(--teal-border)}
.b-green{background:var(--green-bg);color:var(--green)}.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-red{background:var(--red-bg);color:var(--red)}.b-gray{background:var(--bg3);color:var(--text2)}
.btn{padding:7px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer;transition:all .15s}
.btn:hover{background:var(--bg4);border-color:var(--border2)}.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-grad{background:linear-gradient(135deg,var(--blue),var(--violet));border:none;color:#fff;font-weight:600;padding:9px 18px;border-radius:var(--radius-sm);cursor:pointer;font-size:13px}
.container{max-width:1300px;margin:0 auto;padding:20px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden}
.metric::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.metric.m-blue::before{background:linear-gradient(90deg,var(--blue),transparent)}
.metric.m-violet::before{background:linear-gradient(90deg,var(--violet),transparent)}
.metric.m-teal::before{background:linear-gradient(90deg,var(--teal),transparent)}
.metric.m-green::before{background:linear-gradient(90deg,var(--green),transparent)}
.metric-label{font-size:10px;color:var(--text2);margin-bottom:6px;letter-spacing:.5px;text-transform:uppercase}
.metric-val{font-size:22px;font-weight:700}.metric-sub{font-size:10px;color:var(--text3);margin-top:3px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
.col-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.col-title{font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
/* Sure card */
.sure-card{background:var(--bg2);border:1px solid var(--teal-border);border-radius:var(--radius);padding:16px;margin-bottom:10px;position:relative;overflow:hidden}
.sure-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--teal),var(--blue))}
.sure-roi{font-size:26px;font-weight:700;color:var(--teal)}
/* Value card */
.pick-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin-bottom:10px;position:relative;overflow:hidden}
.pick-card.gold::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--violet),var(--blue))}
.pick-card.gold{border-color:var(--violet-border)}
.pick-card:hover{border-color:var(--border2)}
.pick-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.pick-info{flex:1;min-width:140px}
.pick-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:6px}
.pick-evento{font-size:14px;font-weight:600;margin-bottom:2px}
.pick-sub{font-size:12px;color:var(--text2)}
.pick-mercado{font-size:10px;padding:2px 8px;border-radius:20px;background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}
.edge-val{font-size:24px;font-weight:700;text-align:right}.edge-lbl{font-size:10px;color:var(--text2);text-align:right}
.bar-wrap{height:3px;background:var(--border);border-radius:2px;margin:10px 0 3px}
.bar-fill{height:3px;border-radius:2px}
.bar-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:10px}
.pick-bottom{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;padding-top:10px;border-top:1px solid var(--border)}
.pick-nums{display:flex;gap:14px;flex-wrap:wrap}
.num-label{font-size:10px;color:var(--text2);margin-bottom:2px}.num-val{font-size:15px;font-weight:700}
/* En vivo */
.vivo-section{margin-bottom:16px;display:none}
.vivo-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--red);box-shadow:0 0 8px var(--red);animation:pulse 1.5s infinite}
/* Tabs */
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:14px;overflow-x:auto}
.tab{padding:9px 16px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all .15s}
.tab:hover{color:var(--text)}.tab.active{color:var(--blue);border-bottom-color:var(--blue);font-weight:500}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.pill{padding:5px 12px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer;transition:all .15s}
.pill:hover{color:var(--text)}.pill.active{background:var(--blue-bg);color:var(--blue);border-color:var(--blue-border)}
.ctx-tag{display:inline-flex;gap:3px;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:500}
.ctx-clean{background:var(--green-bg);color:var(--green)}.ctx-warn{background:var(--amber-bg);color:var(--amber)}
.ctx-esport{background:var(--violet-bg);color:var(--violet)}
.select{padding:4px 8px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px}
.freemium-banner{background:linear-gradient(135deg,rgba(124,95,247,.08),rgba(79,142,247,.08));border:1px solid var(--violet-border);border-radius:var(--radius);padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.empty{text-align:center;padding:40px 20px;color:var(--text2)}
.empty-icon{font-size:32px;display:block;margin-bottom:10px;opacity:.4}
.lock-row{padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--text3);display:flex;align-items:center;gap:5px}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.8);backdrop-filter:blur(4px);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:26px;width:100%;max-width:400px}
.field{margin-bottom:14px}.field label{display:block;font-size:11px;color:var(--text2);margin-bottom:5px;letter-spacing:.3px;text-transform:uppercase}
.field input,.field select{width:100%;padding:9px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:13px;outline:none}
.field input:focus{border-color:var(--blue)}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style></head><body>

<div class="topbar">
  <div class="logo">
    <div class="logo-dot"></div>
    <span class="logo-text">InvestiaBet</span>
    <span id="plan-badge" class="badge b-gray" style="font-size:10px">—</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <span id="scan-badge" class="badge b-amber">Cargando...</span>
    <span id="last-scan" style="font-size:11px;color:var(--text2)"></span>
    <button class="btn" onclick="window.location.href='/estadisticas'">📊 Mis Stats</button>
    <button class="btn" onclick="showPerfil()">⚙ Perfil</button>
    <button class="btn" id="btn-scan" onclick="triggerScan()">↻ Escanear</button>
    <button class="btn" id="btn-admin" onclick="window.location.href='/admin'" style="display:none;color:var(--violet);border-color:var(--violet-border)">⚙ Admin</button>
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>

<div class="container">

  <div class="freemium-banner" id="freemium-banner" style="display:none">
    <div>
      <div style="font-weight:600;margin-bottom:3px;color:var(--violet)">Plan Gratuito</div>
      <div style="font-size:12px;color:var(--text2)">Ves 3 Gold Tips y 2 Sure Bets. Activá Premium para acceso completo.</div>
    </div>
    <button class="btn-grad" onclick="window.location.href='/premium'">⭐ Activar Premium</button>
  </div>

  <div class="metrics">
    <div class="metric m-teal"><div class="metric-label">Sure Bets (alta confianza)</div><div class="metric-val" id="m-sure" style="color:var(--teal)">—</div><div class="metric-sub">≥85% prob modelo</div></div>
    <div class="metric m-teal"><div class="metric-label">ROI sure potencial</div><div class="metric-val" id="m-roi-sure" style="color:var(--teal)">—</div><div class="metric-sub">si todos ganan</div></div>
    <div class="metric m-violet"><div class="metric-label">⭐ Gold Tips</div><div class="metric-val" id="m-gold" style="color:var(--violet)">—</div><div class="metric-sub">mejores value picks</div></div>
    <div class="metric m-violet"><div class="metric-label">ROI value potencial</div><div class="metric-val" id="m-roi" style="color:var(--violet)">—</div><div class="metric-sub">si todos ganan</div></div>
    <div class="metric m-blue"><div class="metric-label">Bankroll</div><div class="metric-val" id="m-bank" style="color:var(--blue)">—</div><div class="metric-sub" id="m-bank-sub">USD</div></div>
    <div class="metric m-blue"><div class="metric-label">Eventos analizados</div><div class="metric-val" id="m-eventos" style="color:var(--blue)">—</div><div class="metric-sub" id="m-ventana">próximas horas</div></div>
  </div>

  <!-- EN VIVO -->
  <div class="vivo-section" id="vivo-section">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <div style="font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px">
        <span class="vivo-dot"></span>
        <span style="color:var(--red)">En Vivo</span>
        <span class="badge b-red" id="vivo-count">0 picks</span>
      </div>
      <span style="font-size:11px;color:var(--text2)">Edge mín 8% · Mayor volatilidad · Verificá la cuota antes de colocar</span>
    </div>
    <div id="vivo-lista"></div>
  </div>

  <!-- DOS COLUMNAS -->
  <div class="two-col">
    <div>
      <div class="col-header">
        <div class="col-title"><span style="color:var(--teal)">🔒</span><span style="color:var(--teal)">Alta Confianza (≥85%)</span></div>
        <div style="font-size:11px;color:var(--text2)" id="sure-count-label"></div>
      </div>
      <div id="sure-lista"><div class="empty"><span class="empty-icon">🔒</span>Escaneando...</div></div>
    </div>
    <div>
      <div class="col-header">
        <div class="col-title"><span style="color:var(--violet)">⭐</span><span style="color:var(--violet)">Gold Tips</span></div>
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:11px;color:var(--text2)">Edge mín:</span>
          <select class="select" id="min-edge" onchange="renderValue()">
            <option value="3" selected>3%</option><option value="5">5%</option><option value="7">7%</option>
          </select>
        </div>
      </div>
      <div id="value-lista"><div class="empty"><span class="empty-icon">📊</span>Escaneando...</div></div>
    </div>
  </div>

  <!-- TABS -->
  <div class="tabs">
    <div class="tab active" onclick="showTab('todos',this)">Todos</div>
    <div class="tab" onclick="showTab('futbol',this)">Fútbol</div>
    <div class="tab" onclick="showTab('tenis',this)">Tenis</div>
    <div class="tab" onclick="showTab('basquet',this)">Básquet</div>
    <div class="tab" onclick="showTab('esports',this)">Esports</div>
    <div class="tab" onclick="showTab('otros',this)">MMA/Béisbol</div>
    <div class="tab" onclick="showTab('descartados',this)">Descartados</div>
  </div>
  <div id="tab-detail"></div>

</div>

<!-- Modal perfil -->
<div class="modal-bg" id="modal-perfil" style="display:none" onclick="if(event.target===this)hidePerfil()">
  <div class="modal">
    <div style="font-size:16px;font-weight:600;margin-bottom:18px;color:var(--blue)">⚙ Mi perfil</div>
    <div class="field"><label>Bankroll (USD)</label><input type="number" id="p-bankroll" min="50"></div>
    <div class="field"><label>Moneda</label>
      <select id="p-moneda"><option value="USD">USD</option><option value="ARS">ARS</option><option value="USDT">USDT</option></select>
    </div>
    <div class="field"><label>Perfil de riesgo</label>
      <select id="p-riesgo">
        <option value="conservador">Conservador — stake 2% máx</option>
        <option value="inteligente">Inteligente — 3-5% (recomendado)</option>
        <option value="profesional">Profesional — Kelly completo</option>
      </select>
    </div>
    <div class="field"><label>Chat ID de Telegram</label><input type="text" id="p-tgid" placeholder="ej: 1759623959"></div>
    <div class="field" style="display:flex;align-items:center;gap:10px">
      <input type="checkbox" id="p-tgactivo" style="width:auto;accent-color:var(--teal)">
      <label for="p-tgactivo" style="font-size:13px">Recibir Gold Tips + Sure Bets por Telegram</label>
    </div>
    <div style="display:flex;gap:8px;margin-top:18px">
      <button class="btn" style="flex:1" onclick="hidePerfil()">Cancelar</button>
      <button class="btn-grad" style="flex:1" onclick="guardarPerfil()">Guardar</button>
    </div>
    <div id="perfil-msg" style="font-size:12px;color:var(--teal);margin-top:8px;text-align:center"></div>
  </div>
</div>

<script>
let DATA=null,USER=null,currentTab='todos';
window._picks={};
function getToken(){return localStorage.getItem('sb_token')||'';}
function authH(){const t=getToken();return t?{'Authorization':'Bearer '+t,'Content-Type':'application/json'}:{'Content-Type':'application/json'};}
async function aFetch(url,opts={}){opts.credentials='include';opts.headers={...authH(),...(opts.headers||{})};return fetch(url,opts);}
function fmt(n,d=2){return n!=null?Number(n).toFixed(d):'—';}
function fmtUSD(n){
  if(n==null) return '—';
  const mon = USER?.moneda||'USD';
  if(mon==='ARS') return 'ARS '+Number(n).toLocaleString('es-AR',{minimumFractionDigits:0,maximumFractionDigits:0});
  if(mon==='USDT') return 'USDT '+fmt(n,2);
  return '$'+fmt(n,2);
}
function fmtPct(n){return n!=null?(n>=0?'+':'')+fmt(n,1)+'%':'—';}
function dep(d){return{Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[d]||'🎯';}
function edgeColor(e){if(e>=0.12)return'var(--teal)';if(e>=0.07)return'var(--blue)';if(e>=0.04)return'var(--violet)';return'var(--text2)';}
function ctxTag(id){const m={champion_early:['ctx-warn','⚠ Campeón'],relegated:['ctx-warn','⬇ Desc.'],esport:['ctx-esport','🎮 Esport'],clean:['ctx-clean','✓ OK']};const[cls,lbl]=m[id]||['ctx-warn',id];return `<span class="ctx-tag ${cls}">${lbl}</span>`;}

function renderSureCard(s){
  const isPrem=USER&&USER.plan!=='free';
  const _sidx=Object.keys(window._picks).length;
  window._picks[_sidx]=s;
  const _sidxStr=String(_sidx);
  const nc=s.nivel_confianza||'ALTA';
  const ncColor=nc==='EXTREMA'?'var(--teal)':nc==='MUY ALTA'?'var(--blue)':'var(--violet)';
  const ncIcon=nc==='EXTREMA'?'🔥':nc==='MUY ALTA'?'⚡':'✓';
  return `<div class="sure-card">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap">
      <div style="flex:1;min-width:140px">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:5px">
          <span class="badge b-teal" style="font-size:10px">ALTA CONFIANZA</span>
          <span class="badge b-gray" style="font-size:10px">${dep(s.deporte)} ${s.deporte}</span>
          <span style="font-size:10px;color:var(--text2)">${s.liga}</span>
        </div>
        <div style="font-size:14px;font-weight:600;margin-bottom:2px">${s.evento}</div>
        <div style="font-size:11px;color:var(--text2)">${s.mercado||'Resultado'} ${s.hora_local?'· 🕐 '+s.hora_local:''}</div>
      </div>
      <div style="text-align:right">
        <div class="sure-roi" style="color:${ncColor}">${fmt(s.confianza_pct,1)}%</div>
        <div style="font-size:10px;color:var(--text2)">confianza modelo</div>
        <div style="font-size:11px;margin-top:3px;color:${ncColor}">${ncIcon} ${nc}</div>
      </div>
    </div>
    ${isPrem?`
    <div style="background:var(--bg3);border-radius:var(--radius-sm);padding:12px;margin-top:12px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px">
        <div><div style="font-size:13px;font-weight:600">${s.equipo_pick}</div><div style="font-size:11px;color:var(--text2)">Mejor cuota disponible</div></div>
        <div style="text-align:right">
          <div style="font-size:20px;font-weight:700;color:var(--blue)">@${fmt(s.odds_ref,2)}</div>
          <div style="font-size:11px;color:var(--text2)">Stake: <strong style="color:var(--text)">$${fmt(s.stake_usd,2)}</strong></div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;font-size:11px;margin-bottom:10px">
        <div style="text-align:center;padding:6px;background:var(--bg2);border-radius:6px"><div style="color:var(--text2);margin-bottom:2px">Pinnacle</div><div style="font-weight:600">${fmt((s.prob_pinnacle||0)*100,1)}%</div></div>
        <div style="text-align:center;padding:6px;background:var(--bg2);border-radius:6px"><div style="color:var(--text2);margin-bottom:2px">Consensus</div><div style="font-weight:600">${fmt((s.prob_consensus||0)*100,1)}%</div></div>
        <div style="text-align:center;padding:6px;background:var(--bg2);border-radius:6px"><div style="color:var(--text2);margin-bottom:2px">Modelo</div><div style="font-weight:600;color:${ncColor}">${fmt((s.prob_modelo||0)*100,1)}%</div></div>
      </div>
      <button class="btn" style="width:100%;font-size:12px;color:var(--teal);border-color:var(--teal-border)" onclick="colocarSure(this,${_sidxStr})">✓ Colocar en Stake</button>
    </div>
    <div style="margin-top:8px;padding:7px 12px;background:var(--teal-bg);border-radius:var(--radius-sm);font-size:11px;color:var(--teal)">📊 ${s.señales||s.senales||'Análisis profundo'}</div>
    <div style="margin-top:6px;padding:7px 12px;background:var(--violet-bg);border-radius:var(--radius-sm);font-size:11px;color:var(--violet);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">
      <span>Ganancia pot: <strong>+$${fmt(s.ganancia_pot,2)}</strong></span>
      <span>ROI: <strong>+${fmt(s.roi_pct,2)}%</strong></span>
    </div>`:`<div class="lock-row">🔒 Detalle disponible en Plan Premium</div>`}
  </div>`;
}

function renderValueCard(p){
  const isPrem=USER&&USER.plan!=='free';
  const barW=Math.min(Math.abs(p.edge)/0.15*100,100).toFixed(1);
  const col=edgeColor(p.edge);
  const _pidx=Object.keys(window._picks).length;
  window._picks[_pidx]=p;
  const _pidxStr=String(_pidx);
  return `<div class="pick-card${p.es_gold?' gold':''}">
    <div class="pick-top">
      <div class="pick-info">
        <div class="pick-meta">
          ${p.es_gold?'<span class="badge b-violet">⭐ Gold</span>':''}
          <span class="badge b-gray" style="font-size:10px">${dep(p.deporte)} ${p.deporte}</span>
          <span class="pick-mercado">${p.mercado||'1X2'}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--amber)">🕐 ${p.hora_local}</span>`:''}
          ${ctxTag(p.contexto_id)}
        </div>
        <div class="pick-evento">${p.evento}</div>
        <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_ref,2)} · ${p.liga}</div>
      </div>
      <div>
        <div class="edge-val" style="color:${col}">${p.edge>=0?'+':''}${(p.edge*100).toFixed(1)}%</div>
        <div class="edge-lbl">edge</div>
        ${isPrem&&p.roi_diario_pct!=null?`<div style="font-size:11px;margin-top:2px;text-align:right;color:var(--violet)">ROI ${fmtPct(p.roi_diario_pct)}</div>`:''}
      </div>
    </div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${barW}%;background:${col}"></div></div>
    <div class="bar-labels"><span>Prob: ${(p.prob_ajustada*100).toFixed(1)}%</span><span>Implícita: ${(1/p.odds_ref*100).toFixed(1)}%</span></div>
    ${isPrem&&p.stake_usd!=null?`
    <div class="pick-bottom">
      <div class="pick-nums">
        <div><div class="num-label">Stake</div><div class="num-val" style="color:var(--blue)">${fmtUSD(p.stake_usd)}</div></div>
        <div><div class="num-label">Ganancia pot.</div><div class="num-val" style="color:var(--violet)">+${fmtUSD(p.ganancia_pot)}</div></div>
        <div><div class="num-label">ROI</div><div class="num-val" style="color:var(--teal)">${fmtPct(p.roi_diario_pct)}</div></div>
      </div>
      <button class="btn" style="font-size:12px" onclick="colocarPick(this,${_pidxStr})">✓ Colocado en Stake</button>
    </div>`:`<div class="lock-row">🔒 Stake y ROI en Plan Premium</div>`}
  </div>`;
}

function renderVivoCard(p){
  const isPrem=USER&&USER.plan!=='free';
  const col=p.edge>=0.15?'var(--teal)':p.edge>=0.10?'var(--blue)':'var(--amber)';
  const _pidx=Object.keys(window._picks).length;
  window._picks[_pidx]=p;
  const _pidxStr=String(_pidx);
  return `<div class="pick-card" style="border-color:rgba(239,68,68,.3)">
    <div style="position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--red),var(--amber))"></div>
    <div class="pick-top">
      <div class="pick-info">
        <div class="pick-meta">
          <span class="badge b-red" style="font-size:10px">🔴 VIVO</span>
          <span class="badge b-gray" style="font-size:10px">${dep(p.deporte)} ${p.deporte}</span>
          <span class="pick-mercado">${p.mercado||'1X2'}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--text2)">${p.hora_local}</span>`:''}
        </div>
        <div class="pick-evento">${p.evento}</div>
        <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_ref,2)} · ${p.liga}</div>
      </div>
      <div>
        <div class="edge-val" style="color:${col}">${p.edge>=0?'+':''}${(p.edge*100).toFixed(1)}%</div>
        <div class="edge-lbl">edge</div>
      </div>
    </div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${Math.min(p.edge/0.20*100,100).toFixed(1)}%;background:${col}"></div></div>
    <div class="bar-labels"><span>Prob: ${(p.prob_ajustada*100).toFixed(1)}%</span><span>Implícita: ${(1/p.odds_ref*100).toFixed(1)}%</span></div>
    ${isPrem?`
    <div class="pick-bottom">
      <div class="pick-nums">
        <div><div class="num-label">Stake</div><div class="num-val" style="color:var(--blue)">${fmtUSD(p.stake_usd)}</div></div>
        <div><div class="num-label">Ganancia pot.</div><div class="num-val" style="color:var(--amber)">+${fmtUSD(p.ganancia_pot)}</div></div>
      </div>
      <button class="btn" style="font-size:12px;color:var(--red);border-color:rgba(239,68,68,.4)" onclick="colocarPick(this,${_pidxStr})">⚡ Colocar ahora</button>
    </div>`:`<div class="lock-row">🔒 Stake en Plan Premium</div>`}
  </div>`;
}

async function colocarPick(btn,pickId){
  const pick=window._picks[pickId] || window._picks[String(pickId)];
  if(!pick){
    console.error('Pick no encontrado:', pickId, 'Keys:', Object.keys(window._picks));
    btn.textContent='Error - recargá';btn.style.color='var(--red)';
    return;
  }
  btn.disabled=true;btn.textContent='Guardando...';
  try{
    const r=await aFetch('/api/picks/colocar',{method:'POST',body:JSON.stringify(pick)});
    const d=await r.json();
    if(d.ok){
      btn.textContent='✓ Guardado en Stats';
      btn.style.color='var(--teal)';
      btn.style.borderColor='var(--teal)';
      btn.title='Abrí Mis Stats para confirmar el resultado';
      if(d.bankroll_nuevo!=null){
        document.getElementById('m-bank').textContent='$'+Number(d.bankroll_nuevo).toFixed(2);
      }
    } else {
      console.error('Error guardando pick:', d.error);
      btn.textContent='✓ Colocado (sin guardar)';
      btn.style.color='var(--amber)';
      btn.disabled=false;
    }
  }catch(e){
    console.error('Error de red:', e);
    btn.textContent='Error de red';
    btn.style.color='var(--red)';
    btn.disabled=false;
  }
}

async function colocarSure(btn,sureId){
  const sure=window._picks[sureId] || window._picks[String(sureId)];
  if(!sure) return;
  btn.disabled=true;btn.textContent='Guardando...';
  try{
    await aFetch('/api/picks/colocar',{method:'POST',body:JSON.stringify({...sure,tipo:'sure'})});
    btn.textContent='✓ Colocado en Stats';btn.style.color='var(--teal)';
  }catch(e){btn.textContent='✓ Colocado';btn.style.color='var(--teal)';btn.disabled=true;}
}

function renderSure(){
  if(!DATA) return;
  const sures=DATA.sure_bets||[];
  document.getElementById('sure-count-label').textContent=sures.length+' encontradas';
  const el=document.getElementById('sure-lista');
  if(!sures.length){el.innerHTML='<div class="empty"><span class="empty-icon">🔍</span>Sin picks de alta confianza.<br><span style="font-size:12px">El motor escanea cada 30 min.</span></div>';return;}
  el.innerHTML=sures.map(renderSureCard).join('');
}

function renderValue(){
  if(!DATA) return;
  const minE=parseFloat(document.getElementById('min-edge').value)/100;
  const picks=(DATA.gold_tips||DATA.picks_validos||[]).filter(p=>p.edge>=minE);
  const el=document.getElementById('value-lista');
  if(!picks.length){el.innerHTML='<div class="empty"><span class="empty-icon">📊</span>Sin Gold Tips con edge ≥ '+(minE*100).toFixed(0)+'%</div>';return;}
  el.innerHTML=picks.map(renderValueCard).join('');
}

function renderVivo(){
  if(!DATA) return;
  const vivos=DATA.picks_vivo||[];
  const sec=document.getElementById('vivo-section');
  if(!vivos.length){sec.style.display='none';return;}
  sec.style.display='block';
  document.getElementById('vivo-count').textContent=vivos.length+' picks';
  document.getElementById('vivo-lista').innerHTML=vivos.map(renderVivoCard).join('');
}

function filterByDep(key){
  if(!DATA) return[];
  // Los tabs muestran los Gold Tips por deporte — sin repetir la sección superior
  // Solo mostramos los no-gold o todos filtrados por deporte
  const all=DATA.gold_tips||DATA.picks_validos||[];
  if(key==='todos') return all;
  if(key==='otros') return all.filter(p=>['MMA','Béisbol'].includes(p.deporte));
  const m={futbol:'Fútbol',tenis:'Tenis',basquet:'Básquet',esports:'Esports'};
  return all.filter(p=>p.deporte===m[key]);
}

function showTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  if(el) el.classList.add('active');
  currentTab=name;
  const det=document.getElementById('tab-detail');

  if(name==='descartados'){
    const desc=DATA?.picks_descartados||[];
    det.innerHTML=desc.length?desc.map(p=>`<div class="pick-card" style="opacity:.4;border-style:dashed">
      <div class="pick-meta" style="margin-bottom:5px">${ctxTag(p.contexto_id)}<span class="badge b-gray" style="font-size:10px">${p.deporte}</span><span class="pick-mercado">${p.mercado||'1X2'}</span></div>
      <div class="pick-evento">${p.evento}</div>
      <div style="font-size:12px;color:var(--text2)">Pick: ${p.equipo_pick} · @${fmt(p.odds_ref,2)}</div>
      <div style="font-size:11px;color:var(--red);margin-top:6px">✗ ${p.razon_descarte||'Descartado'}</div>
    </div>`).join(''):'<div class="empty">✓ Sin picks descartados.</div>';
    return;
  }

  if(name==='todos'){
    // En "Todos" no repetimos — mostramos mensaje guía
    det.innerHTML=`<div class="empty" style="padding:20px">
      <span class="empty-icon">👆</span>
      Usá los filtros de arriba para ver picks por deporte.<br>
      <span style="font-size:12px">Los Gold Tips están en la columna derecha.</span>
    </div>`;
    return;
  }

  const picks=filterByDep(name);
  if(!picks.length){
    det.innerHTML=`<div class="empty"><span class="empty-icon">🔍</span>Sin picks de ${name} en las próximas 48hs.</div>`;
    return;
  }
  det.innerHTML=picks.map(renderValueCard).join('');
}

function updateMetrics(){
  if(!DATA||!USER) return;
  const isPrem=USER.plan!=='free';
  document.getElementById('m-bank').textContent=fmtUSD(DATA.bankroll);
  document.getElementById('m-bank-sub').textContent=USER.moneda||'USD';
  document.getElementById('m-gold').textContent=isPrem?(DATA.gold_tips||[]).length:'🔒';
  document.getElementById('m-roi').textContent=isPrem?fmtPct(DATA.roi_gold_potencial):'🔒';
  document.getElementById('m-sure').textContent=(DATA.sure_bets||[]).length;
  document.getElementById('m-roi-sure').textContent=isPrem?fmtPct(DATA.roi_sure_potencial):'🔒';
  document.getElementById('m-eventos').textContent=DATA.total_eventos||0;
  document.getElementById('m-ventana').textContent='próximas '+(DATA.ventana_horas||48)+'hs';
}

async function loadUser(){
  const r=await aFetch('/api/me');
  if(!r.ok){window.location.href='/login';return;}
  USER=await r.json();
  const pl={'free':'Gratuito','premium':'Premium','admin':'Admin'};
  const pc={'free':'b-gray','premium':'b-violet','admin':'b-blue'};
  let planLabel = pl[USER.plan]||USER.plan;
  if(USER.plan==='premium' && USER.fecha_vencimiento){
    const venc = new Date(USER.fecha_vencimiento);
    const hoy  = new Date();
    const dias = Math.ceil((venc-hoy)/(1000*60*60*24));
    if(dias<=3) planLabel += ' ⚠ '+dias+'d';
    else planLabel += ' · '+venc.toLocaleDateString('es-AR',{day:'2-digit',month:'2-digit'});
  }
  document.getElementById('plan-badge').textContent=planLabel;
  document.getElementById('plan-badge').className='badge '+(pc[USER.plan]||'b-gray');
  document.getElementById('freemium-banner').style.display=USER.plan==='free'?'flex':'none';
  if(USER.plan==='free') document.getElementById('btn-scan').style.display='none';
  if(USER.plan==='admin') document.getElementById('btn-admin').style.display='inline-flex';
  document.getElementById('p-bankroll').value=USER.bankroll||1000;
  document.getElementById('p-moneda').value=USER.moneda||'USD';
  document.getElementById('p-riesgo').value=USER.perfil_riesgo||'inteligente';
  document.getElementById('p-tgid').value=USER.tg_chat_id||'';
  document.getElementById('p-tgactivo').checked=USER.tg_activo||false;
}

function showPerfil(){document.getElementById('modal-perfil').style.display='flex';}
function hidePerfil(){document.getElementById('modal-perfil').style.display='none';}

async function guardarPerfil(){
  const data={
    bankroll:parseFloat(document.getElementById('p-bankroll').value)||1000,
    moneda:document.getElementById('p-moneda').value,
    perfil_riesgo:document.getElementById('p-riesgo').value,
    tg_chat_id:document.getElementById('p-tgid').value.trim(),
    tg_activo:document.getElementById('p-tgactivo').checked,
  };
  const r=await aFetch('/api/me/perfil',{method:'POST',body:JSON.stringify(data)});
  const d=await r.json();const msg=document.getElementById('perfil-msg');
  if(d.ok){msg.textContent='✓ Perfil guardado';USER={...USER,...data};setTimeout(hidePerfil,1500);fetchData();}
  else{msg.style.color='var(--red)';msg.textContent=d.error||'Error';}
}

async function fetchData(){
  try{
    const r=await aFetch('/api/picks');
    if(r.status===401){window.location.href='/login';return;}
    const d=await r.json();
    if(d.scanning&&!d.gold_tips){
      document.getElementById('sure-lista').innerHTML='<div class="empty"><span class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</span>Analizando mercados...</div>';
      document.getElementById('value-lista').innerHTML='<div class="empty"><span class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</span>Calculando picks...</div>';
      setTimeout(fetchData,5000);return;
    }
    DATA=d;
    _pickIdx=0; window._picks={};
    document.getElementById('scan-badge').className='badge b-teal';
    document.getElementById('scan-badge').textContent='● Live';
    document.getElementById('last-scan').textContent=d.ultimo_scan?'Último: '+d.ultimo_scan:'';
    updateMetrics();renderSure();renderValue();renderVivo();showTab(currentTab,null);
  }catch(e){setTimeout(fetchData,8000);}
}

async function triggerScan(){
  const btn=document.getElementById('btn-scan');
  btn.disabled=true;btn.textContent='↻ Escaneando...';
  await aFetch('/api/scan',{method:'POST'});
  setTimeout(()=>{btn.disabled=false;btn.textContent='↻ Escanear';fetchData();},3000);
}
async function doLogout(){await aFetch('/api/auth/logout',{method:'POST'});localStorage.removeItem('sb_token');window.location.href='/login';}
loadUser().then(fetchData);
setInterval(fetchData,300000);
</script></body></html>"""
