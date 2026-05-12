import os, hashlib, secrets, logging
import asyncpg
from datetime import datetime
from typing import Optional

log = logging.getLogger("stakebot.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT DEFAULT 'free',
                activo BOOLEAN DEFAULT TRUE,
                bankroll FLOAT DEFAULT 1000,
                moneda TEXT DEFAULT 'USD',
                perfil_riesgo TEXT DEFAULT 'inteligente',
                tg_chat_id TEXT DEFAULT '',
                tg_activo BOOLEAN DEFAULT FALSE,
                codigo_invitacion TEXT DEFAULT '',
                fecha_registro TIMESTAMPTZ DEFAULT NOW(),
                ultimo_login TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS invitaciones (
                id SERIAL PRIMARY KEY,
                codigo TEXT UNIQUE NOT NULL,
                plan TEXT DEFAULT 'premium',
                usado BOOLEAN DEFAULT FALSE,
                usado_por INTEGER,
                creado_por INTEGER,
                fecha_creacion TIMESTAMPTZ DEFAULT NOW(),
                fecha_uso TIMESTAMPTZ,
                max_usos INTEGER DEFAULT 1,
                usos_actuales INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sesiones (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER,
                token TEXT UNIQUE NOT NULL,
                fecha_creacion TIMESTAMPTZ DEFAULT NOW(),
                fecha_expiracion TIMESTAMPTZ,
                activa BOOLEAN DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS historial_picks (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id),
                pick_id TEXT NOT NULL,
                evento TEXT,
                liga TEXT,
                deporte TEXT,
                mercado TEXT,
                equipo_pick TEXT,
                odds_ref FLOAT,
                odds_real FLOAT,
                stake_usd FLOAT,
                tipo TEXT DEFAULT 'value',
                es_gold BOOLEAN DEFAULT FALSE,
                estado TEXT DEFAULT 'pendiente',
                es_cashout BOOLEAN DEFAULT FALSE,
                odds_cashout FLOAT,
                pnl FLOAT,
                bankroll_antes FLOAT,
                bankroll_despues FLOAT,
                fecha_colocado TIMESTAMPTZ DEFAULT NOW(),
                fecha_resultado TIMESTAMPTZ,
                UNIQUE(usuario_id, pick_id)
            );
            CREATE TABLE IF NOT EXISTS bankroll_historial (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id),
                monto FLOAT NOT NULL,
                tipo TEXT NOT NULL,
                descripcion TEXT,
                fecha TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        admin_email = os.getenv("ADMIN_EMAIL", "admin@stakebot.com")
        admin_pass  = os.getenv("ADMIN_PASSWORD", "admin1234")
        ph = hash_password(admin_pass)
        await conn.execute("""
            INSERT INTO usuarios (email, username, password_hash, plan)
            VALUES ($1, $2, $3, 'admin') ON CONFLICT DO NOTHING
        """, admin_email, "admin", ph)
        # Migración: agregar columnas nuevas si no existen
        migraciones = [
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS odds_ref FLOAT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS odds_real FLOAT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS odds_cashout FLOAT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS es_cashout BOOLEAN DEFAULT FALSE",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS bankroll_antes FLOAT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS bankroll_engine FLOAT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS bankroll_despues FLOAT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS fecha_resultado TIMESTAMPTZ",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS mercado TEXT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS liga TEXT",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS tipo TEXT DEFAULT 'value'",
            "ALTER TABLE historial_picks ADD COLUMN IF NOT EXISTS es_gold BOOLEAN DEFAULT FALSE",
            "CREATE TABLE IF NOT EXISTS bankroll_historial (id SERIAL PRIMARY KEY, usuario_id INTEGER REFERENCES usuarios(id), monto FLOAT NOT NULL, tipo TEXT NOT NULL, descripcion TEXT, fecha TIMESTAMPTZ DEFAULT NOW())",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fecha_vencimiento TIMESTAMPTZ",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS trial_usado BOOLEAN DEFAULT FALSE",
        ]
        for sql in migraciones:
            try:
                await conn.execute(sql)
            except Exception as e:
                log.warning(f"Migración: {e}")
        await conn.execute("UPDATE historial_picks SET odds_ref=odds WHERE odds_ref IS NULL AND odds IS NOT NULL")

        # Migración automática: actualizar stakes guardados en USD al bankroll del usuario
        try:
            usuarios = await conn.fetch("SELECT id, bankroll FROM usuarios WHERE bankroll > 1000")
            for u in usuarios:
                bankroll = float(u["bankroll"])
                await conn.execute("""
                    UPDATE historial_picks
                    SET stake_usd = ROUND((stake_usd / COALESCE(bankroll_engine, 1000) * $1)::numeric, 2),
                        bankroll_engine = $1
                    WHERE usuario_id = $2
                    AND (bankroll_engine IS NULL OR bankroll_engine <= 1000)
                    AND stake_usd < 1000
                """, bankroll, u["id"])
            log.info("Migración de stakes completada")
        except Exception as e:
            log.warning(f"Migración stakes: {e}")

    log.info("Base de datos inicializada")

def hash_password(password: str) -> str:
    salt = os.getenv("SECRET_KEY", "stakebot2025secretkey")
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

async def crear_usuario(email, username, password, codigo="") -> dict:
    ph = hash_password(password)
    plan = "free"
    pool = await get_pool()
    async with pool.acquire() as conn:
        if codigo:
            inv = await conn.fetchrow(
                "SELECT * FROM invitaciones WHERE codigo=$1 AND usos_actuales<max_usos",
                codigo.upper()
            )
            if not inv:
                return {"ok": False, "error": "Código de invitación inválido o ya usado"}
            plan = inv["plan"]
        try:
            row = await conn.fetchrow(
                "INSERT INTO usuarios (email,username,password_hash,plan,codigo_invitacion) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                email.lower().strip(), username.strip(), ph, plan, codigo
            )
            if codigo:
                await conn.execute(
                    "UPDATE invitaciones SET usos_actuales=usos_actuales+1, usado=(usos_actuales+1>=max_usos), usado_por=$1, fecha_uso=NOW() WHERE codigo=$2",
                    row["id"], codigo
                )
            return {"ok": True, "user_id": row["id"], "plan": plan}
        except asyncpg.UniqueViolationError:
            return {"ok": False, "error": "Email o usuario ya registrado"}

async def login(email, password) -> dict:
    ph = hash_password(password)
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM usuarios WHERE email=$1 AND password_hash=$2 AND activo=TRUE",
            email.lower().strip(), ph
        )
        if not user:
            return {"ok": False, "error": "Email o contraseña incorrectos"}
        token = secrets.token_urlsafe(32)
        await conn.execute(
            "INSERT INTO sesiones (usuario_id,token,fecha_expiracion) VALUES ($1,$2,NOW()+INTERVAL '30 days')",
            user["id"], token
        )
        await conn.execute("UPDATE usuarios SET ultimo_login=NOW() WHERE id=$1", user["id"])
        return {"ok": True, "token": token, "user": serialize_row(dict(user))}

async def get_user_by_token(token) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT u.* FROM usuarios u JOIN sesiones s ON s.usuario_id=u.id WHERE s.token=$1 AND s.activa=TRUE AND s.fecha_expiracion>NOW() AND u.activo=TRUE",
            token
        )
        return dict(row) if row else None

