"""
Base de datos PostgreSQL — usuarios, planes, invitaciones
"""
import os, hashlib, secrets, logging
from datetime import datetime, timezone
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("stakebot.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Crea las tablas si no existen."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usuarios (
                    id          SERIAL PRIMARY KEY,
                    email       TEXT UNIQUE NOT NULL,
                    username    TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    plan        TEXT DEFAULT 'free',  -- 'free' | 'premium' | 'admin'
                    activo      BOOLEAN DEFAULT TRUE,
                    bankroll    FLOAT DEFAULT 1000,
                    moneda      TEXT DEFAULT 'USD',
                    perfil_riesgo TEXT DEFAULT 'inteligente', -- conservador|inteligente|profesional
                    tg_chat_id  TEXT DEFAULT '',
                    tg_activo   BOOLEAN DEFAULT FALSE,
                    codigo_invitacion TEXT DEFAULT '',
                    fecha_registro TIMESTAMPTZ DEFAULT NOW(),
                    fecha_vencimiento TIMESTAMPTZ,
                    ultimo_login TIMESTAMPTZ
                );

                CREATE TABLE IF NOT EXISTS invitaciones (
                    id          SERIAL PRIMARY KEY,
                    codigo      TEXT UNIQUE NOT NULL,
                    plan        TEXT DEFAULT 'premium',
                    usado       BOOLEAN DEFAULT FALSE,
                    usado_por   INTEGER REFERENCES usuarios(id),
                    creado_por  INTEGER,
                    fecha_creacion TIMESTAMPTZ DEFAULT NOW(),
                    fecha_uso   TIMESTAMPTZ,
                    max_usos    INTEGER DEFAULT 1,
                    usos_actuales INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sesiones (
                    id          SERIAL PRIMARY KEY,
                    usuario_id  INTEGER REFERENCES usuarios(id),
                    token       TEXT UNIQUE NOT NULL,
                    fecha_creacion TIMESTAMPTZ DEFAULT NOW(),
                    fecha_expiracion TIMESTAMPTZ,
                    activa      BOOLEAN DEFAULT TRUE
                );

                CREATE TABLE IF NOT EXISTS historial_picks (
                    id          SERIAL PRIMARY KEY,
                    usuario_id  INTEGER REFERENCES usuarios(id),
                    pick_id     TEXT NOT NULL,
                    evento      TEXT,
                    equipo_pick TEXT,
                    odds        FLOAT,
                    stake_usd   FLOAT,
                    es_gold     BOOLEAN DEFAULT FALSE,
                    estado      TEXT DEFAULT 'colocado', -- colocado|ganado|perdido
                    pnl         FLOAT,
                    fecha       TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            conn.commit()

            # Crear admin por defecto si no existe
            admin_email = os.getenv("ADMIN_EMAIL", "admin@stakebot.com")
            admin_pass  = os.getenv("ADMIN_PASSWORD", "admin1234")
            cur.execute("SELECT id FROM usuarios WHERE email = %s", (admin_email,))
            if not cur.fetchone():
                ph = hash_password(admin_pass)
                cur.execute("""
                    INSERT INTO usuarios (email, username, password_hash, plan)
                    VALUES (%s, %s, %s, 'admin')
                """, (admin_email, "admin", ph))
                conn.commit()
                log.info(f"Admin creado: {admin_email}")

    log.info("Base de datos inicializada")

# ── Auth ───────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.getenv("SECRET_KEY", "stakebot_salt_2025")
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

def crear_usuario(email: str, username: str, password: str,
                  codigo: str = "", plan: str = "free") -> dict:
    ph = hash_password(password)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Validar código de invitación si se requiere plan premium
            if codigo:
                cur.execute("""
                    SELECT * FROM invitaciones
                    WHERE codigo = %s AND usado = FALSE AND usos_actuales < max_usos
                """, (codigo,))
                inv = cur.fetchone()
                if not inv:
                    return {"ok": False, "error": "Código de invitación inválido o ya usado"}
                plan = inv["plan"]

            try:
                cur.execute("""
                    INSERT INTO usuarios
                        (email, username, password_hash, plan, codigo_invitacion)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, (email.lower(), username, ph, plan, codigo))
                user_id = cur.fetchone()["id"]

                # Marcar invitación como usada
                if codigo:
                    cur.execute("""
                        UPDATE invitaciones
                        SET usos_actuales = usos_actuales + 1,
                            usado = (usos_actuales + 1 >= max_usos),
                            usado_por = %s, fecha_uso = NOW()
                        WHERE codigo = %s
                    """, (user_id, codigo))

                conn.commit()
                return {"ok": True, "user_id": user_id, "plan": plan}
            except psycopg2.errors.UniqueViolation:
                return {"ok": False, "error": "Email o usuario ya registrado"}

def login(email: str, password: str) -> dict:
    ph = hash_password(password)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM usuarios
                WHERE email = %s AND password_hash = %s AND activo = TRUE
            """, (email.lower(), ph))
            user = cur.fetchone()
            if not user:
                return {"ok": False, "error": "Email o contraseña incorrectos"}

            # Crear sesión
            token = secrets.token_urlsafe(32)
            cur.execute("""
                INSERT INTO sesiones (usuario_id, token, fecha_expiracion)
                VALUES (%s, %s, NOW() + INTERVAL '30 days')
            """, (user["id"], token))
            cur.execute("UPDATE usuarios SET ultimo_login = NOW() WHERE id = %s", (user["id"],))
            conn.commit()
            return {"ok": True, "token": token, "user": dict(user)}

def get_user_by_token(token: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.* FROM usuarios u
                JOIN sesiones s ON s.usuario_id = u.id
                WHERE s.token = %s
                  AND s.activa = TRUE
                  AND s.fecha_expiracion > NOW()
                  AND u.activo = TRUE
            """, (token,))
            row = cur.fetchone()
            return dict(row) if row else None

def logout(token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sesiones SET activa = FALSE WHERE token = %s", (token,))
            conn.commit()

def update_perfil(user_id: int, data: dict) -> dict:
    campos = []
    valores = []
    permitidos = ["bankroll", "moneda", "perfil_riesgo", "tg_chat_id", "tg_activo", "username"]
    for k, v in data.items():
        if k in permitidos:
            campos.append(f"{k} = %s")
            valores.append(v)
    if not campos:
        return {"ok": False, "error": "Sin campos válidos"}
    valores.append(user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s", valores)
            conn.commit()
    return {"ok": True}

# ── Invitaciones ───────────────────────────────────────────────────────────────

def crear_invitacion(plan: str = "premium", max_usos: int = 1,
                     creado_por: int = None) -> str:
    codigo = secrets.token_urlsafe(8).upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invitaciones (codigo, plan, max_usos, creado_por)
                VALUES (%s, %s, %s, %s)
            """, (codigo, plan, max_usos, creado_por))
            conn.commit()
    return codigo

def get_invitaciones() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM invitaciones ORDER BY fecha_creacion DESC")
            return [dict(r) for r in cur.fetchall()]

# ── Admin ──────────────────────────────────────────────────────────────────────

def get_all_users() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, email, username, plan, activo, bankroll,
                       moneda, perfil_riesgo, tg_activo, fecha_registro, ultimo_login
                FROM usuarios ORDER BY fecha_registro DESC
            """)
            return [dict(r) for r in cur.fetchall()]

def set_user_plan(user_id: int, plan: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET plan = %s WHERE id = %s", (plan, user_id))
            conn.commit()

def set_user_activo(user_id: int, activo: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET activo = %s WHERE id = %s", (activo, user_id))
            conn.commit()

# ── Historial picks ────────────────────────────────────────────────────────────

def guardar_pick(user_id: int, pick: dict, estado: str = "colocado"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO historial_picks
                    (usuario_id, pick_id, evento, equipo_pick, odds, stake_usd, es_gold, estado)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                user_id, pick["id"], pick["evento"], pick["equipo_pick"],
                pick["odds_stake"], pick["stake_usd"], pick.get("es_gold", False), estado
            ))
            conn.commit()

def get_historial(user_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM historial_picks
                WHERE usuario_id = %s ORDER BY fecha DESC LIMIT 100
            """, (user_id,))
            return [dict(r) for r in cur.fetchall()]

def update_resultado(pick_db_id: int, estado: str, pnl: float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE historial_picks SET estado = %s, pnl = %s WHERE id = %s
            """, (estado, pnl, pick_db_id))
            conn.commit()
