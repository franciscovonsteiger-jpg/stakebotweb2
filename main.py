import os, json, asyncio, logging
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stakebot")

# ── Cache en memoria ──────────────────────────────────────────────────────────
cache = {
    "ultimo_scan": None,
    "resultado":   None,
    "scanning":    False,
    "error":       None,
}

async def run_scan_bg():
    if cache["scanning"]:
        return
    cache["scanning"] = True
    cache["error"]    = None
    try:
        from core.engine import escanear_mercado
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, escanear_mercado)
        cache["resultado"]   = resultado
        cache["ultimo_scan"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        log.info(f"Scan OK — {len(resultado['picks_validos'])} picks válidos")
    except Exception as e:
        cache["error"] = str(e)
        log.error(f"Error en scan: {e}")
    finally:
        cache["scanning"] = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(run_scan_bg())
    yield

app = FastAPI(title="Stake Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/picks")
async def get_picks():
    if cache["error"]:
        return JSONResponse({"error": cache["error"]}, status_code=500)
    if not cache["resultado"]:
        return JSONResponse({"scanning": True, "mensaje": "Primer scan en progreso..."})
    return JSONResponse({**cache["resultado"], "ultimo_scan": cache["ultimo_scan"], "scanning": cache["scanning"]})

@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if cache["scanning"]:
        return JSONResponse({"mensaje": "Ya hay un scan en progreso"})
    background_tasks.add_task(run_scan_bg)
    return JSONResponse({"mensaje": "Scan iniciado"})

@app.get("/api/status")
async def status():
    r = cache["resultado"]
    return JSONResponse({
        "ok":           True,
        "ultimo_scan":  cache["ultimo_scan"],
        "scanning":     cache["scanning"],
        "picks_validos":    len(r["picks_validos"]) if r else 0,
        "picks_descartados": len(r["picks_descartados"]) if r else 0,
        "bankroll":     r["bankroll"] if r else 0,
    })

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stake Bot — Señales</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--border:#2e3348;--text:#e8eaf0;--text2:#8b92a8;--text3:#555e7a;--green:#22c55e;--green-bg:rgba(34,197,94,.12);--red:#ef4444;--red-bg:rgba(239,68,68,.12);--amber:#f59e0b;--amber-bg:rgba(245,158,11,.12);--purple:#8b5cf6;--purple-bg:rgba(139,92,246,.12);--blue:#3b82f6;--blue-bg:rgba(59,130,246,.12);--radius:10px;--radius-sm:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:16px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px}
.logo-dot{width:8px;height:8px;border-radius:50%;background:var(--green)}
.topbar-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:500}
.badge-green{background:var(--green-bg);color:var(--green)}
.badge-amber{background:var(--amber-bg);color:var(--amber)}
.badge-red{background:var(--red-bg);color:var(--red)}
.badge-purple{background:var(--purple-bg);color:var(--purple)}
.badge-gray{background:var(--bg3);color:var(--text2)}
.badge-blue{background:var(--blue-bg);color:var(--blue)}
.btn{padding:7px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer;transition:all .15s}
.btn:hover{background:var(--border)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.metric-label{font-size:11px;color:var(--text2);margin-bottom:6px}
.metric-val{font-size:22px;font-weight:600;color:var(--text)}
.metric-sub{font-size:10px;color:var(--text3);margin-top:3px}
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:16px;overflow-x:auto}
.tab{padding:10px 18px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--purple);font-weight:500}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.pill{padding:5px 14px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer;transition:all .15s}
.pill.active{background:var(--bg3);color:var(--text);border-color:var(--border)}
.pill:hover{color:var(--text)}
.pick{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:10px;transition:border-color .15s}
.pick:hover{border-color:#3e4560}
.pick.descartado{opacity:.5;border-style:dashed}
.pick-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.pick-info{flex:1;min-width:180px}
.pick-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.pick-evento{font-size:15px;font-weight:600;margin-bottom:3px}
.pick-sub{font-size:12px;color:var(--text2)}
.pick-edge{text-align:right}
.edge-val{font-size:24px;font-weight:700}
.edge-label{font-size:10px;color:var(--text2);margin-top:2px}
.bar-wrap{height:3px;background:var(--bg3);border-radius:2px;margin:10px 0 4px}
.bar-fill{height:3px;border-radius:2px;transition:width .5s}
.bar-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:12px}
.pick-bottom{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;padding-top:12px;border-top:1px solid var(--border)}
.pick-nums{display:flex;gap:20px;flex-wrap:wrap}
.pick-num-label{font-size:10px;color:var(--text2);margin-bottom:3px}
.pick-num-val{font-size:16px;font-weight:600}
.pick-actions{display:flex;gap:6px}
.ctx-tag{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:500}
.ctx-champion{background:var(--amber-bg);color:var(--amber)}
.ctx-clean{background:var(--green-bg);color:var(--green)}
.ctx-esport{background:var(--purple-bg);color:var(--purple)}
.ctx-other{background:var(--red-bg);color:var(--red)}
.empty{text-align:center;padding:48px 20px;color:var(--text2)}
.empty-icon{font-size:36px;margin-bottom:12px;opacity:.4}
.scanning-wrap{text-align:center;padding:60px 20px;color:var(--text2)}
.spin{display:inline-block;animation:spin 1s linear infinite;font-size:28px;margin-bottom:12px}
@keyframes spin{to{transform:rotate(360deg)}}
.alert{border-radius:var(--radius-sm);padding:10px 14px;font-size:12px;margin-bottom:14px;display:flex;gap:8px;align-items:flex-start}
.alert-warn{background:var(--amber-bg);border:1px solid rgba(245,158,11,.3);color:var(--amber)}
.alert-err{background:var(--red-bg);border:1px solid rgba(239,68,68,.3);color:var(--red)}
.select{padding:5px 10px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px}
@media(max-width:600px){.pick-edge{display:none}.pick-nums{gap:12px}}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">
    <div class="logo-dot" id="conn-dot"></div>
    Stake Bot
    <span class="badge badge-purple" style="font-size:10px">v2</span>
  </div>
  <div class="topbar-right">
    <span id="scan-badge" class="badge badge-amber">Iniciando...</span>
    <span id="last-scan-txt" style="font-size:11px;color:var(--text2)"></span>
    <button class="btn" id="btn-scan" onclick="triggerScan()">↻ Escanear</button>
  </div>