async def logout(token):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE sesiones SET activa=FALSE WHERE token=$1", token)

async def update_perfil(user_id, data) -> dict:
    permitidos = ["bankroll","moneda","perfil_riesgo","tg_chat_id","tg_activo","username"]
    campos, valores = [], []
    for k, v in data.items():
        if k in permitidos:
            campos.append(f"{k}=${len(valores)+1}")
            valores.append(v)
    if not campos:
        return {"ok": False, "error": "Sin campos válidos"}
    valores.append(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE usuarios SET {','.join(campos)} WHERE id=${len(valores)}", *valores
        )
    return {"ok": True}

async def ajustar_bankroll(user_id: int, monto: float, tipo: str, descripcion: str = "") -> dict:
    """Ajusta el bankroll del usuario y registra en el historial."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT bankroll FROM usuarios WHERE id=$1", user_id)
        if not user:
            return {"ok": False, "error": "Usuario no encontrado"}
        bankroll_antes = float(user["bankroll"])

        if tipo == "ajuste":
            # Ajuste manual = reemplazar el valor directamente
            nuevo = round(monto, 2)
            diferencia = round(nuevo - bankroll_antes, 2)
        else:
            # Depósito o retiro = sumar/restar
            nuevo = round(bankroll_antes + monto, 2)
            diferencia = monto

        await conn.execute("UPDATE usuarios SET bankroll=$1 WHERE id=$2", nuevo, user_id)
        row = await conn.fetchrow(
            "INSERT INTO bankroll_historial (usuario_id, monto, tipo, descripcion) VALUES ($1,$2,$3,$4) RETURNING id",
            user_id, diferencia, tipo, descripcion or f"Bankroll anterior: {bankroll_antes}"
        )
        return {"ok": True, "bankroll_nuevo": nuevo, "historial_id": row["id"]}

async def revertir_ajuste(user_id: int, historial_id: int) -> dict:
    """Revierte un ajuste de bankroll."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        hist = await conn.fetchrow(
            "SELECT * FROM bankroll_historial WHERE id=$1 AND usuario_id=$2",
            historial_id, user_id
        )
        if not hist:
            return {"ok": False, "error": "Ajuste no encontrado"}
        user = await conn.fetchrow("SELECT bankroll FROM usuarios WHERE id=$1", user_id)
        bankroll_actual = float(user["bankroll"])
        # Revertir = deshacer el efecto del ajuste
        nuevo = round(bankroll_actual - float(hist["monto"]), 2)
        await conn.execute("UPDATE usuarios SET bankroll=$1 WHERE id=$2", nuevo, user_id)
        await conn.execute("DELETE FROM bankroll_historial WHERE id=$1", historial_id)
        return {"ok": True, "bankroll_nuevo": nuevo}

