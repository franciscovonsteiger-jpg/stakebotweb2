import os, hashlib, secrets, logging
import asyncpg
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
                odds FLOAT,
                stake_usd FLOAT,
                tipo TEXT DEFAULT 'value',
                es_gold BOOLEAN DEFAULT FALSE,
                estado TEXT DEFAULT 'colocado',
                pnl FLOAT,
                fecha_colocado TIMESTAMPTZ DEFAULT NOW(),
                fecha_resultado TIMESTAMPTZ,
                UNIQUE(usuario_id, pick_id)
            );
        """)

        admin_email = os.getenv("ADMIN_EMAIL", "admin@stakebot.com")
        admin_pass  = os.getenv("ADMIN_PASSWORD", "admin1234")
        ph = hash_password(admin_pass)
        await conn.execute("""
            INSERT INTO usuarios (email, username, password_hash, plan)
            VALUES ($1, $2, $3, 'admin')
            ON CONFLICT DO NOTHING
        """, admin_email, "admin", ph)

    log.info("Base de datos inicializada")

def hash_password(password: str) -> str:
    salt = os.getenv("SECRET_KEY", "stakebot_salt_2025")
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

async def crear_usuario(email, username, password, codigo="") -> dict:
    ph   = hash_password(password)
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
    ph   = hash_password(password)
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
        return {"ok": True, "token": token, "user": dict(user)}

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
            f"UPDATE usuarios SET {','.join(campos)} WHERE id=${len(valores)}",
            *valores
        )
    return {"ok": True}

# ── Historial de picks ─────────────────────────────────────────────────────────

async def guardar_pick(user_id: int, pick: dict) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO historial_picks
                    (usuario_id, pick_id, evento, liga, deporte, mercado,
                     equipo_pick, odds, stake_usd, tipo, es_gold, estado)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'colocado')
                ON CONFLICT (usuario_id, pick_id) DO NOTHING
            """,
                user_id,
                pick.get("id",""),
                pick.get("evento",""),
                pick.get("liga",""),
                pick.get("deporte",""),
                pick.get("mercado",""),
                pick.get("equipo_pick",""),
                float(pick.get("odds_ref") or pick.get("odds_stake") or 0),
                float(pick.get("stake_usd") or 0),
                pick.get("tipo","value"),
                bool(pick.get("es_gold", False)),
            )
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

async def actualizar_resultado(pick_db_id: int, user_id: int, estado: str) -> dict:
    """Actualiza el resultado de un pick (ganado/perdido/void)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM historial_picks WHERE id=$1 AND usuario_id=$2",
            pick_db_id, user_id
        )
        if not row:
            return {"ok": False, "error": "Pick no encontrado"}

        stake = row["stake_usd"] or 0
        odds  = row["odds"] or 0

        if estado == "ganado":
            pnl = round(stake * (odds - 1), 2)
        elif estado == "perdido":
            pnl = -round(stake, 2)
        else:  # void
            pnl = 0.0

        await conn.execute("""
            UPDATE historial_picks
            SET estado=$1, pnl=$2, fecha_resultado=NOW()
            WHERE id=$3
        """, estado, pnl, pick_db_id)

        return {"ok": True, "pnl": pnl}

async def get_historial(user_id: int, limite: int = 100) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM historial_picks
            WHERE usuario_id=$1
            ORDER BY fecha_colocado DESC
            LIMIT $2
        """, user_id, limite)
        return [dict(r) for r in rows]

async def get_estadisticas(user_id: int) -> dict:
    """Calcula estadísticas reales del usuario basadas en su historial."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Historial completo
        todos = await conn.fetch(
            "SELECT * FROM historial_picks WHERE usuario_id=$1 ORDER BY fecha_colocado DESC",
            user_id
        )
        # Últimos 30 días
        mes = await conn.fetch("""
            SELECT * FROM historial_picks
            WHERE usuario_id=$1
            AND fecha_colocado >= NOW() - INTERVAL '30 days'
            ORDER BY fecha_colocado DESC
        """, user_id)

        def calcular_stats(picks):
            resueltos = [p for p in picks if p["estado"] in ("ganado","perdido","void")]
            ganados   = [p for p in resueltos if p["estado"] == "ganado"]
            perdidos  = [p for p in resueltos if p["estado"] == "perdido"]
            pnl_total = sum(p["pnl"] or 0 for p in resueltos)
            invertido = sum(p["stake_usd"] or 0 for p in resueltos)
            roi       = round(pnl_total / invertido * 100, 2) if invertido > 0 else 0
            win_rate  = round(len(ganados) / len(resueltos) * 100, 1) if resueltos else 0

            # Por tipo
            value_r = [p for p in resueltos if p["tipo"] == "value"]
            sure_r  = [p for p in resueltos if p["tipo"] == "sure"]
            gold_r  = [p for p in resueltos if p["es_gold"]]

            def tipo_stats(lista):
                if not lista: return {"total":0,"ganados":0,"win_rate":0,"pnl":0,"roi":0}
                g = [p for p in lista if p["estado"]=="ganado"]
                pnl = sum(p["pnl"] or 0 for p in lista)
                inv = sum(p["stake_usd"] or 0 for p in lista)
                return {
                    "total":    len(lista),
                    "ganados":  len(g),
                    "win_rate": round(len(g)/len(lista)*100,1),
                    "pnl":      round(pnl,2),
                    "roi":      round(pnl/inv*100,2) if inv>0 else 0,
                }

            # Por deporte
            deportes = {}
            for p in resueltos:
                dep = p["deporte"] or "Otro"
                if dep not in deportes:
                    deportes[dep] = []
                deportes[dep].append(p)
            dep_stats = {d: tipo_stats(v) for d,v in deportes.items()}

            return {
                "total_colocados": len(picks),
                "total_resueltos": len(resueltos),
                "ganados":         len(ganados),
                "perdidos":        len(perdidos),
                "pendientes":      len(picks) - len(resueltos),
                "win_rate":        win_rate,
                "pnl_total":       round(pnl_total, 2),
                "invertido_total": round(invertido, 2),
                "roi":             roi,
                "value_stats":     tipo_stats(value_r),
                "sure_stats":      tipo_stats(sure_r),
                "gold_stats":      tipo_stats(gold_r),
                "por_deporte":     dep_stats,
            }

        user = await conn.fetchrow("SELECT bankroll FROM usuarios WHERE id=$1", user_id)
        bankroll = float(user["bankroll"]) if user else 1000

        return {
            "bankroll":    bankroll,
            "todo":        calcular_stats(todos),
            "mes":         calcular_stats(mes),
            "historial":   [dict(r) for r in todos[:50]],
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
        return [dict(r) for r in rows]

async def get_all_users() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id,email,username,plan,activo,bankroll,moneda,perfil_riesgo,tg_activo,fecha_registro,ultimo_login FROM usuarios ORDER BY fecha_registro DESC"
        )
        return [dict(r) for r in rows]

async def set_user_plan(user_id, plan):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE usuarios SET plan=$1 WHERE id=$2", plan, user_id)

async def set_user_activo(user_id, activo):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE usuarios SET activo=$1 WHERE id=$2", activo, user_id)