</div>

<div class="container">

  <div class="metrics" id="metrics">
    <div class="metric"><div class="metric-label">Bankroll</div><div class="metric-val" id="m-bank">—</div><div class="metric-sub">USD</div></div>
    <div class="metric"><div class="metric-label">Picks válidos</div><div class="metric-val" id="m-validos" style="color:var(--green)">—</div><div class="metric-sub">edge ≥ 5%</div></div>
    <div class="metric"><div class="metric-label">Descartados</div><div class="metric-val" id="m-descartados" style="color:var(--red)">—</div><div class="metric-sub">por contexto</div></div>
    <div class="metric"><div class="metric-label">Exposición total</div><div class="metric-val" id="m-expo">—</div><div class="metric-sub">si colocás todos</div></div>
    <div class="metric"><div class="metric-label">Eventos analizados</div><div class="metric-val" id="m-eventos">—</div><div class="metric-sub">este scan</div></div>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="showTab('validos',this)">Picks del día</div>
    <div class="tab" onclick="showTab('descartados',this)">Descartados por contexto</div>
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
        <select class="select" id="min-edge" onchange="renderPicks()">
          <option value="3">3%</option>
          <option value="5" selected>5%</option>
          <option value="7">7%</option>
          <option value="10">10%</option>
        </select>
      </div>
    </div>
    <div id="lista-validos"></div>
  </div>

  <div id="tab-descartados" style="display:none">
    <div class="alert alert-warn">
      ⚠ Estos picks tienen edge matemático positivo pero el algoritmo v2 los descartó por contexto motivacional. No apostar.
    </div>
    <div id="lista-descartados"></div>
  </div>

