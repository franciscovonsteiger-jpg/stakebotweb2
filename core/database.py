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
        """)

        admin_email = os.getenv("ADMIN_EMAIL", "admin@stakebot.com")
        admin_pass  = os.getenv("ADMIN_PASSWORD", "admin1234")
        exists = await conn.fetchval("SELECT id FROM usuarios WHERE email=$1", admin_email)
        if not exists:
            ph = hash_password(admin_pass)
            await conn.execute(
                "INSERT INTO usuarios (email,username,password_hash,plan) VALUES ($1,$2,$3,'admin')",
                admin_email, "admin", ph
            )
            log.info(f"Admin creado: {admin_email}")
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
                "SELECT * FROM invitaciones WHERE codigo=$1 AND usado=FALSE AND usos_actuales<max_usos",
                codigo.upper()
            )
            if not inv:
                return {"ok": False, "error": "Código de invitación inválido o ya usado"}
            plan = inv["plan"]
        try:
            row = await conn.fetchrow(
                "INSERT INTO usuarios (email,username,password_hash,plan,codigo_invitacion) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                email.lower(), username, ph, plan, codigo
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
            email.lower(), ph
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
    for i,(k,v) in enumerate([(k,v) for k,v in data.items() if k in permitidos], 1):
        campos.append(f"{k}=${i}")
        valores.append(v)
    if not campos:
        return {"ok": False, "error": "Sin campos válidos"}
    valores.append(user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE usuarios SET {','.join(campos)} WHERE id=${len(valores)}", *valores)
    return {"ok": True}

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
