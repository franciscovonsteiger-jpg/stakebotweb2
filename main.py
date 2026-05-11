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
        sure_count = len(resultado.get("sure_bets", []))
        log.info(f"Scan OK — {len(resultado['picks_validos'])} value picks · {gold_count} Gold · {sure_count} Sure Bets")
        usuarios = await get_all_users()
        cache["gold_enviados"] = notificar_usuarios_premium(resultado, usuarios, cache["gold_enviados"])
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

app = FastAPI(title="InvestiaBet", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=True)

async def require_auth(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    from core.database import get_user_by_token
    return await get_user_by_token(token)

def picks_para_usuario(user: dict) -> dict:
    resultado = cache["resultado"]
    if not resultado:
        return {"scanning": True}
    plan     = user["plan"] if user else "free"
    bankroll = user.get("bankroll", 1000) if user else 1000
    todos    = resultado.get("picks_validos", [])
    sures    = resultado.get("sure_bets", [])

    if plan in ("premium", "admin"):
        picks_vis = todos
        gold_vis  = resultado.get("gold_tips", [])
        sure_vis  = sures
        sure_gold = resultado.get("sure_gold", [])
        roi_vis   = resultado.get("roi_gold_potencial", 0)
        expo_vis  = resultado.get("expo_gold_usd", 0)
        roi_sure  = resultado.get("roi_sure_garantizado", 0)
    else:
        picks_vis = [{**p, "stake_usd": None, "ganancia_pot": None,
                      "roi_diario_pct": None, "es_gold": False} for p in todos[:3]]
        sure_vis  = sures[:2]
        gold_vis, sure_gold, roi_vis, expo_vis, roi_sure = [], [], None, None, None

    return {
        "timestamp":            resultado["timestamp"],
        "ultimo_scan":          cache["ultimo_scan"],
        "scanning":             cache["scanning"],
        "total_eventos":        resultado.get("total_eventos", 0),
        "ventana_horas":        resultado.get("ventana_horas", 36),
        "picks_validos":        picks_vis,
        "picks_descartados":    resultado.get("picks_descartados", []) if plan in ("premium","admin") else [],
        "gold_tips":            gold_vis,
        "sure_bets":            sure_vis,
        "sure_gold":            sure_gold if plan in ("premium","admin") else [],
        "roi_gold_potencial":   roi_vis,
        "expo_gold_usd":        expo_vis,
        "roi_sure_garantizado": roi_sure,
        "bankroll":             bankroll,
        "plan":                 plan,
        "total_picks":          len(todos),
        "total_sure":           len(sures),
        "picks_bloqueados":     max(0, len(todos) - 3) if plan == "free" else 0,
    }

@app.get("/api/debug/hash")
async def debug_hash():
    import hashlib
    salt = os.getenv("SECRET_KEY", "stakebot_salt_2025")
    h    = hashlib.sha256(f"{salt}admin1234".encode()).hexdigest()
    return {"secret_key_usado": salt, "hash_admin1234": h}

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
    token = request.cookies.get("session_token") or ""
    auth  = request.headers.get("Authorization","")
    if not token and auth.startswith("Bearer "):
        token = auth[7:]
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
        "tg_chat_id":user["tg_chat_id"],"tg_activo":user["tg_activo"]})

@app.post("/api/me/perfil")
async def update_perfil_ep(request: Request):
    user = await require_auth(request)
    if not user: return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    from core.database import update_perfil
    return JSONResponse(await update_perfil(user["id"], data))

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