</div>

<script>
let DATA = null;
let currentFilter = 'todos';
let currentTab = 'validos';

function fmt(n, d=2){ return n != null ? Number(n).toFixed(d) : '—'; }
function fmtUSD(n){ return '$' + fmt(n, 2); }

function ctxBadge(id, desc){
  const map = {
    champion_early: ['ctx-champion','⚠ Campeón anticipado'],
    relegated:      ['ctx-other',   '⬇ Descendido'],
    nothing_to_play:['ctx-other',   '○ Sin objetivos'],
    esport:         ['ctx-esport',  '🎮 Esport'],
    clean:          ['ctx-clean',   '✓ Sin alertas'],
  };
  const [cls, label] = map[id] || ['ctx-other', desc];
  return `<span class="ctx-tag ${cls}">${label}</span>`;
}

function edgeColor(edge){
  if(edge >= 0.10) return '#8b5cf6';
  if(edge >= 0.06) return '#22c55e';
  return '#8b92a8';
}

function renderPickCard(p, descartado=false){
  const edgePct  = (p.edge * 100).toFixed(1);
  const barW     = Math.min(Math.abs(p.edge) / 0.15 * 100, 100).toFixed(1);
  const barColor = edgeColor(p.edge);
  const implPct  = (1/p.odds_stake*100).toFixed(1);
  const adjPct   = (p.prob_ajustada*100).toFixed(1);
  const deporteEmoji = {Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';

  return `<div class="pick${descartado?' descartado':''}">
    <div class="pick-top">
      <div class="pick-info">
        <div class="pick-meta">
          <span class="badge badge-gray">${deporteEmoji} ${p.deporte}</span>
          <span style="font-size:10px;color:var(--text3)">${p.liga}</span>
          ${p.hora_local ? `<span style="font-size:10px;color:var(--text3)">🕐 ${p.hora_local}</span>` : ''}
          ${ctxBadge(p.contexto_id, p.contexto_desc)}
        </div>
        <div class="pick-evento">${p.evento}</div>
        <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · Cuota Stake: <strong>@${fmt(p.odds_stake,2)}</strong></div>
        ${p.razon_descarte ? `<div style="font-size:11px;color:var(--amber);margin-top:5px">⚠ ${p.razon_descarte}</div>` : ''}
      </div>
      <div class="pick-edge">
        <div class="edge-val" style="color:${barColor}">${p.edge>=0?'+':''}${edgePct}%</div>
        <div class="edge-label">edge real</div>
      </div>
    </div>

    <div class="bar-wrap"><div class="bar-fill" style="width:${barW}%;background:${barColor}"></div></div>
    <div class="bar-labels">
      <span>Prob. implícita cuota: ${implPct}%</span>
      <span>Prob. ajustada modelo: ${adjPct}%</span>
    </div>

    ${!descartado ? `
    <div class="pick-bottom">
      <div class="pick-nums">
        <div>
          <div class="pick-num-label">Stake sugerido (1/2 Kelly)</div>
          <div class="pick-num-val">${fmtUSD(p.stake_usd)} <span style="font-size:11px;color:var(--text2)">USD</span></div>
        </div>
        <div>
          <div class="pick-num-label">Ganancia potencial</div>
          <div class="pick-num-val" style="color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div>
        </div>
      </div>
      <div class="pick-actions">
        <button class="btn" onclick="marcarColocado('${p.id}', this)">✓ Lo coloqué en Stake</button>
      </div>
    </div>` : `
    <div style="padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--red)">
      ✗ Descartado — ${p.razon_descarte || 'Contexto motivacional negativo'}
    </div>`}
  </div>`;
}

function renderPicks(){
  if(!DATA) return;
  const minEdge = parseFloat(document.getElementById('min-edge').value) / 100;
  let picks = DATA.picks_validos.filter(p => p.edge >= minEdge);
  if(currentFilter !== 'todos') picks = picks.filter(p => p.deporte === currentFilter);
  const el = document.getElementById('lista-validos');
  if(picks.length === 0){
    el.innerHTML = `<div class="empty"><div class="empty-icon">🔍</div>Sin picks válidos con edge ≥ ${(minEdge*100).toFixed(0)}% en este filtro.<br><span style="font-size:12px">Bajá el edge mínimo o esperá el próximo scan.</span></div>`;
    return;
  }
  el.innerHTML = picks.map(p => renderPickCard(p, false)).join('');
}

function renderDescartados(){
  if(!DATA) return;
  const el = document.getElementById('lista-descartados');
  const picks = DATA.picks_descartados;
  if(picks.length === 0){
    el.innerHTML = `<div class="empty"><div class="empty-icon">✓</div>Ningún pick fue descartado en este scan.</div>`;
    return;
  }
  el.innerHTML = picks.map(p => renderPickCard(p, true)).join('');
}

function updateMetrics(){
  if(!DATA) return;
  const validos = DATA.picks_validos;
  const expo = validos.reduce((a,p) => a + (p.stake_usd||0), 0);
  document.getElementById('m-bank').textContent     = fmtUSD(DATA.bankroll);
  document.getElementById('m-validos').textContent  = validos.length;
  document.getElementById('m-descartados').textContent = DATA.picks_descartados.length;
  document.getElementById('m-expo').textContent     = fmtUSD(expo);
  document.getElementById('m-eventos').textContent  = DATA.total_eventos;
}

function filtrar(f, btn){
  currentFilter = f;
  document.querySelectorAll('.pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderPicks();
}

function showTab(name, el){
  ['validos','descartados'].forEach(t => {
    document.getElementById('tab-'+t).style.display = t===name ? 'block' : 'none';
  });
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  currentTab = name;
  if(name === 'descartados') renderDescartados();
}

function marcarColocado(id, btn){
  btn.textContent = '✓ Colocado';
  btn.style.color = '#22c55e';
  btn.style.borderColor = '#22c55e';
  btn.disabled = true;
}

async function fetchData(){
  try{
    const r = await fetch('/api/picks');
    const d = await r.json();
    if(d.scanning && !d.picks_validos){
      document.getElementById('scan-badge').className = 'badge badge-amber';
      document.getElementById('scan-badge').textContent = 'Escaneando...';
      document.getElementById('lista-validos').innerHTML = `<div class="scanning-wrap"><div class="spin">↻</div><div>Analizando ${Object.keys({}).length}+ mercados...</div><div style="font-size:12px;margin-top:6px">Primer scan puede tardar 1-2 minutos</div></div>`;
      setTimeout(fetchData, 5000);
      return;
    }
    if(d.error){
      document.getElementById('lista-validos').innerHTML = `<div class="alert alert-err">⚠ ${d.error}<br><span style="font-size:11px">Verificá tu API key en Railway → Variables</span></div>`;
      return;
    }
    DATA = d;
    document.getElementById('scan-badge').className = 'badge badge-green';
    document.getElementById('scan-badge').textContent = '● Live';
    document.getElementById('conn-dot').style.background = '#22c55e';
    document.getElementById('last-scan-txt').textContent = d.ultimo_scan ? 'Último scan: ' + d.ultimo_scan : '';
    updateMetrics();
    renderPicks();
    if(currentTab === 'descartados') renderDescartados();
  } catch(e){
    console.error(e);
    setTimeout(fetchData, 8000);
  }
}

async function triggerScan(){
  const btn = document.getElementById('btn-scan');
  btn.disabled = true;
  btn.textContent = 'Escaneando...';
  document.getElementById('scan-badge').className = 'badge badge-amber';
  document.getElementById('scan-badge').textContent = 'Escaneando...';
  await fetch('/api/scan', {method:'POST'});
  setTimeout(()=>{
    btn.disabled = false;
    btn.textContent = '↻ Escanear';
    fetchData();
  }, 3000);
}

// Auto-refresh cada 5 minutos
fetchData();
setInterval(fetchData, 300000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)
