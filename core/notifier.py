"""
Notificador Telegram multi-usuario
Cada usuario premium recibe los Gold Tips en su propio chat de Telegram.
"""
import os, logging, asyncio
import requests

log = logging.getLogger("stakebot.tg")

OWNER_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_message(token: str, chat_id: str, texto: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning(f"Telegram error {chat_id}: {e}")
        return False

def formato_gold_tip(pick: dict, rank: int, bankroll: float = 1000) -> str:
    dep = {"Fútbol":"⚽","Tenis":"🎾","Básquet":"🏀",
           "Esports":"🎮","MMA":"🥊","Béisbol":"⚾"}.get(pick["deporte"], "🎯")
    edge_pct = f"{pick['edge']*100:.1f}"

    # Recalcular stake según bankroll del usuario
    stake = round(bankroll * pick.get("stake_pct", 0.02), 2)
    ganancia = round(stake * (pick["odds_stake"] - 1), 2)
    roi = round(ganancia / bankroll * 100, 2) if bankroll else 0

    return (
        f"⭐ <b>GOLD TIP #{rank} — Stake Gold IA</b>\n\n"
        f"{dep} <b>{pick['evento']}</b>\n"
        f"🏆 {pick['liga']}\n"
        f"🕐 {pick['hora_local']}\n\n"
        f"🎯 Pick: <b>{pick['equipo_pick']}</b>\n"
        f"💰 Cuota referencia: <b>@{pick['odds_stake']:.2f}</b>\n\n"
        f"📊 Edge: <b>+{edge_pct}%</b>\n"
        f"🏦 Stake sugerido: <b>${stake:.2f}</b>\n"
        f"✅ Ganancia potencial: <b>+${ganancia:.2f}</b>\n"
        f"📈 ROI: <b>+{roi:.2f}%</b>\n\n"
        f"📌 Buscá en <b>Stake.com</b> y apostá si la cuota es ≥ @{pick['odds_stake']:.2f}"
    )

def formato_resumen(gold_picks: list, roi_total: float) -> str:
    if not gold_picks:
        return "🔍 <b>Sin Gold Tips por ahora</b>\nEl motor sigue escaneando el mercado."

    lista = ""
    for i, p in enumerate(gold_picks, 1):
        lista += f"\n{i}. <b>{p['evento']}</b>\n   {p['equipo_pick']} @{p['odds_stake']:.2f} · Edge +{p['edge']*100:.1f}%"

    return (
        f"⭐ <b>{len(gold_picks)} Gold Tips — Stake Gold IA</b>\n"
        f"ROI potencial: <b>+{roi_total:.2f}%</b>\n"
        f"{lista}\n\n"
        f"<i>Revisá el dashboard para el detalle completo.</i>"
    )

def notificar_owner(texto: str):
    """Notifica al dueño de la plataforma."""
    send_message(OWNER_TOKEN, OWNER_CHAT_ID, texto)

def notificar_usuarios_premium(resultado: dict, usuarios: list, ya_enviados: set):
    """
    Envía Gold Tips a todos los usuarios premium con Telegram configurado.
    Solo envía picks nuevos (no repetidos).
    """
    gold = resultado.get("gold_tips", [])
    nuevos = [p for p in gold if p["id"] not in ya_enviados]

    if not nuevos:
        log.info("Sin Gold Tips nuevos para notificar")
        return ya_enviados

    usuarios_premium = [
        u for u in usuarios
        if u.get("plan") in ("premium", "admin")
        and u.get("tg_activo")
        and u.get("tg_chat_id")
    ]

    log.info(f"Notificando {len(nuevos)} Gold Tips a {len(usuarios_premium)} usuarios premium")

    for usuario in usuarios_premium:
        bankroll = usuario.get("bankroll", 1000)
        chat_id  = usuario["tg_chat_id"]

        # Resumen primero
        resumen = formato_resumen(nuevos, resultado.get("roi_gold_potencial", 0))
        send_message(OWNER_TOKEN, chat_id, resumen)

        # Cada pick individual
        for i, pick in enumerate(nuevos, 1):
            texto = formato_gold_tip(pick, i, bankroll)
            send_message(OWNER_TOKEN, chat_id, texto)

    # Notificar también al owner
    resumen_owner = formato_resumen(nuevos, resultado.get("roi_gold_potencial", 0))
    notificar_owner(resumen_owner + f"\n\n👥 Enviado a {len(usuarios_premium)} usuarios premium")

    # Marcar como enviados
    for p in nuevos:
        ya_enviados.add(p["id"])

    return ya_enviados
