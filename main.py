import os, asyncio, logging
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stakebot")

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
        gold_count = len(resultado.get("gold_tips", []))
        log.info(f"Scan OK — {len(resultado['picks_validos'])} picks · {gold_count} Gold Tips")
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
        return JSONResponse({"mensaje": "Scan en progreso"})
    background_tasks.add_task(run_scan_bg)
    return JSONResponse({"mensaje": "Scan iniciado"})

@app.get("/api/status")
async def status():
    r = cache["resultado"]
    return JSONResponse({
        "ok": True,
        "ultimo_scan":  cache["ultimo_scan"],
        "scanning":     cache["scanning"],
        "picks_validos": len(r["picks_validos"]) if r else 0,
        "gold_tips":     len(r.get("gold_tips",[])) if r else 0,
        "roi_gold_pct":  r.get("roi_gold_potencial", 0) if r else 0,
    })

DASHBOARD = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stake Bot</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--border:#2e3348;--text:#e8eaf0;--text2:#8b92a8;--text3:#555e7a;--green:#22c55e;--green-bg:rgba(34,197,94,.12);--red:#ef4444;--red-bg:rgba(239,68,68,.12);--amber:#f59e0b;--amber-bg:rgba(245,158,11,.12);--purple:#8b5cf6;--purple-bg:rgba(139,92,246,.12);--blue:#3b82f6;--gold:#f59e0b;--gold-bg:rgba(245,158,11,.15);--radius:10px;--radius-sm:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
.logo-dot{width:8px;height:8px;border-radius:50%;background:var(--green)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:500}
.b-green{background:var(--green-bg);color:var(--green)}
.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-purple{background:var(--purple-bg);color:var(--purple)}
.b-gray{background:var(--bg3);color:var(--text2)}
.b-gold{background:var(--gold-bg);color:var(--gold);border:1px solid rgba(245,158,11,.3)}
.btn{padding:7px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer}
.btn:hover{background:var(--border)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.metric-label{font-size:11px;color:var(--text2);margin-bottom:5px}
.metric-val{font-size:21px;font-weight:600}
.metric-sub{font-size:10px;color:var(--text3);margin-top:2px}

/* Gold Tips section */
.gold-section{background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.25);border-radius:var(--radius);padding:16px 18px;margin-bottom:16px}
.gold-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.gold-title{font-size:15px;font-weight:600;color:var(--gold);display:flex;align-items:center;gap:8px}
.gold-roi{font-size:13px;color:var(--text2);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.gold-roi span{color:var(--text)}
.gold-pick{background:var(--bg2);border:1px solid rgba(245,158,11,.2);border-radius:var(--radius);padding:14px 16px;margin-bottom:8px}
.gold-pick:last-child{margin-bottom:0}
.gold-rank{width:28px;height:28px;border-radius:50%;background:var(--gold-bg);border:1px solid rgba(245,158,11,.4);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:var(--gold);flex-shrink:0}

/* Regular picks */
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:14px;overflow-x:auto}
.tab{padding:9px 16px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab.active{color:var(--text);border-bottom-color:var(--purple);font-weight:500}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.pill{padding:4px 12px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text2);font-size:12px;cursor:pointer}
.pill.active{background:var(--bg3);color:var(--text)}
.pick{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin-bottom:8px}
.pick:hover{border-color:#3e4560}
.pick.descartado{opacity:.45;border-style:dashed}
.pick-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.pick-info{flex:1;min-width:160px}
.pick-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:5px}
.pick-evento{font-size:14px;font-weight:600;margin-bottom:2px}
.pick-sub{font-size:12px;color:var(--text2)}
.edge-val{font-size:22px;font-weight:700;text-align:right}
.edge-lbl{font-size:10px;color:var(--text2);text-align:right;margin-top:1px}
.bar-wrap{height:3px;background:var(--bg3);border-radius:2px;margin:8px 0 3px}
.bar-fill{height:3px;border-radius:2px}
.bar-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:10px}
.pick-bottom{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;padding-top:10px;border-top:1px solid var(--border)}
.pick-nums{display:flex;gap:16px;flex-wrap:wrap}
.num-label{font-size:10px;color:var(--text2);margin-bottom:2px}
.num-val{font-size:15px;font-weight:600}
.ctx-tag{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:500}
.ctx-clean{background:var(--green-bg);color:var(--green)}
.ctx-warn{background:var(--amber-bg);color:var(--amber)}
.ctx-bad{background:var(--red-bg);color:var(--red)}
.ctx-esport{background:var(--purple-bg);color:var(--purple)}
.empty{text-align:center;padding:40px 20px;color:var(--text2)}
.select{padding:4px 8px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo"><div class="logo-dot" id="conn-dot"></div>Stake Bot <span class="badge b-purple" style="font-size:10px">v2</span></div>
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span id="scan-badge" class="badge b-amber">Iniciando...</span>
    <span id="last-scan" style="font-size:11px;color:var(--text2)"></span>
    <button class="btn" id="btn-scan" onclick="triggerScan()">↻ Escanear</button>
  </div>
</div>

<div class="container">

  <div class="metrics" id="metrics">
    <div class="metric"><div class="metric-label">Bankroll</div><div class="metric-val" id="m-bank">—</div><div class="metric-sub">USD</div></div>
    <div class="metric"><div class="metric-label">Gold Tips hoy</div><div class="metric-val" id="m-gold" style="color:var(--gold)">—</div><div class="metric-sub">picks premium</div></div>
    <div class="metric"><div class="metric-label">ROI potencial gold</div><div class="metric-val" id="m-roi" style="color:var(--green)">—</div><div class="metric-sub">si todos son verdes</div></div>
    <div class="metric"><div class="metric-label">Exposición gold</div><div class="metric-val" id="m-expo">—</div><div class="metric-sub">USD en juego</div></div>
    <div class="metric"><div class="metric-label">Total picks válidos</div><div class="metric-val" id="m-validos">—</div><div class="metric-sub">edge ≥ 3%</div></div>
    <div class="metric"><div class="metric-label">Eventos analizados</div><div class="metric-val" id="m-eventos">—</div><div class="metric-sub">este scan</div></div>
  </div>

  <!-- Gold Tips -->
  <div class="gold-section" id="gold-section" style="display:none">
    <div class="gold-header">
      <div class="gold-title">⭐ Gold Tips del día</div>
      <div class="gold-roi">
        <span>ROI potencial: <span id="gold-roi-val" style="color:var(--green);font-weight:600">—</span></span>
        <span>Exposición: <span id="gold-expo-val">—</span></span>
      </div>
    </div>
    <div id="gold-lista"></div>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="showTab('validos',this)">Todos los picks</div>
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
        <select class="select" id="min-edge" onchange="renderPicks()">
          <option value="3" selected>3%</option>
          <option value="5">5%</option>
          <option value="7">7%</option>
        </select>
      </div>
    </div>
    <div id="lista-validos"></div>
  </div>

  <div id="tab-descartados" style="display:none">
    <div id="lista-descartados"></div>
  </div>

</div>

<script>
let DATA = null;
let currentFilter = 'todos';
let currentTab = 'validos';

function fmt(n, d=2){ return n!=null ? Number(n).toFixed(d) : '—'; }
function fmtUSD(n){ return '$'+fmt(n,2); }
function fmtPct(n){ return (n>=0?'+':'')+fmt(n,1)+'%'; }

function edgeColor(e){
  if(e>=0.10) return '#8b5cf6';
  if(e>=0.06) return '#22c55e';
  return '#8b92a8';
}

function ctxTag(id){
  const m={
    champion_early:['ctx-warn','⚠ Campeón'],
    relegated:['ctx-bad','⬇ Descendido'],
    esport:['ctx-esport','🎮 Esport'],
    clean:['ctx-clean','✓ OK'],
  };
  const [cls,lbl]=m[id]||['ctx-warn',id];
  return `<span class="ctx-tag ${cls}">${lbl}</span>`;
}

function renderGoldPick(p, rank){
  const edgePct=(p.edge*100).toFixed(1);
  const roiPct=fmtPct(p.roi_diario_pct);
  const dep={Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';
  return `<div class="gold-pick">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div class="gold-rank">${rank}</div>
      <div style="flex:1;min-width:160px">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span class="badge b-gray" style="font-size:10px">${dep} ${p.deporte}</span>
          <span style="font-size:10px;color:var(--text3)">${p.liga}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--text3)">🕐 ${p.hora_local}</span>`:''}
        </div>
        <div style="font-size:14px;font-weight:600;margin-bottom:2px">${p.evento}</div>
        <div style="font-size:12px;color:var(--text2)">
          Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> ·
          Cuota referencia: <strong>@${fmt(p.odds_stake,2)}</strong>
        </div>
      </div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;text-align:center">
        <div>
          <div style="font-size:10px;color:var(--text2);margin-bottom:2px">Edge</div>
          <div style="font-size:18px;font-weight:700;color:${edgeColor(p.edge)}">+${edgePct}%</div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text2);margin-bottom:2px">Stake</div>
          <div style="font-size:18px;font-weight:700">${fmtUSD(p.stake_usd)}</div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text2);margin-bottom:2px">Ganancia pot.</div>
          <div style="font-size:18px;font-weight:700;color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text2);margin-bottom:2px">ROI bankroll</div>
          <div style="font-size:18px;font-weight:700;color:var(--green)">${roiPct}</div>
        </div>
      </div>
    </div>
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--text2)">
      📌 Buscá esta cuota en <strong style="color:var(--text)">Stake.com</strong> — si paga ≥ @${fmt(p.odds_stake,2)} colocá ${fmtUSD(p.stake_usd)} USD
    </div>
  </div>`;
}

function renderPickCard(p, descartado=false){
  const edgePct=(p.edge*100).toFixed(1);
  const barW=Math.min(Math.abs(p.edge)/0.15*100,100).toFixed(1);
  const barColor=edgeColor(p.edge);
  const dep={Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';
  const goldBadge=p.es_gold?'<span class="badge b-gold">⭐ Gold</span>':'';
  return `<div class="pick${descartado?' descartado':''}">
    <div class="pick-top">
      <div class="pick-info">
        <div class="pick-meta">
          ${goldBadge}
          <span class="badge b-gray" style="font-size:10px">${dep} ${p.deporte}</span>
          <span style="font-size:10px;color:var(--text3)">${p.liga}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--text3)">🕐 ${p.hora_local}</span>`:''}
          ${ctxTag(p.contexto_id)}
        </div>
        <div class="pick-evento">${p.evento}</div>
        <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_stake,2)}</div>
        ${p.razon_descarte&&descartado?`<div style="font-size:11px;color:var(--amber);margin-top:4px">⚠ ${p.razon_descarte}</div>`:''}
      </div>
      <div>
        <div class="edge-val" style="color:${barColor}">${p.edge>=0?'+':''}${edgePct}%</div>
        <div class="edge-lbl">edge</div>
        <div style="font-size:11px;color:var(--green);text-align:right;margin-top:2px">ROI ${fmtPct(p.roi_diario_pct)}</div>
      </div>
    </div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${barW}%;background:${barColor}"></div></div>
    <div class="bar-labels">
      <span>Prob. mercado: ${(p.prob_ajustada*100).toFixed(1)}%</span>
      <span>Prob. implícita cuota: ${(1/p.odds_stake*100).toFixed(1)}%</span>
    </div>
    ${!descartado?`
    <div class="pick-bottom">
      <div class="pick-nums">
        <div><div class="num-label">Stake (1/2 Kelly)</div><div class="num-val">${fmtUSD(p.stake_usd)}</div></div>
        <div><div class="num-label">Ganancia potencial</div><div class="num-val" style="color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div></div>
        <div><div class="num-label">ROI sobre bankroll</div><div class="num-val" style="color:var(--green)">${fmtPct(p.roi_diario_pct)}</div></div>
      </div>
      <button class="btn" onclick="marcar(this)">✓ Colocado en Stake</button>
    </div>`:`
    <div style="padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--red)">✗ ${p.razon_descarte||'Descartado'}</div>`}
  </div>`;
}

function renderGold(){
  if(!DATA) return;
  const gold = DATA.gold_tips||[];
  const sec  = document.getElementById('gold-section');
  if(!gold.length){ sec.style.display='none'; return; }
  sec.style.display='block';
  document.getElementById('gold-lista').innerHTML = gold.map((p,i)=>renderGoldPick(p,i+1)).join('');
  document.getElementById('gold-roi-val').textContent = fmtPct(DATA.roi_gold_potencial||0);
  document.getElementById('gold-expo-val').textContent = fmtUSD(DATA.expo_gold_usd||0);
}

function renderPicks(){
  if(!DATA) return;
  const minEdge = parseFloat(document.getElementById('min-edge').value)/100;
  let picks = (DATA.picks_validos||[]).filter(p=>p.edge>=minEdge);
  if(currentFilter!=='todos') picks=picks.filter(p=>p.deporte===currentFilter);
  const el = document.getElementById('lista-validos');
  if(!picks.length){
    el.innerHTML=`<div class="empty">🔍 Sin picks con edge ≥ ${(minEdge*100).toFixed(0)}% en este filtro.</div>`;
    return;
  }
  el.innerHTML = picks.map(p=>renderPickCard(p,false)).join('');
}

function renderDescartados(){
  if(!DATA) return;
  const el = document.getElementById('lista-descartados');
  const picks = DATA.picks_descartados||[];
  if(!picks.length){ el.innerHTML='<div class="empty">✓ Ningún pick descartado.</div>'; return; }
  el.innerHTML = picks.map(p=>renderPickCard(p,true)).join('');
}

function updateMetrics(){
  if(!DATA) return;
  document.getElementById('m-bank').textContent    = fmtUSD(DATA.bankroll);
  document.getElementById('m-gold').textContent    = (DATA.gold_tips||[]).length;
  document.getElementById('m-roi').textContent     = fmtPct(DATA.roi_gold_potencial||0);
  document.getElementById('m-expo').textContent    = fmtUSD(DATA.expo_gold_usd||0);
  document.getElementById('m-validos').textContent = (DATA.picks_validos||[]).length;
  document.getElementById('m-eventos').textContent = DATA.total_eventos||0;
}

function filtrar(f,btn){
  currentFilter=f;
  document.querySelectorAll('.pill').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderPicks();
}

function showTab(name,el){
  ['validos','descartados'].forEach(t=>document.getElementById('tab-'+t).style.display=t===name?'block':'none');
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  currentTab=name;
  if(name==='descartados') renderDescartados();
}

function marcar(btn){
  btn.textContent='✓ Colocado';
  btn.style.color='#22c55e';
  btn.style.borderColor='#22c55e';
  btn.disabled=true;
}

async function fetchData(){
  try{
    const r  = await fetch('/api/picks');
    const d  = await r.json();
    if(d.scanning&&!d.picks_validos){
      document.getElementById('lista-validos').innerHTML=`<div class="empty"><div class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</div>Analizando mercados...<br><span style="font-size:12px">Puede tardar 1-2 minutos</span></div>`;
      setTimeout(fetchData,5000); return;
    }
    if(d.error){
      document.getElementById('lista-validos').innerHTML=`<div class="empty" style="color:var(--red)">⚠ ${d.error}</div>`;
      return;
    }
    DATA=d;
    document.getElementById('scan-badge').className='badge b-green';
    document.getElementById('scan-badge').textContent='● Live';
    document.getElementById('conn-dot').style.background='#22c55e';
    document.getElementById('last-scan').textContent=d.ultimo_scan?'Último scan: '+d.ultimo_scan:'';
    updateMetrics();
    renderGold();
    renderPicks();
    if(currentTab==='descartados') renderDescartados();
  } catch(e){ setTimeout(fetchData,8000); }
}

async function triggerScan(){
  const btn=document.getElementById('btn-scan');
  btn.disabled=true; btn.textContent='↻ Escaneando...';
  document.getElementById('scan-badge').className='badge b-amber';
  document.getElementById('scan-badge').textContent='Escaneando...';
  await fetch('/api/scan',{method:'POST'});
  setTimeout(()=>{ btn.disabled=false; btn.textContent='↻ Escanear'; fetchData(); },3000);
}

fetchData();
setInterval(fetchData,300000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)