# ── Historial picks ────────────────────────────────────────────────────────────

async def guardar_pick(user_id: int, pick: dict) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT bankroll, moneda FROM usuarios WHERE id=$1", user_id)
        bankroll_antes   = float(user["bankroll"]) if user else 0
        moneda           = user["moneda"] if user else "USD"
        stake_original   = float(pick.get("stake_usd") or 0)

        # Si el usuario tiene moneda ARS y el stake viene en USD, usarlo tal cual
        # El stake ya está calculado según el bankroll del usuario en /api/picks
        stake            = stake_original
        # bankroll_engine es el bankroll del usuario cuando se calculó el stake
        # Si viene del pick (ya calculado en ARS), usarlo directamente
        bankroll_engine_pick = float(pick.get("bankroll_engine") or bankroll_antes or 1)
        bankroll_despues = round(bankroll_antes - stake, 2)  # descontar al colocar

        try:
            # El pick ya viene con stake calculado sobre el bankroll del usuario
            # Guardamos bankroll_engine para referencia histórica exacta
            result = await conn.execute("""
                INSERT INTO historial_picks
                    (usuario_id, pick_id, evento, liga, deporte, mercado,
                     equipo_pick, odds_ref, stake_usd, tipo, es_gold, estado,
                     bankroll_antes, bankroll_despues, bankroll_engine)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'pendiente',$12,$13,$14)
                ON CONFLICT (usuario_id, pick_id) DO UPDATE
                SET estado = EXCLUDED.estado
            """,
                user_id,
                pick.get("id",""),
                pick.get("evento",""),
                pick.get("liga",""),
                pick.get("deporte",""),
                pick.get("mercado",""),
                pick.get("equipo_pick",""),
                float(pick.get("odds_ref") or pick.get("odds_stake") or 0),
                stake,
                pick.get("tipo","value"),
                bool(pick.get("es_gold", False)),
                bankroll_antes,
                bankroll_despues,
                bankroll_engine_pick,  # bankroll usado para calcular el stake
            )
            # Solo descontar si se insertó (no era duplicado)
            if result == "INSERT 0 1":
                await conn.execute(
                    "UPDATE usuarios SET bankroll=$1 WHERE id=$2",
                    bankroll_despues, user_id
                )
                await conn.execute(
                    "INSERT INTO bankroll_historial (usuario_id,monto,tipo,descripcion) VALUES ($1,$2,$3,$4)",
                    user_id, -stake, "pick_colocado",
                    f"{pick.get('evento','')} — stake colocado"
                )
            return {"ok": True, "bankroll_nuevo": bankroll_despues}
        except Exception as e:
            log.error(f"Error guardar_pick: {e}")
            return {"ok": False, "error": str(e)}

