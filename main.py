import os, asyncio, logging
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stakebot")

TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL_MIN", 30)) * 60  # a segundos

cache = {
    "ultimo_scan":      None,
    "resultado":        None,
    "scanning":         False,
    "error":            None,
    "gold_enviados":    set(),  # IDs ya notificados para no repetir
}

# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_send(texto: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": texto, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning(f"Telegram error: {e}")
        return False

def tg_gold_tip(pick: dict, rank: int) -> str:
    dep = {"Fútbol":"⚽","Tenis":"🎾","Básquet":"🏀","Esports":"🎮","MMA":"🥊","Béisbol":"⚾"}.get(pick["deporte"],"🎯")
    edge_pct  = f"{pick['edge']*100:.1f}"
    roi_pct   = f"+{pick['roi_diario_pct']:.2f}"
    return (
        f"⭐ <b>GOLD TIP #{rank} — Stake Bot</b>\n\n"
        f"{dep} <b>{pick['evento']}</b>\n"
        f"🏆 {pick['liga']}\n"
        f"🕐 {pick['hora_local']}\n\n"
        f"🎯 Pick: <b>{pick['equipo_pick']}</b>\n"
        f"💰 Cuota referencia: <b>@{pick['odds_stake']:.2f}</b>\n\n"
        f"📊 Edge: <b>+{edge_pct}%</b>\n"
        f"🏦 Stake: <b>${pick['stake_usd']:.2f} USD</b>\n"
        f"✅ Ganancia potencial: <b>+${pick['ganancia_pot']:.2f}</b>\n"
        f"📈 ROI bankroll: <b>{roi_pct}%</b>\n\n"
        f"📌 Buscá esta cuota en <b>Stake.com</b>\n"
        f"Si paga ≥ @{pick['odds_stake']:.2f} → apostá ${pick['stake_usd']:.2f} USD"
    )

def tg_resumen(resultado: dict) -> str:
    gold  = resultado.get("gold_tips", [])
    total = len(resultado.get("picks_validos", []))
    roi   = resultado.get("roi_gold_potencial", 0)
    expo  = resultado.get("expo_gold_usd", 0)
    ventana = resultado.get("ventana_horas", 36)

    if not gold:
        return (
            f"🔍 <b>Scan completado — Sin Gold Tips</b>\n\n"
            f"Eventos analizados: {resultado.get('total_eventos',0)}\n"
            f"Picks válidos: {total}\n"
            f"Ventana: próximas {ventana}hs\n\n"
            f"Próximo scan en {SCAN_INTERVAL//60} minutos."
        )

    lista = ""
    for i, p in enumerate(gold, 1):
        lista += f"\n{i}. <b>{p['evento']}</b> — {p['equipo_pick']} @{p['odds_stake']:.2f} · Edge +{p['edge']*100:.1f}%"

    return (
        f"⭐ <b>{len(gold)} Gold Tips encontrados</b>\n"
        f"ROI potencial total: <b>+{roi:.2f}%</b> | Exposición: <b>${expo:.2f}</b>\n"
        f"{lista}\n\n"
        f"<i>Revisá el dashboard para el detalle completo.</i>"
    )

def notificar_gold_tips(resultado: dict):
    """Envía por Telegram solo los Gold Tips nuevos (no repetidos)."""
    gold = resultado.get("gold_tips", [])
    nuevos = [p for p in gold if p["id"] not in cache["gold_enviados"]]

    if not nuevos:
        log.info("Telegram: sin Gold Tips nuevos para notificar")
        return

    # Resumen primero
    tg_send(tg_resumen(resultado))

    # Luego cada pick individualmente
    for i, pick in enumerate(nuevos, 1):
        tg_send(tg_gold_tip(pick, i))
        cache["gold_enviados"].add(pick["id"])
        asyncio.sleep(0.5)  # pequeña pausa entre mensajes

    log.info(f"Telegram: {len(nuevos)} Gold Tips enviados")

