import os, asyncio, logging
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, Request, Response, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stakebot")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MIN", 30)) * 60

cache = {
    "resultado":     None,
    "ultimo_scan":   None,
    "scanning":      False,
    "error":         None,
    "gold_enviados": set(),
}

async def run_scan_bg():
    if cache["scanning"]:
        return
    cache["scanning"] = True
    cache["error"] = None
    try:
        from core.engine import escanear_mercado
        from core.notifier import notificar_usuarios_premium
        from core.database import get_all_users
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, escanear_mercado)
        cache["resultado"]   = resultado
        cache["ultimo_scan"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        gold_count = len(resultado.get("gold_tips", []))
        log.info(f"Scan OK — {len(resultado['picks_validos'])} picks · {gold_count} Gold Tips")
        usuarios = await get_all_users()
        cache["gold_enviados"] = notificar_usuarios_premium(
            resultado, usuarios, cache["gold_enviados"]
        )
    except Exception as e:
        cache["error"] = str(e)
        log.error(f"Error scan: {e}")
    finally:
        cache["scanning"] = False

async def scanner_loop():
    while True:
        await run_scan_bg()
        await asyncio.sleep(SCAN_INTERVAL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.database import init_db
    await init_db()
    asyncio.create_task(scanner_loop())
    yield

app = FastAPI(title="Stake Gold IA", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/debug/hash")
async def debug_hash():
    import hashlib
    salt = os.getenv("SECRET_KEY", "stakebot_salt_2025")
    pwd  = "admin1234"
    h    = hashlib.sha256(f"{salt}{pwd}".encode()).hexdigest()
    return {"secret_key_usado": salt, "hash_admin1234": h}

@app.get("/api/debug/login")
async def debug_login():
    import hashlib
    from core.database import get_pool
    salt = os.getenv("SECRET_KEY", "stakebot_salt_2025")
    pwd  = "admin1234"
    h    = hashlib.sha256(f"{salt}{pwd}".encode()).hexdigest()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, email, password_hash, plan, activo FROM usuarios WHERE email=$1", "admin@stakebot.com")
        if not row:
            return {"error": "usuario no encontrado"}
        return {
            "email_en_db":    row["email"],
            "hash_en_db":     row["password_hash"],
            "hash_calculado": h,
            "coinciden":      row["password_hash"] == h,
            "plan":           row["plan"],
            "activo":         row["activo"],
        }

async def require_auth(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    from core.database import get_user_by_token
    return await get_user_by_token(token)

def picks_para_usuario(user: dict) -> dict:
    resultado = cache["resultado"]
    if not resultado:
        return {"scanning": True}
    plan  = user["plan"] if user else "free"
    todos = resultado.get("picks_validos", [])
    if plan in ("premium", "admin"):
        picks_vis = todos
        gold_vis  = resultado.get("gold_tips", [])
        roi_vis   = resultado.get("roi_gold_potencial", 0)
        expo_vis  = resultado.get("expo_gold_usd", 0)
    else:
        picks_vis = [{**p, "stake_usd": None, "ganancia_pot": None,
                      "roi_diario_pct": None, "es_gold": False} for p in todos[:3]]
        gold_vis, roi_vis, expo_vis = [], None, None
    bankroll = user["bankroll"] if user else 1000
    return {
        "timestamp":          resultado["timestamp"],
        "ultimo_scan":        cache["ultimo_scan"],
        "scanning":           cache["scanning"],
        "total_eventos":      resultado.get("total_eventos", 0),
        "ventana_horas":      resultado.get("ventana_horas", 36),
        "picks_validos":      picks_vis,
        "picks_descartados":  resultado.get("picks_descartados", []) if plan in ("premium","admin") else [],
        "gold_tips":          gold_vis,
        "roi_gold_potencial": roi_vis,
        "expo_gold_usd":      expo_vis,
        "bankroll":           bankroll,
        "plan":               plan,
        "total_picks":        len(todos),
        "picks_bloqueados":   max(0, len(todos) - 3) if plan == "free" else 0,
    }

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
async def register(request: Request):
    data = await request.json()
    from core.database import crear_usuario
    result = await crear_usuario(
        email    = data.get("email", ""),
        username = data.get("username", ""),
        password = data.get("password", ""),
        codigo   = data.get("codigo", ""),
    )
    return JSONResponse(result)

@app.post("/api/auth/login")
async def do_login(request: Request, response: Response):
    data = await request.json()
    from core.database import login
    result = await login(data.get("email", ""), data.get("password", ""))
    if result["ok"]:
        response.set_cookie("session_token", result["token"],
                            max_age=30*24*3600, httponly=True, samesite="lax")
        return JSONResponse({"ok": True, "plan": result["user"]["plan"],
                             "username": result["user"]["username"]})
    return JSONResponse(result, status_code=401)

@app.post("/api/auth/logout")
async def do_logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        from core.database import logout
        await logout(token)
    response.delete_cookie("session_token")
    return JSONResponse({"ok": True})

@app.get("/api/me")
async def get_me(request: Request):
    user = await require_auth(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({
        "ok": True, "id": user["id"], "email": user["email"],
        "username": user["username"], "plan": user["plan"],
        "bankroll": user["bankroll"], "moneda": user["moneda"],
        "perfil_riesgo": user["perfil_riesgo"],
        "tg_chat_id": user["tg_chat_id"], "tg_activo": user["tg_activo"],
    })

@app.post("/api/me/perfil")
async def update_perfil_endpoint(request: Request):
    user = await require_auth(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    from core.database import update_perfil
    return JSONResponse(await update_perfil(user["id"], data))

@app.get("/api/picks")
async def get_picks(request: Request):
    user = await require_auth(request)
    if not user:
        return JSONResponse({"ok": False, "error": "No autenticado"}, status_code=401)
    return JSONResponse(picks_para_usuario(user))

@app.post("/api/scan")
async def trigger_scan(request: Request, background_tasks: BackgroundTasks):
    user = await require_auth(request)
    if not user or user["plan"] not in ("premium", "admin"):
        return JSONResponse({"ok": False}, status_code=403)
    if cache["scanning"]:
        return JSONResponse({"mensaje": "Scan en progreso"})
    background_tasks.add_task(run_scan_bg)
    return JSONResponse({"mensaje": "Scan iniciado"})

@app.get("/api/admin/usuarios")
async def admin_usuarios(request: Request):
    user = await require_auth(request)
    if not user or user["plan"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    from core.database import get_all_users
    return JSONResponse(await get_all_users())

@app.post("/api/admin/usuario/{user_id}/plan")
async def admin_set_plan(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    from core.database import set_user_plan
    await set_user_plan(user_id, data["plan"])
    return JSONResponse({"ok": True})

@app.post("/api/admin/usuario/{user_id}/activo")
async def admin_set_activo(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    from core.database import set_user_activo
    await set_user_activo(user_id, data["activo"])
    return JSONResponse({"ok": True})

@app.post("/api/admin/invitacion")
async def admin_crear_invitacion(request: Request):
    user = await require_auth(request)
    if not user or user["plan"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    data = await request.json()
    from core.database import crear_invitacion
    codigo = await crear_invitacion(
        plan     = data.get("plan", "premium"),
        max_usos = data.get("max_usos", 1),
        creado_por = user["id"],
    )
    return JSONResponse({"ok": True, "codigo": codigo})

@app.get("/api/admin/invitaciones")
async def admin_invitaciones(request: Request):
    user = await require_auth(request)
    if not user or user["plan"] != "admin":
        return JSONResponse({"ok": False}, status_code=403)
    from core.database import get_invitaciones
    return JSONResponse(await get_invitaciones())

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = await require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await require_auth(request)
    if not user or user["plan"] != "admin":
        return RedirectResponse("/login")
    return HTMLResponse(ADMIN_HTML)

# ── HTML (mismo que antes) ────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stake Gold IA</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--border:#2e3348;--text:#e8eaf0;--text2:#8b92a8;--gold:#f59e0b;--green:#22c55e;--red:#ef4444;--radius:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:36px 32px;width:100%;max-width:420px}
.logo{text-align:center;margin-bottom:28px}
.logo-icon{font-size:40px;margin-bottom:8px}
.logo-title{font-size:22px;font-weight:700;color:var(--gold)}
.logo-sub{font-size:13px;color:var(--text2);margin-top:4px}
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:24px}
.tab{flex:1;padding:10px;text-align:center;font-size:14px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent}
.tab.active{color:var(--text);border-bottom-color:var(--gold);font-weight:500}
.field{margin-bottom:16px}
.field label{display:block;font-size:12px;color:var(--text2);margin-bottom:6px}
.field input{width:100%;padding:10px 14px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;outline:none}
.field input:focus{border-color:var(--gold)}
.btn{width:100%;padding:12px;background:var(--gold);border:none;border-radius:8px;color:#000;font-size:14px;font-weight:600;cursor:pointer;margin-top:8px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.msg{padding:10px 14px;border-radius:8px;font-size:13px;margin-top:12px;text-align:center}
.msg-err{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.3)}
.msg-ok{background:rgba(34,197,94,.12);color:var(--green);border:1px solid rgba(34,197,94,.3)}
.plan-info{background:var(--bg3);border-radius:8px;padding:12px 14px;margin-bottom:16px;font-size:12px;color:var(--text2);line-height:1.7}
.plan-info strong{color:var(--gold)}
.hidden{display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">⭐</div>
    <div class="logo-title">Stake Gold IA</div>
    <div class="logo-sub">Señales profesionales · Stake.com</div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('login')">Ingresar</div>
    <div class="tab" onclick="showTab('register')">Registrarse</div>
  </div>
  <div id="form-login">
    <div class="field"><label>Email</label><input type="email" id="l-email" placeholder="tu@email.com"></div>
    <div class="field"><label>Contraseña</label><input type="password" id="l-pass" placeholder="••••••••" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <button class="btn" onclick="doLogin()" id="btn-login">Ingresar</button>
    <div id="login-msg"></div>
  </div>
  <div id="form-register" class="hidden">
    <div class="plan-info">
      <strong>Gratis:</strong> 3 señales/día.<br>
      <strong>Premium ($15,000 ARS/mes):</strong> Gold Tips + Telegram + ROI completo.<br>
      Con código de invitación accedés directo a Premium.
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
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email:document.getElementById('l-email').value,password:document.getElementById('l-pass').value})});
  const d=await r.json();
  btn.disabled=false;btn.textContent='Ingresar';
  if(d.ok){window.location.href=d.plan==='admin'?'/admin':'/';}
  else{const el=document.getElementById('login-msg');el.className='msg msg-err';el.textContent=d.error||'Error al ingresar';}
}
async function doRegister(){
  const btn=document.getElementById('btn-register');
  btn.disabled=true;btn.textContent='Creando cuenta...';
  const r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email:document.getElementById('r-email').value,username:document.getElementById('r-user').value,
      password:document.getElementById('r-pass').value,codigo:document.getElementById('r-code').value.toUpperCase()})});
  const d=await r.json();
  btn.disabled=false;btn.textContent='Crear cuenta';
  const el=document.getElementById('register-msg');
  if(d.ok){el.className='msg msg-ok';el.textContent=`Cuenta creada (plan ${d.plan}). Iniciá sesión.`;setTimeout(()=>showTab('login'),2000);}
  else{el.className='msg msg-err';el.textContent=d.error||'Error al registrar';}
}
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — Stake Gold IA</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--border:#2e3348;--text:#e8eaf0;--text2:#8b92a8;--gold:#f59e0b;--green:#22c55e;--red:#ef4444;--radius:10px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.logo{font-size:16px;font-weight:600;color:var(--gold)}
.btn{padding:7px 16px;border-radius:6px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer}
.btn-gold{background:var(--gold);color:#000;border-color:var(--gold);font-weight:600}
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
.b-gold{background:rgba(245,158,11,.15);color:var(--gold)}
.b-green{background:rgba(34,197,94,.12);color:var(--green)}
.b-gray{background:var(--bg3);color:var(--text2)}
.inv-box{background:var(--bg3);border-radius:8px;padding:12px 16px;margin-top:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.inv-code{font-family:monospace;font-size:20px;font-weight:700;color:var(--gold);letter-spacing:3px}
.field{display:flex;flex-direction:column;gap:4px;flex:1;min-width:120px}
.field label{font-size:11px;color:var(--text2)}
select,input{padding:7px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">⭐ Stake Gold IA — Panel Admin</div>
  <div style="display:flex;gap:8px">
    <button class="btn" onclick="window.location.href='/'">Dashboard</button>
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>
<div class="container">
  <div class="metrics">
    <div class="metric"><div class="metric-label">Usuarios totales</div><div class="metric-val" id="m-total">—</div></div>
    <div class="metric"><div class="metric-label">Premium</div><div class="metric-val" id="m-premium" style="color:var(--gold)">—</div></div>
    <div class="metric"><div class="metric-label">Freemium</div><div class="metric-val" id="m-free">—</div></div>
    <div class="metric"><div class="metric-label">Invitaciones</div><div class="metric-val" id="m-inv">—</div></div>
  </div>
  <div class="card">
    <div class="card-title">Generar código de invitación</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
      <div class="field"><label>Plan</label><select id="inv-plan"><option value="premium">Premium</option><option value="free">Free</option></select></div>
      <div class="field"><label>Usos máximos</label><input type="number" id="inv-usos" value="1" min="1" style="width:100px"></div>
      <button class="btn btn-gold" onclick="generarInvitacion()">Generar código</button>
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
async function cargarDatos(){
  const[ru,ri]=await Promise.all([fetch('/api/admin/usuarios').then(r=>r.json()),fetch('/api/admin/invitaciones').then(r=>r.json())]);
  const usuarios=Array.isArray(ru)?ru:[];const invs=Array.isArray(ri)?ri:[];
  document.getElementById('m-total').textContent=usuarios.length;
  document.getElementById('m-premium').textContent=usuarios.filter(u=>u.plan==='premium').length;
  document.getElementById('m-free').textContent=usuarios.filter(u=>u.plan==='free').length;
  document.getElementById('m-inv').textContent=invs.filter(i=>i.usado).length+'/'+invs.length;
  document.getElementById('tabla-usuarios').innerHTML=usuarios.map(u=>`<tr>
    <td><strong>${u.username}</strong></td>
    <td style="color:var(--text2);font-size:12px">${u.email}</td>
    <td><span class="badge ${u.plan==='premium'?'b-gold':u.plan==='admin'?'b-green':'b-gray'}">${u.plan}</span></td>
    <td>$${u.bankroll||1000} ${u.moneda||'USD'}</td>
    <td>${u.tg_activo?'<span class="badge b-green">✓</span>':'—'}</td>
    <td style="font-size:11px;color:var(--text2)">${u.fecha_registro?String(u.fecha_registro).substring(0,10):''}</td>
    <td style="display:flex;gap:6px;flex-wrap:wrap">
      <select onchange="cambiarPlan(${u.id},this.value)" style="font-size:11px;padding:4px 6px">
        <option ${u.plan==='free'?'selected':''} value="free">Free</option>
        <option ${u.plan==='premium'?'selected':''} value="premium">Premium</option>
        <option ${u.plan==='admin'?'selected':''} value="admin">Admin</option>
      </select>
      <button class="btn" style="font-size:11px;padding:4px 10px;color:${u.activo?'var(--red)':'var(--green)'}"
        onclick="toggleActivo(${u.id},${!u.activo})">${u.activo?'Desactivar':'Activar'}</button>
    </td></tr>`).join('');
  document.getElementById('tabla-invitaciones').innerHTML=invs.map(i=>`<tr>
    <td><code style="color:var(--gold)">${i.codigo}</code></td>
    <td><span class="badge ${i.plan==='premium'?'b-gold':'b-gray'}">${i.plan}</span></td>
    <td>${i.usos_actuales}/${i.max_usos}</td>
    <td>${i.usado?'<span class="badge b-gray">Agotado</span>':'<span class="badge b-green">Disponible</span>'}</td>
    <td style="font-size:11px;color:var(--text2)">${i.fecha_creacion?String(i.fecha_creacion).substring(0,10):''}</td>
  </tr>`).join('');
}
async function generarInvitacion(){
  const r=await fetch('/api/admin/invitacion',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({plan:document.getElementById('inv-plan').value,max_usos:parseInt(document.getElementById('inv-usos').value)||1})});
  const d=await r.json();
  if(d.ok){
    document.getElementById('inv-result').innerHTML=`<div class="inv-box">
      <div><div style="font-size:11px;color:var(--text2);margin-bottom:4px">Código generado:</div>
      <div class="inv-code">${d.codigo}</div></div>
      <button class="btn" onclick="navigator.clipboard.writeText('${d.codigo}');this.textContent='¡Copiado!'">Copiar</button>
    </div>`;
    cargarDatos();
  }
}
async function cambiarPlan(id,plan){await fetch('/api/admin/usuario/'+id+'/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan})});cargarDatos();}
async function toggleActivo(id,activo){await fetch('/api/admin/usuario/'+id+'/activo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({activo})});cargarDatos();}
async function doLogout(){await fetch('/api/auth/logout',{method:'POST'});window.location.href='/login';}
cargarDatos();setInterval(cargarDatos,30000);
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stake Gold IA</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--border:#2e3348;--text:#e8eaf0;--text2:#8b92a8;--text3:#555e7a;--green:#22c55e;--green-bg:rgba(34,197,94,.12);--red:#ef4444;--red-bg:rgba(239,68,68,.12);--amber:#f59e0b;--amber-bg:rgba(245,158,11,.12);--purple:#8b5cf6;--purple-bg:rgba(139,92,246,.12);--gold:#f59e0b;--gold-bg:rgba(245,158,11,.12);--radius:10px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
.logo-dot{width:8px;height:8px;border-radius:50%;background:#555e7a}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:500}
.b-green{background:var(--green-bg);color:var(--green)}.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-purple{background:var(--purple-bg);color:var(--purple)}.b-gray{background:var(--bg3);color:var(--text2)}
.b-gold{background:var(--gold-bg);color:var(--gold);border:1px solid rgba(245,158,11,.3)}
.btn{padding:7px 14px;border-radius:6px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer}
.btn:disabled{opacity:.5;cursor:not-allowed}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.metric-label{font-size:11px;color:var(--text2);margin-bottom:5px}
.metric-val{font-size:21px;font-weight:600}
.metric-sub{font-size:10px;color:var(--text3);margin-top:2px}
.gold-section{background:rgba(245,158,11,.05);border:1px solid rgba(245,158,11,.2);border-radius:var(--radius);padding:16px 18px;margin-bottom:16px}
.gold-title{font-size:15px;font-weight:600;color:var(--gold);margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.gold-pick{background:var(--bg2);border:1px solid rgba(245,158,11,.15);border-radius:var(--radius);padding:14px 16px;margin-bottom:8px}
.gold-pick:last-child{margin-bottom:0}
.gold-rank{width:28px;height:28px;border-radius:50%;background:var(--gold-bg);border:1px solid rgba(245,158,11,.4);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:var(--gold);flex-shrink:0}
.freemium-banner{background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.25);border-radius:var(--radius);padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:14px;overflow-x:auto}
.tab{padding:9px 16px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab.active{color:var(--text);border-bottom-color:var(--purple);font-weight:500}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.pill{padding:4px 12px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer}
.pill.active{background:var(--bg3);color:var(--text)}
.pick{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin-bottom:8px}
.pick:hover{border-color:#3e4560}
.pick-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.pick-info{flex:1;min-width:160px}
.pick-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:5px}
.pick-evento{font-size:14px;font-weight:600;margin-bottom:2px}
.pick-sub{font-size:12px;color:var(--text2)}
.bar-wrap{height:3px;background:var(--bg3);border-radius:2px;margin:8px 0 3px}
.bar-fill{height:3px;border-radius:2px}
.bar-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:10px}
.pick-bottom{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;padding-top:10px;border-top:1px solid var(--border)}
.pick-nums{display:flex;gap:16px;flex-wrap:wrap}
.num-label{font-size:10px;color:var(--text2);margin-bottom:2px}
.num-val{font-size:15px;font-weight:600}
.ctx-tag{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:500}
.ctx-clean{background:var(--green-bg);color:var(--green)}.ctx-warn{background:var(--amber-bg);color:var(--amber)}
.ctx-esport{background:var(--purple-bg);color:var(--purple)}
.empty{text-align:center;padding:40px 20px;color:var(--text2)}
.select{padding:4px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:24px;width:100%;max-width:400px}
.field{margin-bottom:14px}
.field label{display:block;font-size:11px;color:var(--text2);margin-bottom:5px}
.field input,.field select{width:100%;padding:8px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo"><div class="logo-dot" id="conn-dot"></div>⭐ Stake Gold IA<span id="plan-badge" class="badge b-gray" style="margin-left:4px">—</span></div>
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <span id="scan-badge" class="badge b-amber">Cargando...</span>
    <span id="last-scan" style="font-size:11px;color:var(--text2)"></span>
    <button class="btn" onclick="showPerfil()">⚙ Perfil</button>
    <button class="btn" id="btn-scan" onclick="triggerScan()">↻ Escanear</button>
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>
<div class="container">
  <div class="freemium-banner" id="freemium-banner" style="display:none">
    <div>
      <div style="font-weight:600;margin-bottom:3px">Plan gratuito — 3 señales/día</div>
      <div style="font-size:12px;color:var(--text2)">Activá Premium para Gold Tips, ROI detallado y alertas por Telegram.</div>
    </div>
    <button style="padding:8px 16px;background:var(--gold);border:none;border-radius:8px;color:#000;font-size:13px;font-weight:600;cursor:pointer">⭐ Activar Premium</button>
  </div>
  <div class="metrics">
    <div class="metric"><div class="metric-label">Bankroll</div><div class="metric-val" id="m-bank">—</div><div class="metric-sub" id="m-bank-sub">USD</div></div>
    <div class="metric"><div class="metric-label">⭐ Gold Tips</div><div class="metric-val" id="m-gold" style="color:var(--gold)">—</div><div class="metric-sub">hoy</div></div>
    <div class="metric"><div class="metric-label">ROI potencial</div><div class="metric-val" id="m-roi" style="color:var(--green)">—</div><div class="metric-sub">si todos son verdes</div></div>
    <div class="metric"><div class="metric-label">Exposición</div><div class="metric-val" id="m-expo">—</div><div class="metric-sub">USD en juego</div></div>
    <div class="metric"><div class="metric-label">Picks válidos</div><div class="metric-val" id="m-validos">—</div><div class="metric-sub" id="m-bloq"></div></div>
    <div class="metric"><div class="metric-label">Ventana</div><div class="metric-val" id="m-ventana">—</div><div class="metric-sub">próximas horas</div></div>
  </div>
  <div class="gold-section" id="gold-section" style="display:none">
    <div class="gold-title">
      <span>⭐ Gold Tips del día</span>
      <span style="font-size:12px;color:var(--text2)">ROI potencial: <strong id="gold-roi-val" style="color:var(--green)">—</strong> · Expo: <strong id="gold-expo-val">—</strong></span>
    </div>
    <div id="gold-lista"></div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('validos',this)">Picks del día</div>
    <div class="tab" onclick="showTab('descartados',this)">Descartados</div>
  </div>
  <div id="tab-validos">
    <div class="filters">
      <button class="pill active" onclick="filtrar('todos',this)">Todos</button>
      <button class="pill" onclick="filtrar('Fútbol',this)">Fútbol</button>
      <button class="pill" onclick="filtrar('Tenis',this)">Tenis</button>
      <button class="pill" onclick="filtrar('Básquet',this)">Básquet</button>
      <button class="pill" onclick="filtrar('Esports',this)">Esports</button>
      <button class="pill" onclick="filtrar('MMA',this)">MMA</button>
      <div style="margin-left:auto;display:flex;align-items:center;gap:6px">
        <span style="font-size:11px;color:var(--text2)">Edge mín:</span>
        <select class="select" id="min-edge" onchange="renderPicks()"><option value="3" selected>3%</option><option value="5">5%</option><option value="7">7%</option></select>
      </div>
    </div>
    <div id="lista-validos"></div>
  </div>
  <div id="tab-descartados" style="display:none"><div id="lista-descartados"></div></div>
</div>
<div class="modal-bg" id="modal-perfil" style="display:none" onclick="if(event.target===this)hidePerfil()">
  <div class="modal">
    <div style="font-size:16px;font-weight:600;margin-bottom:16px">⚙ Mi perfil</div>
    <div class="field"><label>Bankroll (USD)</label><input type="number" id="p-bankroll" min="50"></div>
    <div class="field"><label>Perfil de riesgo</label>
      <select id="p-riesgo">
        <option value="conservador">Conservador — stake 2% máx</option>
        <option value="inteligente">Inteligente — 3-5% (recomendado)</option>
        <option value="profesional">Profesional — Kelly completo</option>
      </select>
    </div>
    <div class="field"><label>Chat ID de Telegram</label><input type="text" id="p-tgid" placeholder="ej: 123456789"></div>
    <div class="field" style="display:flex;align-items:center;gap:10px">
      <input type="checkbox" id="p-tgactivo" style="width:auto">
      <label for="p-tgactivo" style="font-size:13px">Recibir Gold Tips por Telegram</label>
    </div>
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn" style="flex:1" onclick="hidePerfil()">Cancelar</button>
      <button style="flex:1;padding:8px;background:var(--gold);border:none;border-radius:6px;color:#000;font-weight:600;cursor:pointer" onclick="guardarPerfil()">Guardar</button>
    </div>
    <div id="perfil-msg" style="font-size:12px;color:var(--green);margin-top:8px;text-align:center"></div>
  </div>
</div>
<script>
let DATA=null,USER=null,currentFilter='todos',currentTab='validos';
function fmt(n,d=2){return n!=null?Number(n).toFixed(d):'—';}
function fmtUSD(n){return n!=null?'$'+fmt(n,2):'—';}
function fmtPct(n){return n!=null?(n>=0?'+':'')+fmt(n,1)+'%':'—';}
function edgeColor(e){return e>=0.10?'#8b5cf6':e>=0.06?'#22c55e':'#8b92a8';}
function ctxTag(id){const m={champion_early:['ctx-warn','⚠ Campeón'],relegated:['ctx-warn','⬇ Desc.'],esport:['ctx-esport','🎮 Esport'],clean:['ctx-clean','✓ OK']};const[cls,lbl]=m[id]||['ctx-warn',id];return `<span class="ctx-tag ${cls}">${lbl}</span>`;}
async function loadUser(){
  const r=await fetch('/api/me');
  if(!r.ok){window.location.href='/login';return;}
  USER=await r.json();
  const pl={'free':'Plan Gratuito','premium':'Plan Premium','admin':'Admin'};
  const pc={'free':'b-gray','premium':'b-gold','admin':'b-green'};
  document.getElementById('plan-badge').textContent=pl[USER.plan]||USER.plan;
  document.getElementById('plan-badge').className='badge '+(pc[USER.plan]||'b-gray');
  document.getElementById('freemium-banner').style.display=USER.plan==='free'?'flex':'none';
  if(USER.plan==='free') document.getElementById('btn-scan').style.display='none';
  document.getElementById('p-bankroll').value=USER.bankroll||1000;
  document.getElementById('p-riesgo').value=USER.perfil_riesgo||'inteligente';
  document.getElementById('p-tgid').value=USER.tg_chat_id||'';
  document.getElementById('p-tgactivo').checked=USER.tg_activo||false;
}
function renderGoldPick(p,rank){
  const dep={Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';
  return `<div class="gold-pick"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <div class="gold-rank">${rank}</div>
    <div style="flex:1;min-width:160px">
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px"><span class="badge b-gray" style="font-size:10px">${dep} ${p.deporte}</span><span style="font-size:10px;color:var(--text3)">${p.liga}</span>${p.hora_local?`<span style="font-size:10px;color:var(--amber)">🕐 ${p.hora_local}</span>`:''}</div>
      <div style="font-size:14px;font-weight:600;margin-bottom:2px">${p.evento}</div>
      <div style="font-size:12px;color:var(--text2)">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_stake,2)}</div>
    </div>
    <div style="display:flex;gap:14px;flex-wrap:wrap;text-align:center">
      <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">Edge</div><div style="font-size:17px;font-weight:700;color:${edgeColor(p.edge)}">+${(p.edge*100).toFixed(1)}%</div></div>
      <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">Stake</div><div style="font-size:17px;font-weight:700">${fmtUSD(p.stake_usd)}</div></div>
      <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">Ganancia pot.</div><div style="font-size:17px;font-weight:700;color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div></div>
      <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">ROI</div><div style="font-size:17px;font-weight:700;color:var(--green)">${fmtPct(p.roi_diario_pct)}</div></div>
    </div>
  </div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--text2)">
    📌 Buscá en <strong style="color:var(--text)">Stake.com</strong> — si paga ≥ @${fmt(p.odds_stake,2)} apostá <strong>${fmtUSD(p.stake_usd)}</strong>
  </div></div>`;
}
function renderPickCard(p){
  const dep={Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';
  const barW=Math.min(Math.abs(p.edge)/0.15*100,100).toFixed(1);const col=edgeColor(p.edge);
  const isPrem=USER&&USER.plan!=='free';
  return `<div class="pick"><div class="pick-top">
    <div class="pick-info">
      <div class="pick-meta">${p.es_gold?'<span class="badge b-gold">⭐ Gold</span>':''}<span class="badge b-gray" style="font-size:10px">${dep} ${p.deporte}</span><span style="font-size:10px;color:var(--text3)">${p.liga}</span>${p.hora_local?`<span style="font-size:10px;color:var(--amber)">🕐 ${p.hora_local}</span>`:''}${ctxTag(p.contexto_id)}</div>
      <div class="pick-evento">${p.evento}</div>
      <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_stake,2)}</div>
    </div>
    <div style="text-align:right"><div style="font-size:22px;font-weight:700;color:${col}">${p.edge>=0?'+':''}${(p.edge*100).toFixed(1)}%</div><div style="font-size:10px;color:var(--text2)">edge</div>${isPrem?`<div style="font-size:11px;color:var(--green);margin-top:2px">ROI ${fmtPct(p.roi_diario_pct)}</div>`:''}</div>
  </div>
  <div class="bar-wrap"><div class="bar-fill" style="width:${barW}%;background:${col}"></div></div>
  <div class="bar-labels"><span>Prob. mercado: ${(p.prob_ajustada*100).toFixed(1)}%</span><span>Implícita: ${(1/p.odds_stake*100).toFixed(1)}%</span></div>
  ${isPrem?`<div class="pick-bottom"><div class="pick-nums">
    <div><div class="num-label">Stake</div><div class="num-val">${fmtUSD(p.stake_usd)}</div></div>
    <div><div class="num-label">Ganancia pot.</div><div class="num-val" style="color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div></div>
    <div><div class="num-label">ROI</div><div class="num-val" style="color:var(--green)">${fmtPct(p.roi_diario_pct)}</div></div>
  </div><button class="btn" onclick="marcar(this)">✓ Colocado</button></div>`:`<div style="padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--text3)">🔒 Stake y ROI disponibles en Plan Premium</div>`}
  </div>`;
}
function renderGold(){
  if(!DATA) return;
  const gold=DATA.gold_tips||[];const sec=document.getElementById('gold-section');
  if(!gold.length||USER?.plan==='free'){sec.style.display='none';return;}
  sec.style.display='block';
  document.getElementById('gold-lista').innerHTML=gold.map((p,i)=>renderGoldPick(p,i+1)).join('');
  document.getElementById('gold-roi-val').textContent=fmtPct(DATA.roi_gold_potencial);
  document.getElementById('gold-expo-val').textContent=fmtUSD(DATA.expo_gold_usd);
}
function renderPicks(){
  if(!DATA) return;
  const minE=parseFloat(document.getElementById('min-edge').value)/100;
  let picks=(DATA.picks_validos||[]).filter(p=>p.edge>=minE);
  if(currentFilter!=='todos') picks=picks.filter(p=>p.deporte===currentFilter);
  const el=document.getElementById('lista-validos');
  el.innerHTML=picks.length?picks.map(renderPickCard).join(''):`<div class="empty">🔍 Sin picks con edge ≥ ${(minE*100).toFixed(0)}%</div>`;
  if(DATA.picks_bloqueados>0&&USER?.plan==='free')
    el.innerHTML+=`<div style="text-align:center;padding:20px;border:1px dashed var(--border);border-radius:var(--radius);color:var(--text2)">🔒 <strong>${DATA.picks_bloqueados} picks más</strong> en Plan Premium</div>`;
}
function renderDescartados(){
  if(!DATA) return;
  const el=document.getElementById('lista-descartados');const picks=DATA.picks_descartados||[];
  el.innerHTML=picks.length?picks.map(p=>`<div class="pick" style="opacity:.45;border-style:dashed">
    <div class="pick-meta" style="margin-bottom:5px">${ctxTag(p.contexto_id)}<span class="badge b-gray" style="font-size:10px">${p.deporte}</span></div>
    <div class="pick-evento">${p.evento}</div>
    <div style="font-size:12px;color:var(--text2)">Pick: ${p.equipo_pick} · @${fmt(p.odds_stake,2)}</div>
    <div style="font-size:11px;color:var(--red);margin-top:6px">✗ ${p.razon_descarte||'Descartado'}</div>
  </div>`).join(''):'<div class="empty">✓ Ningún pick descartado.</div>';
}
function updateMetrics(){
  if(!DATA||!USER) return;
  document.getElementById('m-bank').textContent=fmtUSD(DATA.bankroll);
  document.getElementById('m-bank-sub').textContent=USER.moneda||'USD';
  document.getElementById('m-gold').textContent=USER.plan==='free'?'🔒':(DATA.gold_tips||[]).length;
  document.getElementById('m-roi').textContent=USER.plan==='free'?'🔒':fmtPct(DATA.roi_gold_potencial);
  document.getElementById('m-expo').textContent=USER.plan==='free'?'🔒':fmtUSD(DATA.expo_gold_usd);
  document.getElementById('m-validos').textContent=(DATA.picks_validos||[]).length;
  document.getElementById('m-bloq').textContent=DATA.picks_bloqueados>0?`+${DATA.picks_bloqueados} bloqueados`:'edge ≥ 3%';
  document.getElementById('m-ventana').textContent=(DATA.ventana_horas||36)+'hs';
}
function filtrar(f,btn){currentFilter=f;document.querySelectorAll('.pill').forEach(b=>b.classList.remove('active'));btn.classList.add('active');renderPicks();}
function showTab(name,el){['validos','descartados'].forEach(t=>document.getElementById('tab-'+t).style.display=t===name?'block':'none');document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));el.classList.add('active');currentTab=name;if(name==='descartados')renderDescartados();}
function marcar(btn){btn.textContent='✓ Colocado';btn.style.color='#22c55e';btn.style.borderColor='#22c55e';btn.disabled=true;}
function showPerfil(){document.getElementById('modal-perfil').style.display='flex';}
function hidePerfil(){document.getElementById('modal-perfil').style.display='none';}
async function guardarPerfil(){
  const data={bankroll:parseFloat(document.getElementById('p-bankroll').value)||1000,perfil_riesgo:document.getElementById('p-riesgo').value,tg_chat_id:document.getElementById('p-tgid').value.trim(),tg_activo:document.getElementById('p-tgactivo').checked};
  const r=await fetch('/api/me/perfil',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const d=await r.json();const msg=document.getElementById('perfil-msg');
  if(d.ok){msg.textContent='✓ Perfil guardado';USER={...USER,...data};setTimeout(hidePerfil,1500);fetchData();}
  else{msg.style.color='var(--red)';msg.textContent=d.error||'Error al guardar';}
}
async function fetchData(){
  try{
    const r=await fetch('/api/picks');if(r.status===401){window.location.href='/login';return;}
    const d=await r.json();
    if(d.scanning&&!d.picks_validos){document.getElementById('lista-validos').innerHTML='<div class="empty"><div class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</div>Analizando mercados...</div>';setTimeout(fetchData,5000);return;}
    DATA=d;
    document.getElementById('scan-badge').className='badge b-green';document.getElementById('scan-badge').textContent='● Live';
    document.getElementById('conn-dot').style.background='#22c55e';
    document.getElementById('last-scan').textContent=d.ultimo_scan?'Último: '+d.ultimo_scan:'';
    updateMetrics();renderGold();renderPicks();if(currentTab==='descartados')renderDescartados();
  }catch(e){setTimeout(fetchData,8000);}
}
async function triggerScan(){const btn=document.getElementById('btn-scan');btn.disabled=true;btn.textContent='↻ Escaneando...';await fetch('/api/scan',{method:'POST'});setTimeout(()=>{btn.disabled=false;btn.textContent='↻ Escanear';fetchData();},3000);}
async function doLogout(){await fetch('/api/auth/logout',{method:'POST'});window.location.href='/login';}
loadUser().then(fetchData);setInterval(fetchData,300000);
</script>
</body>
</html>"""