async def actualizar_resultado(pick_db_id: int, user_id: int, data: dict) -> dict:
    """
    Actualiza resultado con:
    - estado: ganado/perdido/void/cashout
    - odds_real: cuota real a la que se colocó
    - odds_cashout: cuota de cash out si aplica
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM historial_picks WHERE id=$1 AND usuario_id=$2",
            pick_db_id, user_id
        )
        if not row:
            return {"ok": False, "error": "Pick no encontrado"}

        estado       = data.get("estado", "")
        odds_real    = float(data.get("odds_real") or row["odds_ref"] or 0)
        odds_cashout = float(data.get("odds_cashout") or 0)
        stake        = float(row["stake_usd"] or 0)
        es_cashout   = estado == "cashout"

        if estado == "ganado":
            pnl = round(stake * (odds_real - 1), 2)
        elif estado == "perdido":
            pnl = -round(stake, 2)
        elif estado == "cashout" and odds_cashout > 0:
            pnl = round(stake * (odds_cashout - 1), 2)
        elif estado == "void":
            pnl = 0.0
        else:
            pnl = 0.0

        # Actualizar bankroll
        user = await conn.fetchrow("SELECT bankroll FROM usuarios WHERE id=$1", user_id)
        bankroll_antes  = float(user["bankroll"]) if user else 0
        bankroll_despues = round(bankroll_antes + pnl, 2)

        await conn.execute("""
            UPDATE historial_picks
            SET estado=$1, odds_real=$2, odds_cashout=$3, es_cashout=$4,
                pnl=$5, bankroll_despues=$6, fecha_resultado=NOW()
            WHERE id=$7
        """, estado, odds_real, odds_cashout if es_cashout else None,
             es_cashout, pnl, bankroll_despues, pick_db_id)

        # Actualizar bankroll del usuario
        await conn.execute("UPDATE usuarios SET bankroll=$1 WHERE id=$2", bankroll_despues, user_id)
        await conn.execute(
            "INSERT INTO bankroll_historial (usuario_id, monto, tipo, descripcion) VALUES ($1,$2,$3,$4)",
            user_id, pnl, "resultado_pick", f"{row['evento']} — {estado}"
        )

        return {"ok": True, "pnl": pnl, "bankroll_nuevo": bankroll_despues}

def serialize_row(row: dict) -> dict:
    """Convierte datetime a string para serialización JSON."""
    result = {}
    for k, v in row.items():
        if hasattr(v, 'isoformat'):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result

async def get_estadisticas(user_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            todos = await conn.fetch(
                "SELECT * FROM historial_picks WHERE usuario_id=$1 ORDER BY fecha_colocado DESC",
                user_id
            )
        except Exception as e:
            log.error(f"Error fetch historial: {e}")
            todos = []
        try:
            mes = await conn.fetch("""
                SELECT * FROM historial_picks
                WHERE usuario_id=$1 AND fecha_colocado >= NOW() - INTERVAL '30 days'
                ORDER BY fecha_colocado DESC
            """, user_id)
        except Exception as e:
            log.error(f"Error fetch mes: {e}")
            mes = []
        user = await conn.fetchrow("SELECT bankroll, moneda FROM usuarios WHERE id=$1", user_id)
        try:
            bankroll_hist = await conn.fetch(
                "SELECT * FROM bankroll_historial WHERE usuario_id=$1 ORDER BY fecha DESC LIMIT 30",
                user_id
            )
        except Exception as e:
            log.warning(f"bankroll_hist: {e}")
            bankroll_hist = []

        def stake_real(p, bankroll_actual):
            """
            Devuelve el stake en la moneda del usuario.
            Si el pick tiene bankroll_engine = bankroll del usuario → stake ya en ARS, ratio=1.
            Si el pick tiene bankroll_engine = 1000 (USD default) → aplicar ratio.
            """
            be = float(p.get("bankroll_engine") or p.get("bankroll_antes") or 0)
            su = float(p.get("stake_usd") or 0)
            if su <= 0: return 0
            if be <= 0: return su
            # Si el stake_engine es similar al bankroll actual → ya está en la moneda correcta
            if be > 1000:  # probablemente ya en ARS
                return su
            # Si stake_engine es pequeño (USD) → aplicar ratio
            pct = su / be
            return round(bankroll_actual * pct, 2)

        def calcular(picks, bk_actual=None):
            resueltos = [p for p in picks if p["estado"] in ("ganado","perdido","void","cashout")]
            pendientes = [p for p in picks if p["estado"] == "pendiente"]
            ganados   = [p for p in resueltos if p["estado"] in ("ganado","cashout")]
            perdidos  = [p for p in resueltos if p["estado"] == "perdido"]

            # ROI y P&L solo sobre picks ya resueltos
            bk = bk_actual or 1000
            # Usar stake_real para todos los cálculos — convierte al bankroll del usuario
            pnl_total      = sum((p["pnl"] or 0) * (bk / float(p.get("bankroll_engine") or p.get("bankroll_antes") or bk or 1)) for p in resueltos)
            invertido_res  = sum(stake_real(p, bk) for p in resueltos)
            invertido_pend = sum(stake_real(p, bk) for p in pendientes)
            # ROI sobre bankroll inicial (primer pick registrado)
            bankroll_inicial = None
            for p in sorted(picks, key=lambda x: x["fecha_colocado"] or ""):
                if p.get("bankroll_antes") is not None:
                    bankroll_inicial = float(p["bankroll_antes"])
                    break
            if not bankroll_inicial:
                bankroll_inicial = invertido_res if invertido_res > 0 else 1
            roi      = round(pnl_total / bankroll_inicial * 100, 2) if bankroll_inicial > 0 else 0
            win_rate = round(len(ganados) / len(resueltos) * 100, 1) if resueltos else 0

            def tipo_stats(lista):
                if not lista: return {"total":0,"ganados":0,"win_rate":0,"pnl":0,"roi":0}
                g   = [p for p in lista if p["estado"] in ("ganado","cashout")]
                pnl = sum((p["pnl"] or 0) * (bk / float(p.get("bankroll_engine") or p.get("bankroll_antes") or bk or 1)) for p in lista)
                inv = sum(stake_real(p, bk) for p in lista)
                return {"total":len(lista),"ganados":len(g),
                        "win_rate":round(len(g)/len(lista)*100,1),
                        "pnl":round(pnl,2),
                        "roi":round(pnl/inv*100,2) if inv>0 else 0}

            dep_stats = {}
            for p in resueltos:
                d = p["deporte"] or "Otro"
                dep_stats.setdefault(d, []).append(p)

            return {
                "total_colocados":  len(picks),
                "total_resueltos":  len(resueltos),
                "ganados":          len(ganados),
                "perdidos":         len(perdidos),
                "cashouts":         len([p for p in resueltos if p["estado"]=="cashout"]),
                "pendientes":       len(pendientes),
                "win_rate":         win_rate,
                "pnl_total":        round(pnl_total, 2),
                "invertido_resuelto": round(invertido_res, 2),   # solo resueltos
                "invertido_pendiente": round(invertido_pend, 2), # en juego
                "invertido_total":    round(invertido_res + invertido_pend, 2),
                "bankroll_disponible": round((bk_actual or 1000) - invertido_pend, 2),
                "roi":               roi,  # SOLO sobre resueltos
                "value_stats":      tipo_stats([p for p in resueltos if p["tipo"]=="value" and not p["es_gold"]]),
                "sure_stats":       tipo_stats([p for p in resueltos if p["tipo"]=="sure"]),
                "gold_stats":       tipo_stats([p for p in resueltos if p["es_gold"]]),
                "por_deporte":      {d: tipo_stats(v) for d,v in dep_stats.items()},
            }

        return {
            "bankroll":       float(user["bankroll"]) if user else 1000,
            "moneda":         user["moneda"] if user else "USD",
            "todo":           calcular(todos, float(user["bankroll"]) if user else 1000),
            "mes":            calcular(mes, float(user["bankroll"]) if user else 1000),
            "pendientes":     [serialize_row(dict(r)) for r in todos if dict(r).get("estado")=="pendiente"],
            "historial":      [serialize_row(dict(r)) for r in todos[:100]],
            "bankroll_hist":  [serialize_row(dict(r)) for r in bankroll_hist],
        }

async def guardar_pick_manual(user_id: int, data: dict) -> dict:
    """Guarda un pick colocado manualmente (retroactivo)."""
    import secrets as sec
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT bankroll FROM usuarios WHERE id=$1", user_id)
        bankroll_antes = float(user["bankroll"]) if user else 0

        estado      = data.get("estado", "pendiente")
        odds_real   = float(data.get("odds_real") or 0)
        stake       = float(data.get("stake_usd") or 0)
        es_cashout  = estado == "cashout"
        odds_co     = float(data.get("odds_cashout") or 0)

        if estado == "ganado":
            pnl = round(stake * (odds_real - 1), 2)
        elif estado == "perdido":
            pnl = -round(stake, 2)
        elif estado == "cashout" and odds_co > 0:
            pnl = round(stake * (odds_co - 1), 2)
        elif estado == "void":
            pnl = 0.0
        else:
            pnl = None

        bankroll_despues = round(bankroll_antes + (pnl or 0), 2) if pnl is not None else None

        pick_id = f"manual-{sec.token_hex(6)}"
        try:
            await conn.execute("""
                INSERT INTO historial_picks
                    (usuario_id, pick_id, evento, liga, deporte, mercado,
                     equipo_pick, odds_ref, odds_real, odds_cashout, stake_usd,
                     tipo, es_gold, estado, es_cashout, pnl,
                     bankroll_antes, bankroll_despues, fecha_colocado, fecha_resultado)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
            """,
                user_id, pick_id,
                data.get("evento",""),
                data.get("liga",""),
                data.get("deporte","Otro"),
                data.get("mercado","Manual"),
                data.get("equipo_pick",""),
                odds_real, odds_real,
                odds_co if es_cashout else None,
                stake,
                data.get("tipo","value"),
                bool(data.get("es_gold", False)),
                estado, es_cashout, pnl,
                bankroll_antes, bankroll_despues,
                data.get("fecha_colocado") or datetime.now(),
                datetime.now() if pnl is not None else None,
            )

            # Actualizar bankroll si hay resultado
            if bankroll_despues is not None:
                await conn.execute("UPDATE usuarios SET bankroll=$1 WHERE id=$2", bankroll_despues, user_id)
                await conn.execute(
                    "INSERT INTO bankroll_historial (usuario_id,monto,tipo,descripcion) VALUES ($1,$2,$3,$4)",
                    user_id, pnl, "pick_manual", f"{data.get('evento','')} — {estado}"
                )

            return {"ok": True, "pnl": pnl, "bankroll_nuevo": bankroll_despues}
        except Exception as e:
            log.error(f"Error guardar_pick_manual: {e}")
            return {"ok": False, "error": str(e)}


# ── Vencimientos ──────────────────────────────────────────────────────────────

async def activar_trial(user_id: int) -> dict:
    """Activa 7 días de prueba premium."""
    from datetime import datetime, timedelta
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT trial_usado FROM usuarios WHERE id=$1", user_id)
        if not user: return {"ok": False, "error": "Usuario no encontrado"}
        if user["trial_usado"]: return {"ok": False, "error": "Ya usaste el período de prueba"}
        venc = datetime.now() + timedelta(days=7)
        await conn.execute("UPDATE usuarios SET plan='premium', fecha_vencimiento=$1, trial_usado=TRUE WHERE id=$2", venc, user_id)
        return {"ok": True, "vencimiento": venc.isoformat()}

async def activar_premium(user_id: int, dias: int = 30) -> dict:
    """Activa premium por N días."""
    from datetime import datetime, timedelta
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT fecha_vencimiento FROM usuarios WHERE id=$1", user_id)
        if not user: return {"ok": False, "error": "Usuario no encontrado"}
        ahora = datetime.now()
        base  = user["fecha_vencimiento"] if user["fecha_vencimiento"] and user["fecha_vencimiento"].replace(tzinfo=None) > ahora else ahora
        nuevo = base.replace(tzinfo=None) + timedelta(days=dias)
        await conn.execute("UPDATE usuarios SET plan='premium', fecha_vencimiento=$1 WHERE id=$2", nuevo, user_id)
        return {"ok": True, "vencimiento": nuevo.isoformat(), "dias": dias}

async def verificar_vencimientos() -> dict:
    """Baja a free los que vencieron y retorna lista para notificar."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vencidos = await conn.fetch("""
            SELECT id, email, username, tg_chat_id, tg_activo, fecha_vencimiento
            FROM usuarios WHERE plan='premium'
            AND fecha_vencimiento IS NOT NULL AND fecha_vencimiento < NOW()
        """)
        if vencidos:
            await conn.execute("""
                UPDATE usuarios SET plan='free'
                WHERE plan='premium' AND fecha_vencimiento IS NOT NULL AND fecha_vencimiento < NOW()
            """)
        por_vencer = await conn.fetch("""
            SELECT id, email, username, tg_chat_id, tg_activo, fecha_vencimiento
            FROM usuarios WHERE plan='premium'
            AND fecha_vencimiento BETWEEN NOW() AND NOW() + INTERVAL '2 days'
        """)
        return {
            "vencidos":   [serialize_row(dict(r)) for r in vencidos],
            "por_vencer": [serialize_row(dict(r)) for r in por_vencer],
        }

# ── Admin ──────────────────────────────────────────────────────────────────────

async def crear_invitacion(plan="premium", max_usos=1, creado_por=None) -> str:
    codigo = secrets.token_urlsafe(8).upper()
    pool   = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO invitaciones (codigo,plan,max_usos,creado_por) VALUES ($1,$2,$3,$4)",
            codigo, plan, max_usos, creado_por
        )
    return codigo

async def get_invitaciones() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM invitaciones ORDER BY fecha_creacion DESC")
        return [serialize_row(dict(r)) for r in rows]

async def get_all_users() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id,email,username,plan,activo,bankroll,moneda,perfil_riesgo,tg_activo,fecha_registro,ultimo_login FROM usuarios ORDER BY fecha_registro DESC"
        )
        return [serialize_row(dict(r)) for r in rows]

async def set_user_plan(user_id, plan):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE usuarios SET plan=$1 WHERE id=$2", plan, user_id)

async def set_user_activo(user_id, activo):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE usuarios SET activo=$1 WHERE id=$2", activo, user_id)