@app.get("/api/admin/usuarios")
async def admin_usuarios(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    from core.database import get_all_users
    return JSONResponse(await get_all_users())

@app.post("/api/admin/usuario/{user_id}/plan")
async def admin_set_plan(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import set_user_plan
    await set_user_plan(user_id, data["plan"])
    return JSONResponse({"ok":True})

@app.post("/api/admin/usuario/{user_id}/activo")
async def admin_set_activo(user_id: int, request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import set_user_activo
    await set_user_activo(user_id, data["activo"])
    return JSONResponse({"ok":True})

@app.post("/api/admin/invitacion")
async def admin_crear_inv(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    data = await request.json()
    from core.database import crear_invitacion
    codigo = await crear_invitacion(plan=data.get("plan","premium"),
                                   max_usos=data.get("max_usos",1), creado_por=user["id"])
    return JSONResponse({"ok":True,"codigo":codigo})

@app.get("/api/admin/invitaciones")
async def admin_invitaciones(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return JSONResponse({"ok":False},status_code=403)
    from core.database import get_invitaciones
    return JSONResponse(await get_invitaciones())

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = await require_auth(request)
    if not user: return RedirectResponse("/login")
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/login", response_class=HTMLResponse)
async def login_page(): return HTMLResponse(LOGIN_HTML)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await require_auth(request)
    if not user or user["plan"]!="admin": return RedirectResponse("/login")
    return HTMLResponse(ADMIN_HTML)

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InvestiaBet</title>
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131929;--border:#1e2a3d;--text:#e2e8f4;--text2:#7a8aaa;--blue:#4f8ef7;--violet:#7c5ff7;--teal:#00d4aa;--gold:#f59e0b;--red:#ef4444;--radius:14px}
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
.field label{display:block;font-size:12px;color:var(--text2);margin-bottom:7px;letter-spacing:.3px}
.field input{width:100%;padding:11px 14px;background:var(--bg3);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;outline:none;transition:border-color .2s}
.field input:focus{border-color:var(--blue)}
.btn{width:100%;padding:13px;background:linear-gradient(135deg,var(--blue),var(--violet));border:none;border-radius:10px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;margin-top:8px;transition:opacity .2s}
.btn:hover{opacity:.9}
.btn:disabled{opacity:.5;cursor:not-allowed}
.msg{padding:11px 14px;border-radius:10px;font-size:13px;margin-top:12px;text-align:center}
.msg-err{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.msg-ok{background:rgba(0,212,170,.1);color:var(--teal);border:1px solid rgba(0,212,170,.2)}
.plan-info{background:var(--bg3);border-radius:10px;padding:13px 15px;margin-bottom:18px;font-size:12px;color:var(--text2);line-height:1.8;border:1px solid var(--border)}
.plan-info strong{color:var(--teal)}
.hidden{display:none}
</style>
</head>
<body>
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
    <div class="field"><label>EMAIL</label><input type="email" id="l-email" placeholder="tu@email.com" autocomplete="email"></div>
    <div class="field"><label>CONTRASEÑA</label><input type="password" id="l-pass" placeholder="••••••••" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <button class="btn" onclick="doLogin()" id="btn-login">Ingresar</button>
    <div id="login-msg"></div>
  </div>
  <div id="form-register" class="hidden">
    <div class="plan-info">
      <strong>Gratis:</strong> 3 señales/día · 2 sure bets.<br>
      <strong>Premium:</strong> Gold Tips completos + Sure Bets + Telegram + ROI.<br>
      Con código de invitación → Premium inmediato.
    </div>
    <div class="field"><label>EMAIL</label><input type="email" id="r-email" placeholder="tu@email.com"></div>
    <div class="field"><label>USUARIO</label><input type="text" id="r-user" placeholder="nombre de usuario"></div>
    <div class="field"><label>CONTRASEÑA</label><input type="password" id="r-pass" placeholder="mínimo 6 caracteres"></div>
    <div class="field"><label>CÓDIGO DE INVITACIÓN (opcional)</label><input type="text" id="r-code" placeholder="XXXXXXXX" style="text-transform:uppercase"></div>
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
  const d=await r.json();
  btn.disabled=false;btn.textContent='Crear cuenta';
  const el=document.getElementById('register-msg');
  if(d.ok){el.className='msg msg-ok';el.textContent='Cuenta creada (plan '+d.plan+'). Iniciá sesión.';setTimeout(()=>showTab('login'),2000);}
  else{el.className='msg msg-err';el.textContent=d.error||'Error al registrar';}
}
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
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
.b-blue{background:rgba(79,142,247,.15);color:var(--blue)}
.b-violet{background:rgba(124,95,247,.15);color:var(--violet)}
.b-teal{background:rgba(0,212,170,.12);color:var(--teal)}
.b-gray{background:var(--bg3);color:var(--text2)}
.inv-box{background:var(--bg3);border-radius:10px;padding:14px 16px;margin-top:10px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.inv-code{font-family:monospace;font-size:22px;font-weight:700;color:var(--teal);letter-spacing:3px}
.field{display:flex;flex-direction:column;gap:4px;flex:1;min-width:120px}
.field label{font-size:11px;color:var(--text2)}
select,input{padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px}
</style>
</head>
<body>
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
    <div class="card-title">Usuarios</div>
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
      <td>$${x.bankroll||1000}</td>
      <td>${x.tg_activo?'<span class="badge b-teal">✓</span>':'—'}</td>
      <td style="font-size:11px;color:var(--text2)">${x.fecha_registro?String(x.fecha_registro).substring(0,10):''}</td>
      <td style="display:flex;gap:6px">
        <select onchange="cambiarPlan(${x.id},this.value)" style="font-size:11px;padding:4px 6px">
          <option ${x.plan==='free'?'selected':''} value="free">Free</option>
          <option ${x.plan==='premium'?'selected':''} value="premium">Premium</option>
          <option ${x.plan==='admin'?'selected':''} value="admin">Admin</option>
        </select>
        <button class="btn" style="font-size:11px;padding:4px 10px;color:${x.activo?'var(--red)':'var(--teal)'}"
          onclick="toggleActivo(${x.id},${!x.activo})">${x.activo?'Desactivar':'Activar'}</button>
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
      <div><div style="font-size:11px;color:var(--text2);margin-bottom:5px">Código generado — compartilo con el usuario:</div>
      <div class="inv-code">${d.codigo}</div></div>
      <button class="btn" onclick="navigator.clipboard.writeText('${d.codigo}');this.textContent='¡Copiado!'">Copiar</button>
    </div>`;
    cargarDatos();
  }
}
async function cambiarPlan(id,plan){await fetch('/api/admin/usuario/'+id+'/plan',{method:'POST',credentials:'include',headers:authH(),body:JSON.stringify({plan})});cargarDatos();}
async function toggleActivo(id,activo){await fetch('/api/admin/usuario/'+id+'/activo',{method:'POST',credentials:'include',headers:authH(),body:JSON.stringify({activo})});cargarDatos();}
async function doLogout(){await fetch('/api/auth/logout',{method:'POST',credentials:'include',headers:authH()});localStorage.removeItem('sb_token');window.location.href='/login';}
cargarDatos();setInterval(cargarDatos,30000);
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InvestiaBet</title>
<style>
:root{
  --bg:#080c14;--bg2:#0d1220;--bg3:#131929;--bg4:#171f2e;
  --border:#1e2a3d;--border2:#243347;
  --text:#e2e8f4;--text2:#7a8aaa;--text3:#3d4f6a;
  --blue:#4f8ef7;--blue-bg:rgba(79,142,247,.1);--blue-border:rgba(79,142,247,.25);
  --violet:#7c5ff7;--violet-bg:rgba(124,95,247,.1);--violet-border:rgba(124,95,247,.25);
  --teal:#00d4aa;--teal-bg:rgba(0,212,170,.08);--teal-border:rgba(0,212,170,.2);
  --green:#22c55e;--green-bg:rgba(34,197,94,.1);
  --red:#ef4444;--red-bg:rgba(239,68,68,.1);
  --amber:#f59e0b;--amber-bg:rgba(245,158,11,.1);
  --radius:12px;--radius-sm:8px
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);background-image:radial-gradient(ellipse at 10% 30%,rgba(79,142,247,.04) 0%,transparent 50%),radial-gradient(ellipse at 90% 70%,rgba(124,95,247,.04) 0%,transparent 50%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}

/* Topbar */
.topbar{background:rgba(13,18,32,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:13px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:17px;font-weight:700;display:flex;align-items:center;gap:10px}
.logo-text{background:linear-gradient(135deg,var(--blue),var(--violet),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.logo-dot{width:8px;height:8px;border-radius:50%;background:var(--teal);box-shadow:0 0 8px var(--teal)}

/* Badges */
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:500;white-space:nowrap}
.b-blue{background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}
.b-violet{background:var(--violet-bg);color:var(--violet);border:1px solid var(--violet-border)}
.b-teal{background:var(--teal-bg);color:var(--teal);border:1px solid var(--teal-border)}
.b-green{background:var(--green-bg);color:var(--green)}
.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-red{background:var(--red-bg);color:var(--red)}
.b-gray{background:var(--bg3);color:var(--text2)}

/* Buttons */
.btn{padding:7px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer;transition:all .15s}
.btn:hover{background:var(--bg4);border-color:var(--border2)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-grad{background:linear-gradient(135deg,var(--blue),var(--violet));border:none;color:#fff;font-weight:600;padding:9px 18px;border-radius:var(--radius-sm);cursor:pointer;font-size:13px;transition:opacity .2s}
.btn-grad:hover{opacity:.85}

/* Layout */
.container{max-width:1300px;margin:0 auto;padding:20px 16px}

/* Metrics */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;position:relative;overflow:hidden}
.metric::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.metric.m-blue::before{background:linear-gradient(90deg,var(--blue),transparent)}
.metric.m-violet::before{background:linear-gradient(90deg,var(--violet),transparent)}
.metric.m-teal::before{background:linear-gradient(90deg,var(--teal),transparent)}
.metric.m-green::before{background:linear-gradient(90deg,var(--green),transparent)}
.metric-label{font-size:10px;color:var(--text2);margin-bottom:6px;letter-spacing:.5px;text-transform:uppercase}
.metric-val{font-size:22px;font-weight:700}
.metric-sub{font-size:10px;color:var(--text3);margin-top:3px}

/* Two column layout */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* Column headers */
.col-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.col-title{font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
.col-sub{font-size:11px;color:var(--text2)}

/* Sure Bet cards */
.sure-card{background:var(--bg2);border:1px solid var(--teal-border);border-radius:var(--radius);padding:16px;margin-bottom:10px;position:relative;overflow:hidden}
.sure-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--teal),var(--blue))}
.sure-card:hover{border-color:var(--teal)}
.sure-roi{font-size:26px;font-weight:700;color:var(--teal)}
.sure-roi-label{font-size:10px;color:var(--text2);margin-top:1px}
.sure-legs{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.sure-leg{background:var(--bg3);border-radius:var(--radius-sm);padding:10px 12px}
.sure-leg-pick{font-size:13px;font-weight:600;margin-bottom:3px}
.sure-leg-detail{font-size:11px;color:var(--text2)}
.sure-leg-odds{font-size:16px;font-weight:700;color:var(--blue);margin-top:4px}

/* Value Bet cards */
.pick-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin-bottom:10px;transition:border-color .15s;position:relative;overflow:hidden}
.pick-card.gold::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--violet),var(--blue))}
.pick-card:hover{border-color:var(--border2)}
.pick-card.gold{border-color:var(--violet-border)}
.pick-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.pick-info{flex:1;min-width:140px}
.pick-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:6px}
.pick-evento{font-size:14px;font-weight:600;margin-bottom:2px}
.pick-sub{font-size:12px;color:var(--text2)}
.pick-mercado{font-size:10px;padding:2px 8px;border-radius:20px;background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}
.edge-val{font-size:24px;font-weight:700;text-align:right}
.edge-lbl{font-size:10px;color:var(--text2);text-align:right}
.bar-wrap{height:3px;background:var(--border);border-radius:2px;margin:10px 0 3px}
.bar-fill{height:3px;border-radius:2px}
.bar-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:10px}
.pick-bottom{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;padding-top:10px;border-top:1px solid var(--border)}
.pick-nums{display:flex;gap:14px;flex-wrap:wrap}
.num-label{font-size:10px;color:var(--text2);margin-bottom:2px}
.num-val{font-size:15px;font-weight:700}

/* Tabs */
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:14px;overflow-x:auto}
.tab{padding:9px 16px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--blue);border-bottom-color:var(--blue);font-weight:500}

/* Filters */
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.pill{padding:5px 12px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer;transition:all .15s}
.pill:hover{color:var(--text);border-color:var(--border2)}
.pill.active{background:var(--blue-bg);color:var(--blue);border-color:var(--blue-border)}

/* Misc */
.ctx-tag{display:inline-flex;gap:3px;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:500}
.ctx-clean{background:var(--green-bg);color:var(--green)}
.ctx-warn{background:var(--amber-bg);color:var(--amber)}
.ctx-esport{background:var(--violet-bg);color:var(--violet)}
.empty{text-align:center;padding:40px 20px;color:var(--text2)}
.empty-icon{font-size:32px;display:block;margin-bottom:10px;opacity:.4}
.select{padding:4px 8px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px}
.freemium-banner{background:linear-gradient(135deg,rgba(124,95,247,.08),rgba(79,142,247,.08));border:1px solid var(--violet-border);border-radius:var(--radius);padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.8);backdrop-filter:blur(4px);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:26px;width:100%;max-width:400px}
.field{margin-bottom:14px}
.field label{display:block;font-size:11px;color:var(--text2);margin-bottom:5px;letter-spacing:.3px;text-transform:uppercase}
.field input,.field select{width:100%;padding:9px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:13px;outline:none}
.field input:focus{border-color:var(--blue)}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.lock-row{padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--text3);display:flex;align-items:center;gap:5px}
</style>
</head>
<body>

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
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>

<div class="container">

  <div class="freemium-banner" id="freemium-banner" style="display:none">
    <div>
      <div style="font-weight:600;margin-bottom:3px;color:var(--violet)">Plan Gratuito</div>
      <div style="font-size:12px;color:var(--text2)">Ves 3 value bets y 2 sure bets. Activá Premium para acceso completo, Gold Tips y alertas Telegram.</div>
    </div>
    <button class="btn-grad">⭐ Activar Premium</button>
  </div>

  <div class="metrics">
    <div class="metric m-teal">
      <div class="metric-label">Sure Bets hoy</div>
      <div class="metric-val" id="m-sure" style="color:var(--teal)">—</div>
      <div class="metric-sub">ganancia garantizada</div>
    </div>
    <div class="metric m-teal">
      <div class="metric-label">ROI sure garantizado</div>
      <div class="metric-val" id="m-roi-sure" style="color:var(--teal)">—</div>
      <div class="metric-sub">sin riesgo</div>
    </div>
    <div class="metric m-violet">
      <div class="metric-label">⭐ Gold Tips valor</div>
      <div class="metric-val" id="m-gold" style="color:var(--violet)">—</div>
      <div class="metric-sub">mejores picks</div>
    </div>
    <div class="metric m-violet">
      <div class="metric-label">ROI value potencial</div>
      <div class="metric-val" id="m-roi" style="color:var(--violet)">—</div>
      <div class="metric-sub">si todos son verdes</div>
    </div>
    <div class="metric m-blue">
      <div class="metric-label">Bankroll</div>
      <div class="metric-val" id="m-bank" style="color:var(--blue)">—</div>
      <div class="metric-sub" id="m-bank-sub">USD</div>
    </div>
    <div class="metric m-blue">
      <div class="metric-label">Eventos analizados</div>
      <div class="metric-val" id="m-eventos" style="color:var(--blue)">—</div>
      <div class="metric-sub" id="m-ventana">próximas horas</div>
    </div>
  </div>

  <!-- DOS COLUMNAS PRINCIPALES -->
  <div class="two-col">

    <!-- COLUMNA 1: SURE BETS -->
    <div>
      <div class="col-header">
        <div class="col-title">
          <span style="color:var(--teal)">🔒</span>
          <span style="color:var(--teal)">Sure Bets</span>
          <span class="badge b-teal" style="font-size:10px">Ganancia garantizada</span>
        </div>
        <div class="col-sub" id="sure-count-label"></div>
      </div>
      <div id="sure-lista">
        <div class="empty"><span class="empty-icon">🔒</span>Escaneando mercados...</div>
      </div>
    </div>

    <!-- COLUMNA 2: VALUE BETS / GOLD TIPS -->
    <div>
      <div class="col-header">
        <div class="col-title">
          <span style="color:var(--violet)">⭐</span>
          <span style="color:var(--violet)">Value Bets</span>
          <span class="badge b-violet" style="font-size:10px">Gold Tips del día</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:11px;color:var(--text2)">Edge mín:</span>
          <select class="select" id="min-edge" onchange="renderValue()">
            <option value="3" selected>3%</option>
            <option value="5">5%</option>
            <option value="7">7%</option>
          </select>
        </div>
      </div>
      <div id="value-lista">
        <div class="empty"><span class="empty-icon">📊</span>Escaneando mercados...</div>
      </div>
    </div>

  </div>

  <!-- TABS DETALLE -->
  <div class="tabs">
    <div class="tab active" onclick="showTab('todos',this)">Todos los picks</div>
    <div class="tab" onclick="showTab('futbol',this)">Fútbol</div>
    <div class="tab" onclick="showTab('tenis',this)">Tenis</div>
    <div class="tab" onclick="showTab('basquet',this)">Básquet</div>
    <div class="tab" onclick="showTab('esports',this)">Esports</div>
    <div class="tab" onclick="showTab('otros',this)">MMA / Béisbol</div>
    <div class="tab" onclick="showTab('descartados',this)">Descartados</div>
  </div>
  <div id="tab-detail"></div>

</div>

<!-- Modal perfil -->
<div class="modal-bg" id="modal-perfil" style="display:none" onclick="if(event.target===this)hidePerfil()">
  <div class="modal">
    <div style="font-size:16px;font-weight:600;margin-bottom:18px;color:var(--blue)">⚙ Mi perfil</div>
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
      <input type="checkbox" id="p-tgactivo" style="width:auto;accent-color:var(--teal)">
      <label for="p-tgactivo" style="font-size:13px;color:var(--text)">Recibir Gold Tips + Sure Bets por Telegram</label>
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

function getToken(){return localStorage.getItem('sb_token')||'';}
function authH(){const t=getToken();return t?{'Authorization':'Bearer '+t,'Content-Type':'application/json'}:{'Content-Type':'application/json'};}
async function aFetch(url,opts={}){opts.credentials='include';opts.headers={...authH(),...(opts.headers||{})};return fetch(url,opts);}
function fmt(n,d=2){return n!=null?Number(n).toFixed(d):'—';}
function fmtUSD(n){return n!=null?'$'+fmt(n,2):'—';}
function fmtPct(n){return n!=null?(n>=0?'+':'')+fmt(n,1)+'%':'—';}

function edgeColor(e){
  if(e>=0.12) return 'var(--teal)';
  if(e>=0.07) return 'var(--blue)';
  if(e>=0.04) return 'var(--violet)';
  return 'var(--text2)';
}

function ctxTag(id){
  const m={champion_early:['ctx-warn','⚠ Campeón'],relegated:['ctx-warn','⬇ Desc.'],esport:['ctx-esport','🎮 Esport'],clean:['ctx-clean','✓ OK']};
  const[cls,lbl]=m[id]||['ctx-warn',id];
  return `<span class="ctx-tag ${cls}">${lbl}</span>`;
}

function depEmoji(d){return{Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[d]||'🎯';}

// ── Sure Bet card ──────────────────────────────────────────────────────────────
function renderSureCard(s){
  const isPrem=USER&&USER.plan!=='free';
  return `<div class="sure-card">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap">
      <div style="flex:1;min-width:140px">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:5px">
          <span class="badge b-teal" style="font-size:10px">SURE BET</span>
          <span class="badge b-gray" style="font-size:10px">${depEmoji(s.deporte)} ${s.deporte}</span>
          <span style="font-size:10px;color:var(--text2)">${s.liga}</span>
        </div>
        <div style="font-size:14px;font-weight:600;margin-bottom:2px">${s.evento}</div>
        <div style="font-size:11px;color:var(--text2)">${s.mercado} ${s.hora_local?'· 🕐 '+s.hora_local:''}</div>
      </div>
      <div style="text-align:right">
        <div class="sure-roi">+${fmt(s.roi_garantizado)}%</div>
        <div class="sure-roi-label">ROI garantizado</div>
        ${isPrem?`<div style="font-size:11px;color:var(--teal);margin-top:4px">+${fmtUSD(s.ganancia_garantizada)} USD</div>`:''}
      </div>
    </div>
    ${isPrem?`
    <div class="sure-legs">
      <div class="sure-leg">
        <div class="sure-leg-pick">${s.pick_a}</div>
        <div class="sure-leg-detail">en ${s.casa_a}</div>
        <div class="sure-leg-odds">@${fmt(s.odds_a,2)}</div>
        <div style="font-size:11px;color:var(--text2);margin-top:4px">Stake: <strong style="color:var(--text)">${fmtUSD(s.stake_a)}</strong></div>
      </div>
      <div class="sure-leg">
        <div class="sure-leg-pick">${s.pick_b}</div>
        <div class="sure-leg-detail">en ${s.casa_b}</div>
        <div class="sure-leg-odds">@${fmt(s.odds_b,2)}</div>
        <div style="font-size:11px;color:var(--text2);margin-top:4px">Stake: <strong style="color:var(--text)">${fmtUSD(s.stake_b)}</strong></div>
      </div>
    </div>
    <div style="margin-top:10px;padding:8px 12px;background:var(--teal-bg);border-radius:var(--radius-sm);font-size:12px;color:var(--teal);display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <span>Inversión total: <strong>${fmtUSD(s.inversion_total)}</strong></span>
      <span>Ganancia mínima garantizada: <strong>+${fmtUSD(s.ganancia_garantizada)}</strong></span>
    </div>`:`
    <div class="lock-row">🔒 Detalle de stakes disponible en Plan Premium</div>`}
  </div>`;
}

// ── Value Pick card ────────────────────────────────────────────────────────────
function renderValueCard(p){
  const isPrem=USER&&USER.plan!=='free';
  const barW=Math.min(Math.abs(p.edge)/0.15*100,100).toFixed(1);
  const col=edgeColor(p.edge);
  return `<div class="pick-card${p.es_gold?' gold':''}">
    <div class="pick-top">
      <div class="pick-info">
        <div class="pick-meta">
          ${p.es_gold?'<span class="badge b-violet">⭐ Gold</span>':''}
          <span class="badge b-gray" style="font-size:10px">${depEmoji(p.deporte)} ${p.deporte}</span>
          <span class="pick-mercado">${p.mercado||'1X2'}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--amber)">🕐 ${p.hora_local}</span>`:''}
          ${ctxTag(p.contexto_id)}
        </div>
        <div class="pick-evento">${p.evento}</div>
        <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_ref||p.odds_stake,2)} · ${p.liga}</div>
      </div>
      <div>
        <div class="edge-val" style="color:${col}">${p.edge>=0?'+':''}${(p.edge*100).toFixed(1)}%</div>
        <div class="edge-lbl">edge</div>
        ${isPrem&&p.roi_diario_pct!=null?`<div style="font-size:11px;margin-top:2px;text-align:right;color:var(--violet)">ROI ${fmtPct(p.roi_diario_pct)}</div>`:''}
      </div>
    </div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${barW}%;background:${col}"></div></div>
    <div class="bar-labels"><span>Prob: ${(p.prob_ajustada*100).toFixed(1)}%</span><span>Implícita: ${(1/(p.odds_ref||p.odds_stake)*100).toFixed(1)}%</span></div>
    ${isPrem&&p.stake_usd!=null?`
    <div class="pick-bottom">
      <div class="pick-nums">
        <div><div class="num-label">Stake</div><div class="num-val" style="color:var(--blue)">${fmtUSD(p.stake_usd)}</div></div>
        <div><div class="num-label">Ganancia pot.</div><div class="num-val" style="color:var(--violet)">+${fmtUSD(p.ganancia_pot)}</div></div>
        <div><div class="num-label">ROI bankroll</div><div class="num-val" style="color:var(--teal)">${fmtPct(p.roi_diario_pct)}</div></div>
      </div>
      <button class="btn" style="font-size:12px" onclick="marcar(this,p)">✓ Colocado</button>
    </div>`:`<div class="lock-row">🔒 Stake y ROI en Plan Premium</div>`}
  </div>`;
}

// ── Renders ────────────────────────────────────────────────────────────────────
function renderSure(){
  if(!DATA) return;
  const sures=DATA.sure_bets||[];
  const el=document.getElementById('sure-lista');
  document.getElementById('sure-count-label').textContent=sures.length+' encontradas';
  if(!sures.length){el.innerHTML='<div class="empty"><span class="empty-icon">🔍</span>Sin sure bets en este momento.<br><span style="font-size:12px">El motor escanea cada 30 min.</span></div>';return;}
  el.innerHTML=sures.map(renderSureCard).join('');
}

function renderValue(){
  if(!DATA) return;
  const minE=parseFloat(document.getElementById('min-edge').value)/100;
  const picks=(DATA.gold_tips&&DATA.gold_tips.length?DATA.gold_tips:DATA.picks_validos||[]).filter(p=>p.edge>=minE);
  const el=document.getElementById('value-lista');
  if(!picks.length){el.innerHTML='<div class="empty"><span class="empty-icon">📊</span>Sin value bets con edge ≥ '+(minE*100).toFixed(0)+'%</div>';return;}
  el.innerHTML=picks.slice(0,8).map(renderValueCard).join('');
}

function filterByDeporte(deporte){
  if(!DATA) return[];
  const all=DATA.picks_validos||[];
  if(deporte==='todos') return all;
  if(deporte==='otros') return all.filter(p=>['MMA','Béisbol'].includes(p.deporte));
  const map={futbol:'Fútbol',tenis:'Tenis',basquet:'Básquet',esports:'Esports'};
  return all.filter(p=>p.deporte===map[deporte]);
}

function showTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  if(el) el.classList.add('active');
  currentTab=name;
  const detail=document.getElementById('tab-detail');
  if(name==='descartados'){
    const desc=DATA?.picks_descartados||[];
    if(!desc.length){detail.innerHTML='<div class="empty">✓ Sin picks descartados.</div>';return;}
    detail.innerHTML=desc.map(p=>`<div class="pick-card" style="opacity:.4;border-style:dashed">
      <div class="pick-meta" style="margin-bottom:5px">${ctxTag(p.contexto_id)}<span class="badge b-gray" style="font-size:10px">${p.deporte}</span><span class="pick-mercado">${p.mercado||'1X2'}</span></div>
      <div class="pick-evento">${p.evento}</div>
      <div style="font-size:12px;color:var(--text2)">Pick: ${p.equipo_pick} · @${fmt(p.odds_ref||p.odds_stake,2)}</div>
      <div style="font-size:11px;color:var(--red);margin-top:6px">✗ ${p.razon_descarte||'Descartado'}</div>
    </div>`).join('');
    return;
  }
  const picks=filterByDeporte(name);
  if(!picks.length){detail.innerHTML='<div class="empty"><span class="empty-icon">'+(name==='todos'?'🔍':'⚽')+'</span>Sin picks en este filtro.</div>';return;}
  detail.innerHTML=picks.map(renderValueCard).join('');
}

function updateMetrics(){
  if(!DATA||!USER) return;
  const isPrem=USER.plan!=='free';
  document.getElementById('m-bank').textContent=fmtUSD(DATA.bankroll);
  document.getElementById('m-bank-sub').textContent=USER.moneda||'USD';
  document.getElementById('m-gold').textContent=isPrem?(DATA.gold_tips||[]).length:'🔒';
  document.getElementById('m-roi').textContent=isPrem?fmtPct(DATA.roi_gold_potencial):'🔒';
  document.getElementById('m-sure').textContent=(DATA.sure_bets||[]).length;
  document.getElementById('m-roi-sure').textContent=isPrem?fmtPct(DATA.roi_sure_garantizado):'🔒';
  document.getElementById('m-eventos').textContent=DATA.total_eventos||0;
  document.getElementById('m-ventana').textContent='próximas '+(DATA.ventana_horas||36)+'hs';
}

async function marcar(btn,pick){btn.disabled=true;btn.textContent='Guardando...';try{if(pick)await aFetch('/api/picks/colocar',{method:'POST',body:JSON.stringify(pick)});}catch(e){}btn.textContent='✓ Colocado';btn.style.color='var(--teal)';btn.style.borderColor='var(--teal)';}

async function loadUser(){
  const r=await aFetch('/api/me');
  if(!r.ok){window.location.href='/login';return;}
  USER=await r.json();
  const pl={'free':'Gratuito','premium':'Premium','admin':'Admin'};
  const pc={'free':'b-gray','premium':'b-violet','admin':'b-blue'};
  document.getElementById('plan-badge').textContent=pl[USER.plan]||USER.plan;
  document.getElementById('plan-badge').className='badge '+(pc[USER.plan]||'b-gray');
  document.getElementById('freemium-banner').style.display=USER.plan==='free'?'flex':'none';
  if(USER.plan==='free') document.getElementById('btn-scan').style.display='none';
  document.getElementById('p-bankroll').value=USER.bankroll||1000;
  document.getElementById('p-riesgo').value=USER.perfil_riesgo||'inteligente';
  document.getElementById('p-tgid').value=USER.tg_chat_id||'';
  document.getElementById('p-tgactivo').checked=USER.tg_activo||false;
}

function showPerfil(){document.getElementById('modal-perfil').style.display='flex';}
function hidePerfil(){document.getElementById('modal-perfil').style.display='none';}

async function guardarPerfil(){
  const data={bankroll:parseFloat(document.getElementById('p-bankroll').value)||1000,
    perfil_riesgo:document.getElementById('p-riesgo').value,
    tg_chat_id:document.getElementById('p-tgid').value.trim(),
    tg_activo:document.getElementById('p-tgactivo').checked};
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
    if(d.scanning&&!d.picks_validos){
      document.getElementById('sure-lista').innerHTML='<div class="empty"><span class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</span>Analizando mercados...</div>';
      document.getElementById('value-lista').innerHTML='<div class="empty"><span class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</span>Calculando value bets...</div>';
      setTimeout(fetchData,5000);return;
    }
    DATA=d;
    document.getElementById('scan-badge').className='badge b-teal';
    document.getElementById('scan-badge').textContent='● Live';
    document.getElementById('last-scan').textContent=d.ultimo_scan?'Último: '+d.ultimo_scan:'';
    updateMetrics();renderSure();renderValue();showTab(currentTab,null);
  }catch(e){setTimeout(fetchData,8000);}
}

async function triggerScan(){
  const btn=document.getElementById('btn-scan');
  btn.disabled=true;btn.textContent='↻ Escaneando...';
  await aFetch('/api/scan',{method:'POST'});
  setTimeout(()=>{btn.disabled=false;btn.textContent='↻ Escanear';fetchData();},3000);
}

async function doLogout(){
  await aFetch('/api/auth/logout',{method:'POST'});
  localStorage.removeItem('sb_token');
  window.location.href='/login';
}

loadUser().then(fetchData);
setInterval(fetchData,300000);
</script>
</body>
</html>"""

# ── Endpoints historial y estadísticas ─────────────────────────────────────────

@app.post("/api/picks/colocar")
async def colocar_pick(request: Request):
    user = await require_auth(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    from core.database import guardar_pick
    return JSONResponse(await guardar_pick(user["id"], data))

@app.post("/api/picks/{pick_id}/resultado")
async def actualizar_resultado(pick_id: int, request: Request):
    user = await require_auth(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    from core.database import actualizar_resultado
    return JSONResponse(await actualizar_resultado(pick_id, user["id"], data.get("estado","")))

@app.get("/api/estadisticas")
async def get_estadisticas(request: Request):
    user = await require_auth(request)
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    from core.database import get_estadisticas
    return JSONResponse(await get_estadisticas(user["id"]))

@app.get("/estadisticas", response_class=HTMLResponse)
async def estadisticas_page(request: Request):
    user = await require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return HTMLResponse(STATS_HTML)

STATS_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Estadísticas — InvestiaBet</title>
<style>
:root{
  --bg:#080c14;--bg2:#0d1220;--bg3:#131929;--bg4:#171f2e;
  --border:#1e2a3d;--border2:#243347;
  --text:#e2e8f4;--text2:#7a8aaa;--text3:#3d4f6a;
  --blue:#4f8ef7;--blue-bg:rgba(79,142,247,.1);--blue-border:rgba(79,142,247,.25);
  --violet:#7c5ff7;--violet-bg:rgba(124,95,247,.1);--violet-border:rgba(124,95,247,.25);
  --teal:#00d4aa;--teal-bg:rgba(0,212,170,.08);--teal-border:rgba(0,212,170,.2);
  --green:#22c55e;--green-bg:rgba(34,197,94,.1);
  --red:#ef4444;--red-bg:rgba(239,68,68,.1);
  --amber:#f59e0b;--amber-bg:rgba(245,158,11,.1);
  --radius:12px;--radius-sm:8px
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:rgba(13,18,32,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:13px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:17px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--violet),var(--teal));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.btn{padding:7px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer}
.btn:hover{background:var(--bg4)}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
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
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:16px}
.card-title{font-size:14px;font-weight:600;margin-bottom:14px;color:var(--text2);display:flex;align-items:center;gap:8px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:700px){.two-col{grid-template-columns:1fr}}
.tipo-row{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--bg3);border-radius:var(--radius-sm);margin-bottom:8px}
.tipo-label{font-size:13px;font-weight:500}
.tipo-stats{display:flex;gap:16px;font-size:12px;color:var(--text2)}
.tipo-stats strong{color:var(--text)}
/* Historial table */
.hist-table{width:100%;border-collapse:collapse;font-size:13px}
.hist-table th{text-align:left;padding:8px 10px;color:var(--text2);font-size:11px;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.3px}
.hist-table td{padding:10px;border-bottom:1px solid var(--border);vertical-align:middle}
.hist-table tr:last-child td{border-bottom:none}
.hist-table tr:hover td{background:var(--bg3)}
.badge{display:inline-flex;align-items:center;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:500}
.b-teal{background:var(--teal-bg);color:var(--teal);border:1px solid var(--teal-border)}
.b-violet{background:var(--violet-bg);color:var(--violet);border:1px solid var(--violet-border)}
.b-blue{background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}
.b-green{background:var(--green-bg);color:var(--green)}
.b-red{background:var(--red-bg);color:var(--red)}
.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-gray{background:var(--bg3);color:var(--text2)}
.resultado-btn{padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:11px;cursor:pointer;margin-right:4px}
.resultado-btn:hover{background:var(--bg3)}
.resultado-btn.ganado{border-color:var(--teal);color:var(--teal)}
.resultado-btn.perdido{border-color:var(--red);color:var(--red)}
.empty{text-align:center;padding:50px 20px;color:var(--text2)}
.empty-icon{font-size:36px;display:block;margin-bottom:12px;opacity:.3}
/* ROI bar */
.roi-bar-wrap{background:var(--border);border-radius:4px;height:6px;margin-top:8px;overflow:hidden}
.roi-bar{height:6px;border-radius:4px;transition:width .5s}
.spin{animation:spin 1s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">📈 InvestiaBet — Mis Estadísticas</div>
  <div style="display:flex;gap:8px">
    <button class="btn" onclick="window.location.href='/'">← Dashboard</button>
    <button class="btn" onclick="doLogout()">Salir</button>
  </div>
</div>

<div class="container">
  <div class="tabs">
    <div class="tab active" onclick="showPeriodo('mes',this)">Últimos 30 días</div>
    <div class="tab" onclick="showPeriodo('todo',this)">Todo el historial</div>
    <div class="tab" onclick="showPeriodo('historial',this)">Detalle de picks</div>
  </div>

  <div id="panel-mes"></div>
  <div id="panel-todo" style="display:none"></div>
  <div id="panel-historial" style="display:none"></div>
</div>

<script>
let DATA=null, currentPeriodo='mes';
function getToken(){return localStorage.getItem('sb_token')||'';}
function authH(){const t=getToken();return t?{'Authorization':'Bearer '+t,'Content-Type':'application/json'}:{'Content-Type':'application/json'};}
async function aFetch(url,opts={}){opts.credentials='include';opts.headers={...authH(),...(opts.headers||{})};return fetch(url,opts);}
function fmt(n,d=2){return n!=null?Number(n).toFixed(d):'—';}
function fmtUSD(n){return n!=null?(n>=0?'+':'')+fmt(n,2):'—';}
function fmtPct(n){return n!=null?(n>=0?'+':'')+fmt(n,1)+'%':'—';}
function fmtDate(d){if(!d)return '—';return new Date(d).toLocaleDateString('es-AR',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'});}

function estadoBadge(e){
  const m={ganado:'b-teal',perdido:'b-red',colocado:'b-amber',void:'b-gray'};
  const l={ganado:'✓ Ganó',perdido:'✗ Perdió',colocado:'⏳ Colocado',void:'— Void'};
  return `<span class="badge ${m[e]||'b-gray'}">${l[e]||e}</span>`;
}

function tipoBadge(tipo,gold){
  if(gold) return '<span class="badge b-violet">⭐ Gold</span>';
  if(tipo==='sure') return '<span class="badge b-teal">🔒 Sure</span>';
  return '<span class="badge b-blue">📊 Value</span>';
}

function renderStats(stats){
  if(!stats) return '<div class="empty"><span class="empty-icon">📊</span>Sin datos aún.</div>';
  const roiColor = stats.roi>=0?'var(--teal)':'var(--red)';
  const pnlColor = stats.pnl_total>=0?'var(--teal)':'var(--red)';

  // Barra de win rate
  const wr = stats.win_rate||0;
  const wrColor = wr>=60?'var(--teal)':wr>=50?'var(--blue)':wr>=40?'var(--amber)':'var(--red)';

  return `
  <div class="metrics">
    <div class="metric m-teal">
      <div class="metric-label">Win Rate</div>
      <div class="metric-val" style="color:${wrColor}">${fmt(stats.win_rate,1)}%</div>
      <div class="roi-bar-wrap"><div class="roi-bar" style="width:${Math.min(wr,100)}%;background:${wrColor}"></div></div>
      <div class="metric-sub">${stats.ganados||0} ganados / ${stats.total_resueltos||0} resueltos</div>
    </div>
    <div class="metric m-${stats.roi>=0?'green':'red'}">
      <div class="metric-label">ROI real</div>
      <div class="metric-val" style="color:${roiColor}">${fmtPct(stats.roi)}</div>
      <div class="metric-sub">sobre inversión total</div>
    </div>
    <div class="metric m-${stats.pnl_total>=0?'teal':'red'}">
      <div class="metric-label">P&L total</div>
      <div class="metric-val" style="color:${pnlColor}">${fmtUSD(stats.pnl_total)}</div>
      <div class="metric-sub">USD ganado/perdido</div>
    </div>
    <div class="metric m-blue">
      <div class="metric-label">Total colocados</div>
      <div class="metric-val" style="color:var(--blue)">${stats.total_colocados||0}</div>
      <div class="metric-sub">${stats.pendientes||0} pendientes de resultado</div>
    </div>
    <div class="metric m-violet">
      <div class="metric-label">Invertido</div>
      <div class="metric-val" style="color:var(--violet)">$${fmt(stats.invertido_total,0)}</div>
      <div class="metric-sub">USD apostado en total</div>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">📊 Por tipo de pick</div>
      ${renderTipoRow('Value Bets', stats.value_stats, 'var(--blue)')}
      ${renderTipoRow('Sure Picks', stats.sure_stats, 'var(--teal)')}
      ${renderTipoRow('Gold Tips', stats.gold_stats, 'var(--violet)')}
    </div>
    <div class="card">
      <div class="card-title">🏆 Por deporte</div>
      ${Object.entries(stats.por_deporte||{}).map(([dep,s])=>renderTipoRow(dep,s,'var(--text2)')).join('')||'<div style="color:var(--text2);font-size:13px">Sin datos por deporte.</div>'}
    </div>
  </div>`;
}

function renderTipoRow(label, s, color){
  if(!s||s.total===0) return `<div class="tipo-row" style="opacity:.4"><div class="tipo-label" style="color:${color}">${label}</div><div class="tipo-stats"><span>Sin datos</span></div></div>`;
  const roiColor = s.roi>=0?'var(--teal)':'var(--red)';
  return `<div class="tipo-row">
    <div class="tipo-label" style="color:${color}">${label}</div>
    <div class="tipo-stats">
      <span>${s.total} picks</span>
      <span>Win: <strong>${fmt(s.win_rate,1)}%</strong></span>
      <span>ROI: <strong style="color:${roiColor}">${fmtPct(s.roi)}</strong></span>
      <span>P&L: <strong style="color:${s.pnl>=0?'var(--teal)':'var(--red)'}">${fmtUSD(s.pnl)}</strong></span>
    </div>
  </div>`;
}

function renderHistorial(picks){
  if(!picks||!picks.length) return `<div class="empty"><span class="empty-icon">📋</span>No hay picks registrados aún.<br><span style="font-size:12px">Cuando hagas click en "Colocado en Stake" en el dashboard, aparecerán acá.</span></div>`;
  return `<div class="card" style="padding:0;overflow:hidden">
    <div style="overflow-x:auto">
    <table class="hist-table">
      <thead><tr>
        <th>Fecha</th><th>Evento</th><th>Pick</th><th>Tipo</th>
        <th>Cuota</th><th>Stake</th><th>Estado</th><th>P&L</th><th>Resultado</th>
      </tr></thead>
      <tbody>
        ${picks.map(p=>`<tr>
          <td style="color:var(--text2);font-size:11px;white-space:nowrap">${fmtDate(p.fecha_colocado)}</td>
          <td>
            <div style="font-weight:500;font-size:13px">${p.evento||'—'}</div>
            <div style="font-size:10px;color:var(--text2)">${p.liga||''} ${p.mercado?'· '+p.mercado:''}</div>
          </td>
          <td style="font-weight:500">${p.equipo_pick||'—'}</td>
          <td>${tipoBadge(p.tipo,p.es_gold)}</td>
          <td style="color:var(--blue);font-weight:600">@${fmt(p.odds,2)}</td>
          <td style="color:var(--violet)">$${fmt(p.stake_usd,2)}</td>
          <td>${estadoBadge(p.estado)}</td>
          <td style="font-weight:700;color:${(p.pnl||0)>=0?'var(--teal)':'var(--red)'}">
            ${p.pnl!=null?fmtUSD(p.pnl):'—'}
          </td>
          <td>
            ${p.estado==='colocado'?`
              <button class="resultado-btn ganado" onclick="marcarResultado(${p.id},'ganado')">✓ Ganó</button>
              <button class="resultado-btn perdido" onclick="marcarResultado(${p.id},'perdido')">✗ Perdió</button>
              <button class="resultado-btn" onclick="marcarResultado(${p.id},'void')" style="font-size:10px">Void</button>
            `:'—'}
          </td>
        </tr>`).join('')}
      </tbody>
    </table>
    </div>
  </div>`;
}

function showPeriodo(p, el){
  currentPeriodo=p;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  if(el) el.classList.add('active');
  ['mes','todo','historial'].forEach(id=>{
    document.getElementById('panel-'+id).style.display = id===p?'block':'none';
  });
  if(DATA) renderAll();
}

function renderAll(){
  if(!DATA) return;
  document.getElementById('panel-mes').innerHTML     = renderStats(DATA.mes);
  document.getElementById('panel-todo').innerHTML    = renderStats(DATA.todo);
  document.getElementById('panel-historial').innerHTML = renderHistorial(DATA.historial);
}

async function marcarResultado(pickId, estado){
  const r = await aFetch(`/api/picks/${pickId}/resultado`,{
    method:'POST', body:JSON.stringify({estado})
  });
  const d = await r.json();
  if(d.ok){ await cargarDatos(); }
  else{ alert('Error: '+d.error); }
}

async function cargarDatos(){
  const r = await aFetch('/api/estadisticas');
  if(r.status===401){window.location.href='/login';return;}
  DATA = await r.json();
  renderAll();
}

async function doLogout(){
  await aFetch('/api/auth/logout',{method:'POST'});
  localStorage.removeItem('sb_token');
  window.location.href='/login';
}

cargarDatos();
</script>
</body>
</html>"""