# ── Scanner loop ──────────────────────────────────────────────────────────────

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
        # Notificar Telegram
        notificar_gold_tips(resultado)
    except Exception as e:
        cache["error"] = str(e)
        log.error(f"Error en scan: {e}")
    finally:
        cache["scanning"] = False

async def scanner_loop():
    """Corre el scan automáticamente cada SCAN_INTERVAL segundos."""
    while True:
        await run_scan_bg()
        log.info(f"Próximo scan en {SCAN_INTERVAL//60} minutos")
        await asyncio.sleep(SCAN_INTERVAL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scanner_loop())
    yield

app = FastAPI(title="Stake Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Endpoints ─────────────────────────────────────────────────────────────────

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
        "ok":            True,
        "ultimo_scan":   cache["ultimo_scan"],
        "scanning":      cache["scanning"],
        "picks_validos": len(r["picks_validos"]) if r else 0,
        "gold_tips":     len(r.get("gold_tips",[])) if r else 0,
        "roi_gold_pct":  r.get("roi_gold_potencial", 0) if r else 0,
        "telegram":      bool(TG_TOKEN and TG_CHAT_ID),
        "scan_interval_min": SCAN_INTERVAL // 60,
    })

# ── Dashboard ─────────────────────────────────────────────────────────────────

DASHBOARD = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stake Bot</title>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--border:#2e3348;--text:#e8eaf0;--text2:#8b92a8;--text3:#555e7a;--green:#22c55e;--green-bg:rgba(34,197,94,.12);--red:#ef4444;--red-bg:rgba(239,68,68,.12);--amber:#f59e0b;--amber-bg:rgba(245,158,11,.12);--purple:#8b5cf6;--purple-bg:rgba(139,92,246,.12);--gold:#f59e0b;--gold-bg:rgba(245,158,11,.12);--radius:10px;--radius-sm:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
.logo{font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
.logo-dot{width:8px;height:8px;border-radius:50%;background:#555e7a}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:500}
.b-green{background:var(--green-bg);color:var(--green)}
.b-amber{background:var(--amber-bg);color:var(--amber)}
.b-purple{background:var(--purple-bg);color:var(--purple)}
.b-gray{background:var(--bg3);color:var(--text2)}
.b-gold{background:var(--gold-bg);color:var(--gold);border:1px solid rgba(245,158,11,.3)}
.b-tg{background:rgba(41,182,246,.12);color:#29b6f6}
.btn{padding:7px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;cursor:pointer;transition:all .15s}
.btn:hover{background:var(--border)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.container{max-width:1100px;margin:0 auto;padding:20px 16px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.metric-label{font-size:11px;color:var(--text2);margin-bottom:5px}
.metric-val{font-size:21px;font-weight:600}
.metric-sub{font-size:10px;color:var(--text3);margin-top:2px}
.gold-section{background:rgba(245,158,11,.05);border:1px solid rgba(245,158,11,.2);border-radius:var(--radius);padding:16px 18px;margin-bottom:16px}
.gold-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.gold-title{font-size:15px;font-weight:600;color:var(--gold);display:flex;align-items:center;gap:8px}
.gold-pick{background:var(--bg2);border:1px solid rgba(245,158,11,.15);border-radius:var(--radius);padding:14px 16px;margin-bottom:8px}
.gold-pick:last-child{margin-bottom:0}
.gold-rank{width:28px;height:28px;border-radius:50%;background:var(--gold-bg);border:1px solid rgba(245,158,11,.4);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:var(--gold);flex-shrink:0}
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
.countdown{font-size:11px;color:var(--text3);margin-left:8px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">
    <div class="logo-dot" id="conn-dot"></div>
    Stake Bot
    <span class="badge b-purple" style="font-size:10px">v2</span>
    <span id="tg-badge" class="badge b-tg" style="display:none">📨 Telegram activo</span>
  </div>
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span id="scan-badge" class="badge b-amber">Iniciando...</span>
    <span id="last-scan" style="font-size:11px;color:var(--text2)"></span>
    <span id="next-scan" class="countdown"></span>
    <button class="btn" id="btn-scan" onclick="triggerScan()">↻ Escanear ahora</button>
  </div>
</div>

<div class="container">

  <div class="metrics">
    <div class="metric"><div class="metric-label">Bankroll</div><div class="metric-val" id="m-bank">—</div><div class="metric-sub">USD</div></div>
    <div class="metric"><div class="metric-label">⭐ Gold Tips hoy</div><div class="metric-val" id="m-gold" style="color:var(--gold)">—</div><div class="metric-sub">picks premium</div></div>
    <div class="metric"><div class="metric-label">ROI potencial gold</div><div class="metric-val" id="m-roi" style="color:var(--green)">—</div><div class="metric-sub">si todos son verdes</div></div>
    <div class="metric"><div class="metric-label">Exposición gold</div><div class="metric-val" id="m-expo">—</div><div class="metric-sub">USD en juego</div></div>
    <div class="metric"><div class="metric-label">Picks válidos</div><div class="metric-val" id="m-validos">—</div><div class="metric-sub">edge ≥ 3%</div></div>
    <div class="metric"><div class="metric-label">Ventana temporal</div><div class="metric-val" id="m-ventana">—</div><div class="metric-sub">próximas horas</div></div>
  </div>

  <div class="gold-section" id="gold-section" style="display:none">
    <div class="gold-header">
      <div class="gold-title">⭐ Gold Tips del día</div>
      <div style="font-size:12px;color:var(--text2);display:flex;gap:14px;flex-wrap:wrap">
        <span>ROI potencial: <strong id="gold-roi-val" style="color:var(--green)">—</strong></span>
        <span>Exposición: <strong id="gold-expo-val">—</strong></span>
        <span id="tg-status" style="color:var(--text3)"></span>
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
let nextScanSec = 0;
let countdownTimer = null;

function fmt(n,d=2){ return n!=null?Number(n).toFixed(d):'—'; }
function fmtUSD(n){ return '$'+fmt(n,2); }
function fmtPct(n){ return (n>=0?'+':'')+fmt(n,1)+'%'; }
function edgeColor(e){ return e>=0.10?'#8b5cf6':e>=0.06?'#22c55e':'#8b92a8'; }

function ctxTag(id){
  const m={champion_early:['ctx-warn','⚠ Campeón'],relegated:['ctx-bad','⬇ Descendido'],esport:['ctx-esport','🎮 Esport'],clean:['ctx-clean','✓ OK']};
  const [cls,lbl]=m[id]||['ctx-warn',id];
  return `<span class="ctx-tag ${cls}">${lbl}</span>`;
}

function startCountdown(minutes){
  if(countdownTimer) clearInterval(countdownTimer);
  nextScanSec = minutes * 60;
  countdownTimer = setInterval(()=>{
    nextScanSec--;
    if(nextScanSec <= 0){ clearInterval(countdownTimer); return; }
    const m = Math.floor(nextScanSec/60);
    const s = nextScanSec % 60;
    document.getElementById('next-scan').textContent = `· próximo scan en ${m}:${String(s).padStart(2,'0')}`;
  }, 1000);
}

function renderGoldPick(p, rank){
  const dep={Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';
  return `<div class="gold-pick">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div class="gold-rank">${rank}</div>
      <div style="flex:1;min-width:160px">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span class="badge b-gray" style="font-size:10px">${dep} ${p.deporte}</span>
          <span style="font-size:10px;color:var(--text3)">${p.liga}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--amber)">🕐 ${p.hora_local}</span>`:''}
        </div>
        <div style="font-size:14px;font-weight:600;margin-bottom:2px">${p.evento}</div>
        <div style="font-size:12px;color:var(--text2)">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · Cuota ref: <strong>@${fmt(p.odds_stake,2)}</strong></div>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;text-align:center">
        <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">Edge</div><div style="font-size:17px;font-weight:700;color:${edgeColor(p.edge)}">+${(p.edge*100).toFixed(1)}%</div></div>
        <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">Stake</div><div style="font-size:17px;font-weight:700">${fmtUSD(p.stake_usd)}</div></div>
        <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">Ganancia pot.</div><div style="font-size:17px;font-weight:700;color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div></div>
        <div><div style="font-size:10px;color:var(--text2);margin-bottom:2px">ROI</div><div style="font-size:17px;font-weight:700;color:var(--green)">${fmtPct(p.roi_diario_pct)}</div></div>
      </div>
    </div>
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--text2)">
      📌 Buscá en <strong style="color:var(--text)">Stake.com</strong> — si paga ≥ @${fmt(p.odds_stake,2)} apostá <strong style="color:var(--text)">${fmtUSD(p.stake_usd)} USD</strong>
    </div>
  </div>`;
}

function renderPickCard(p, desc=false){
  const dep={Fútbol:'⚽',Tenis:'🎾',Básquet:'🏀',Esports:'🎮',MMA:'🥊',Béisbol:'⚾'}[p.deporte]||'🎯';
  const barW=Math.min(Math.abs(p.edge)/0.15*100,100).toFixed(1);
  const col=edgeColor(p.edge);
  return `<div class="pick${desc?' descartado':''}">
    <div class="pick-top">
      <div class="pick-info">
        <div class="pick-meta">
          ${p.es_gold?'<span class="badge b-gold">⭐ Gold</span>':''}
          <span class="badge b-gray" style="font-size:10px">${dep} ${p.deporte}</span>
          <span style="font-size:10px;color:var(--text3)">${p.liga}</span>
          ${p.hora_local?`<span style="font-size:10px;color:var(--amber)">🕐 ${p.hora_local}</span>`:''}
          ${ctxTag(p.contexto_id)}
        </div>
        <div class="pick-evento">${p.evento}</div>
        <div class="pick-sub">Pick: <strong style="color:var(--text)">${p.equipo_pick}</strong> · @${fmt(p.odds_stake,2)}</div>
        ${p.razon_descarte&&desc?`<div style="font-size:11px;color:var(--amber);margin-top:4px">⚠ ${p.razon_descarte}</div>`:''}
      </div>
      <div>
        <div class="edge-val" style="color:${col}">${p.edge>=0?'+':''}${(p.edge*100).toFixed(1)}%</div>
        <div class="edge-lbl">edge</div>
        <div style="font-size:11px;color:var(--green);text-align:right;margin-top:2px">ROI ${fmtPct(p.roi_diario_pct)}</div>
      </div>
    </div>
    <div class="bar-wrap"><div class="bar-fill" style="width:${barW}%;background:${col}"></div></div>
    <div class="bar-labels">
      <span>Prob. mercado: ${(p.prob_ajustada*100).toFixed(1)}%</span>
      <span>Prob. implícita: ${(1/p.odds_stake*100).toFixed(1)}%</span>
    </div>
    ${!desc?`
    <div class="pick-bottom">
      <div class="pick-nums">
        <div><div class="num-label">Stake (1/2 Kelly)</div><div class="num-val">${fmtUSD(p.stake_usd)}</div></div>
        <div><div class="num-label">Ganancia pot.</div><div class="num-val" style="color:var(--green)">+${fmtUSD(p.ganancia_pot)}</div></div>
        <div><div class="num-label">ROI bankroll</div><div class="num-val" style="color:var(--green)">${fmtPct(p.roi_diario_pct)}</div></div>
      </div>
      <button class="btn" onclick="marcar(this)">✓ Colocado en Stake</button>
    </div>`:`
    <div style="padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--red)">✗ ${p.razon_descarte||'Descartado'}</div>`}
  </div>`;
}

function renderGold(){
  if(!DATA) return;
  const gold=DATA.gold_tips||[];
  const sec=document.getElementById('gold-section');
  if(!gold.length){sec.style.display='none';return;}
  sec.style.display='block';
  document.getElementById('gold-lista').innerHTML=gold.map((p,i)=>renderGoldPick(p,i+1)).join('');
  document.getElementById('gold-roi-val').textContent=fmtPct(DATA.roi_gold_potencial||0);
  document.getElementById('gold-expo-val').textContent=fmtUSD(DATA.expo_gold_usd||0);
}

function renderPicks(){
  if(!DATA) return;
  const minE=parseFloat(document.getElementById('min-edge').value)/100;
  let picks=(DATA.picks_validos||[]).filter(p=>p.edge>=minE);
  if(currentFilter!=='todos') picks=picks.filter(p=>p.deporte===currentFilter);
  const el=document.getElementById('lista-validos');
  el.innerHTML=picks.length?picks.map(p=>renderPickCard(p,false)).join(''):`<div class="empty">🔍 Sin picks con edge ≥ ${(minE*100).toFixed(0)}%</div>`;
}

function renderDescartados(){
  if(!DATA) return;
  const el=document.getElementById('lista-descartados');
  const picks=DATA.picks_descartados||[];
  el.innerHTML=picks.length?picks.map(p=>renderPickCard(p,true)).join(''):'<div class="empty">✓ Ningún pick descartado.</div>';
}

function updateMetrics(){
  if(!DATA) return;
  document.getElementById('m-bank').textContent=fmtUSD(DATA.bankroll);
  document.getElementById('m-gold').textContent=(DATA.gold_tips||[]).length;
  document.getElementById('m-roi').textContent=fmtPct(DATA.roi_gold_potencial||0);
  document.getElementById('m-expo').textContent=fmtUSD(DATA.expo_gold_usd||0);
  document.getElementById('m-validos').textContent=(DATA.picks_validos||[]).length;
  document.getElementById('m-ventana').textContent=(DATA.ventana_horas||36)+'hs';
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
  if(name==='descartados')renderDescartados();
}

function marcar(btn){btn.textContent='✓ Colocado';btn.style.color='#22c55e';btn.style.borderColor='#22c55e';btn.disabled=true;}

async function fetchData(){
  try{
    const r=await fetch('/api/picks');
    const d=await r.json();
    if(d.scanning&&!d.picks_validos){
      document.getElementById('lista-validos').innerHTML='<div class="empty"><div class="spin" style="font-size:24px;display:block;margin-bottom:8px">↻</div>Analizando mercados...</div>';
      setTimeout(fetchData,5000);return;
    }
    if(d.error){document.getElementById('lista-validos').innerHTML=`<div class="empty" style="color:var(--red)">⚠ ${d.error}</div>`;return;}
    DATA=d;
    document.getElementById('scan-badge').className='badge b-green';
    document.getElementById('scan-badge').textContent='● Live';
    document.getElementById('conn-dot').style.background='#22c55e';
    document.getElementById('last-scan').textContent=d.ultimo_scan?'Último scan: '+d.ultimo_scan:'';
    updateMetrics();
    renderGold();
    renderPicks();
    if(currentTab==='descartados')renderDescartados();
  }catch(e){setTimeout(fetchData,8000);}
}

async function fetchStatus(){
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    if(d.telegram){
      document.getElementById('tg-badge').style.display='inline-flex';
      document.getElementById('tg-status').textContent='📨 Enviado por Telegram';
    }
    if(d.scan_interval_min) startCountdown(d.scan_interval_min);
  }catch(e){}
}

async function triggerScan(){
  const btn=document.getElementById('btn-scan');
  btn.disabled=true;btn.textContent='↻ Escaneando...';
  document.getElementById('scan-badge').className='badge b-amber';
  document.getElementById('scan-badge').textContent='Escaneando...';
  await fetch('/api/scan',{method:'POST'});
  setTimeout(()=>{btn.disabled=false;btn.textContent='↻ Escanear ahora';fetchData();},3000);
}

fetchData();
fetchStatus();
setInterval(fetchData,300000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)
