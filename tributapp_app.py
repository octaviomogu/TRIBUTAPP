from __future__ import annotations

import csv
import io
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, flash, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


# -----------------------------
# Paths
# -----------------------------
def get_base_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


BASE_DIR = get_base_dir()
DB_PATH = BASE_DIR / "tributapp.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "tributapp-dev-secret-change-me"


# -----------------------------
# Database helpers
# -----------------------------
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn



def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS empresas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            rut TEXT NOT NULL DEFAULT '00000000-0',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS empresa_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'owner',
            UNIQUE(empresa_id, user_id),
            FOREIGN KEY (empresa_id) REFERENCES empresas(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS archivos_importados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            nombre_archivo TEXT NOT NULL,
            tipo_archivo TEXT NOT NULL,
            periodo TEXT,
            total_filas INTEGER NOT NULL DEFAULT 0,
            fecha_importacion TEXT NOT NULL,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS compras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            periodo TEXT,
            tipo_doc TEXT,
            folio TEXT,
            fecha TEXT,
            rut TEXT,
            razon_social TEXT,
            exento REAL DEFAULT 0,
            neto REAL DEFAULT 0,
            iva REAL DEFAULT 0,
            total REAL DEFAULT 0,
            clasificacion TEXT DEFAULT 'Sin clasificar',
            estado_revision TEXT DEFAULT 'Pendiente',
            observacion TEXT DEFAULT '',
            archivo_id INTEGER,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id),
            FOREIGN KEY (archivo_id) REFERENCES archivos_importados(id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ventas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL,
            periodo TEXT,
            tipo_doc TEXT,
            folio TEXT,
            fecha TEXT,
            rut TEXT,
            razon_social TEXT,
            exento REAL DEFAULT 0,
            neto REAL DEFAULT 0,
            iva REAL DEFAULT 0,
            total REAL DEFAULT 0,
            archivo_id INTEGER,
            FOREIGN KEY (empresa_id) REFERENCES empresas(id),
            FOREIGN KEY (archivo_id) REFERENCES archivos_importados(id)
        )
        """
    )

    conn.commit()
    conn.close()


# -----------------------------
# Auth helpers
# -----------------------------
def create_user(name: str, email: str, password: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
        (name.strip(), email.strip().lower(), generate_password_hash(password)),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return int(user_id)



def get_user_by_email(email: str) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    conn.close()
    return row



def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row



def get_current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(int(user_id))



def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if get_current_user() is None:
            flash("Debes iniciar sesión para continuar.")
            return redirect(url_for("login_view"))
        return view_func(*args, **kwargs)

    return wrapped


# -----------------------------
# CSV helpers
# -----------------------------
def normalize_name(name: str) -> str:
    text = (name or "").replace("\ufeff", "").strip().lower()
    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ñ": "n",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("_", " ").replace(".", " ")
    return " ".join(text.split())


COLUMN_ALIASES = {
    "tipo_doc": {"tipo doc", "tipo documento", "tipo doc."},
    "folio": {"folio"},
    "fecha": {"fecha", "fecha docto", "fecha documento", "fecha doc"},
    "rut": {"rut", "rut proveedor", "rut cliente", "rut contraparte", "rut emisor", "rut receptor"},
    "razon_social": {"razon social", "nombre o razon social", "proveedor", "cliente"},
    "exento": {"monto exento", "exento"},
    "neto": {"monto neto", "neto"},
    "iva": {"monto iva", "iva", "monto iva recuperable", "iva recuperable"},
    "total": {"monto total", "total"},
}

REQUIRED_COLUMNS = ["tipo_doc", "folio", "fecha", "rut", "razon_social", "neto", "iva", "total"]



def map_columns(headers: list[str]) -> dict[str, str]:
    normalized_headers = {normalize_name(h): h for h in headers if h not in (None, "")}
    mapping: dict[str, str] = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized_headers:
                mapping[target] = normalized_headers[alias]
                break
    return mapping



def parse_decimal(value: Any) -> float:
    if value is None:
        return 0.0

    text = str(value).strip()
    if text == "":
        return 0.0

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        if text.count(".") > 1:
            text = text.replace(".", "")
        elif text.count(".") == 1:
            _, right = text.split(".")
            if not (1 <= len(right) <= 2):
                text = text.replace(".", "")

    try:
        return float(text)
    except ValueError:
        return 0.0



def parse_percentage(value: Any) -> float:
    numeric = parse_decimal(value)
    return numeric / 100 if numeric > 1 else numeric



def detect_tipo_archivo(filename: str) -> str:
    name = filename.upper()
    if "COMPRA" in name:
        return "compra"
    if "VENTA" in name:
        return "venta"
    return "desconocido"



def detect_periodo(filename: str) -> str:
    stem = Path(filename).stem
    parts = stem.split("_")
    last = parts[-1] if parts else ""
    if len(last) == 6 and last.isdigit():
        return last
    return datetime.now().strftime("%Y%m")


# -----------------------------
# Empresa helpers
# -----------------------------
def get_empresas(user_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_connection()
    if user_id is None:
        rows = conn.execute("SELECT * FROM empresas ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            """
            SELECT e.*
            FROM empresas e
            JOIN empresa_users eu ON eu.empresa_id = e.id
            WHERE eu.user_id = ?
            ORDER BY e.id DESC
            """,
            (user_id,),
        ).fetchall()
    conn.close()
    return rows



def get_empresa_by_id(empresa_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM empresas WHERE id = ?", (empresa_id,)).fetchone()
    conn.close()
    return row



def user_has_access_to_empresa(user_id: int, empresa_id: int) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM empresa_users WHERE user_id = ? AND empresa_id = ?",
        (user_id, empresa_id),
    ).fetchone()
    conn.close()
    return row is not None



def create_empresa(nombre: str, rut: str, owner_user_id: int | None = None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO empresas (nombre, rut) VALUES (?, ?)", (nombre.strip(), rut.strip() or "00000000-0"))
    empresa_id = cur.lastrowid
    if owner_user_id is not None:
        cur.execute(
            "INSERT INTO empresa_users (empresa_id, user_id, role) VALUES (?, ?, ?)",
            (empresa_id, owner_user_id, "owner"),
        )
    conn.commit()
    conn.close()
    return int(empresa_id)



def get_default_empresa_id_for_user(user_id: int) -> int:
    empresas = get_empresas(user_id=user_id)
    if empresas:
        return int(empresas[-1]["id"])
    return create_empresa("Mi Empresa", "00000000-0", owner_user_id=user_id)



def get_active_empresa_id() -> int:
    user = get_current_user()
    if user is None:
        raise ValueError("No hay usuario autenticado")

    empresa_id = session.get("empresa_activa_id")
    if empresa_id and user_has_access_to_empresa(int(user["id"]), int(empresa_id)):
        return int(empresa_id)

    default_id = get_default_empresa_id_for_user(int(user["id"]))
    session["empresa_activa_id"] = default_id
    return default_id



def get_active_empresa() -> sqlite3.Row:
    empresa = get_empresa_by_id(get_active_empresa_id())
    if empresa is None:
        raise ValueError("No encontré la empresa activa")
    return empresa


# -----------------------------
# Import helpers
# -----------------------------
def save_import_record(empresa_id: int, nombre_archivo: str, tipo_archivo: str, periodo: str, total_filas: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO archivos_importados (empresa_id, nombre_archivo, tipo_archivo, periodo, total_filas, fecha_importacion)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (empresa_id, nombre_archivo, tipo_archivo, periodo, total_filas, datetime.now().isoformat()),
    )
    conn.commit()
    inserted_id = cur.lastrowid
    conn.close()
    return int(inserted_id)



def insert_rows(tipo_archivo: str, empresa_id: int, periodo: str, rows: list[dict[str, Any]], archivo_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    inserted = 0

    for row in rows:
        if tipo_archivo == "compra":
            cur.execute(
                """
                INSERT INTO compras (
                    empresa_id, periodo, tipo_doc, folio, fecha, rut, razon_social,
                    exento, neto, iva, total, clasificacion, estado_revision, observacion, archivo_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    empresa_id,
                    periodo,
                    row["tipo_doc"],
                    row["folio"],
                    row["fecha"],
                    row["rut"],
                    row["razon_social"],
                    row["exento"],
                    row["neto"],
                    row["iva"],
                    row["total"],
                    "Sin clasificar",
                    "Pendiente",
                    "",
                    archivo_id,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO ventas (
                    empresa_id, periodo, tipo_doc, folio, fecha, rut, razon_social,
                    exento, neto, iva, total, archivo_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    empresa_id,
                    periodo,
                    row["tipo_doc"],
                    row["folio"],
                    row["fecha"],
                    row["rut"],
                    row["razon_social"],
                    row["exento"],
                    row["neto"],
                    row["iva"],
                    row["total"],
                    archivo_id,
                ),
            )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted



def process_csv(file_storage, empresa_id: int) -> tuple[str, str, int]:
    filename = file_storage.filename or "archivo.csv"
    tipo = detect_tipo_archivo(filename)
    if tipo == "desconocido":
        raise ValueError("No pude detectar si el archivo es de compras o ventas.")

    periodo = detect_periodo(filename)
    text = file_storage.read().decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if not reader.fieldnames:
        raise ValueError("El CSV no tiene encabezados válidos.")

    reader.fieldnames = [h for h in reader.fieldnames if h not in (None, "")]
    mapping = map_columns(reader.fieldnames)
    missing = [col for col in REQUIRED_COLUMNS if col not in mapping]
    if missing:
        raise ValueError(f"Faltan columnas requeridas: {', '.join(missing)}")

    normalized_rows: list[dict[str, Any]] = []
    for raw in reader:
        if not any(str(v).strip() for v in raw.values() if v is not None):
            continue
        exento_key = mapping.get("exento")
        normalized_rows.append(
            {
                "tipo_doc": str(raw.get(mapping["tipo_doc"], "")).strip(),
                "folio": str(raw.get(mapping["folio"], "")).strip(),
                "fecha": str(raw.get(mapping["fecha"], "")).strip(),
                "rut": str(raw.get(mapping["rut"], "")).strip(),
                "razon_social": str(raw.get(mapping["razon_social"], "")).strip(),
                "exento": parse_decimal(raw.get(exento_key, 0) if exento_key else 0),
                "neto": parse_decimal(raw.get(mapping["neto"], 0)),
                "iva": parse_decimal(raw.get(mapping["iva"], 0)),
                "total": parse_decimal(raw.get(mapping["total"], 0)),
            }
        )

    archivo_id = save_import_record(empresa_id, filename, tipo, periodo, len(normalized_rows))
    inserted = insert_rows(tipo, empresa_id, periodo, normalized_rows, archivo_id)
    return tipo, periodo, inserted


# -----------------------------
# Calculadoras
# -----------------------------
def calcular_honorarios_desde_bruto(bruto: float, tasa: float = 0.1525) -> dict[str, float]:
    bruto_redondeado = round(bruto)
    retencion = round(bruto_redondeado * tasa)
    liquido = bruto_redondeado - retencion
    return {"bruto": float(bruto_redondeado), "retencion": float(retencion), "liquido": float(liquido)}



def calcular_honorarios_desde_liquido(liquido: float, tasa: float = 0.1525) -> dict[str, float]:
    if tasa >= 1:
        raise ValueError("La tasa no puede ser mayor o igual a 1")
    liquido_redondeado = round(liquido)
    bruto = round(liquido_redondeado / (1 - tasa))
    retencion = bruto - liquido_redondeado
    return {"bruto": float(bruto), "retencion": float(retencion), "liquido": float(liquido_redondeado)}



def calcular_iva(ventas_netas: float, compras_netas: float, tasa: float = 0.19) -> dict[str, float]:
    iva_debito = round(ventas_netas * tasa, 2)
    iva_credito = round(compras_netas * tasa, 2)
    iva_pagar = round(iva_debito - iva_credito, 2)
    return {"iva_debito": iva_debito, "iva_credito": iva_credito, "iva_pagar": iva_pagar}


# -----------------------------
# Query helpers
# -----------------------------
def get_dashboard_metrics(empresa_id: int) -> dict[str, Any]:
    conn = get_connection()
    ventas = conn.execute("SELECT COALESCE(SUM(neto), 0) AS total FROM ventas WHERE empresa_id = ?", (empresa_id,)).fetchone()["total"]
    compras = conn.execute("SELECT COALESCE(SUM(neto), 0) AS total FROM compras WHERE empresa_id = ?", (empresa_id,)).fetchone()["total"]
    iva_debito = conn.execute("SELECT COALESCE(SUM(iva), 0) AS total FROM ventas WHERE empresa_id = ?", (empresa_id,)).fetchone()["total"]
    iva_credito = conn.execute("SELECT COALESCE(SUM(iva), 0) AS total FROM compras WHERE empresa_id = ?", (empresa_id,)).fetchone()["total"]
    conn.close()
    return {
        "ventas_netas": ventas,
        "compras_netas": compras,
        "iva_debito": iva_debito,
        "iva_credito": iva_credito,
        "iva_pagar": iva_debito - iva_credito,
    }



def get_recent_imports(limit: int = 10, empresa_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_connection()
    if empresa_id is None:
        rows = conn.execute(
            "SELECT ai.*, e.nombre AS empresa_nombre FROM archivos_importados ai JOIN empresas e ON e.id = ai.empresa_id ORDER BY ai.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ai.*, e.nombre AS empresa_nombre FROM archivos_importados ai JOIN empresas e ON e.id = ai.empresa_id WHERE ai.empresa_id = ? ORDER BY ai.id DESC LIMIT ?",
            (empresa_id, limit),
        ).fetchall()
    conn.close()
    return rows



def get_compras(limit: int = 100, empresa_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_connection()
    if empresa_id is None:
        rows = conn.execute("SELECT * FROM compras ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM compras WHERE empresa_id = ? ORDER BY id DESC LIMIT ?", (empresa_id, limit)).fetchall()
    conn.close()
    return rows



def get_reportes(empresa_id: int | None = None) -> dict[str, Any]:
    conn = get_connection()
    if empresa_id is None:
        ventas_por_periodo = list(
            conn.execute(
                "SELECT periodo, COALESCE(SUM(neto), 0) AS neto, COALESCE(SUM(iva), 0) AS iva, COALESCE(SUM(total), 0) AS total FROM ventas GROUP BY periodo ORDER BY periodo DESC"
            ).fetchall()
        )
        compras_por_periodo = list(
            conn.execute(
                "SELECT periodo, COALESCE(SUM(neto), 0) AS neto, COALESCE(SUM(iva), 0) AS iva, COALESCE(SUM(total), 0) AS total FROM compras GROUP BY periodo ORDER BY periodo DESC"
            ).fetchall()
        )
        top_proveedores = list(
            conn.execute(
                "SELECT razon_social, COALESCE(SUM(total), 0) AS total FROM compras GROUP BY razon_social ORDER BY total DESC LIMIT 10"
            ).fetchall()
        )
        top_clientes = list(
            conn.execute(
                "SELECT razon_social, COALESCE(SUM(total), 0) AS total FROM ventas GROUP BY razon_social ORDER BY total DESC LIMIT 10"
            ).fetchall()
        )
    else:
        ventas_por_periodo = list(
            conn.execute(
                "SELECT periodo, COALESCE(SUM(neto), 0) AS neto, COALESCE(SUM(iva), 0) AS iva, COALESCE(SUM(total), 0) AS total FROM ventas WHERE empresa_id = ? GROUP BY periodo ORDER BY periodo DESC",
                (empresa_id,),
            ).fetchall()
        )
        compras_por_periodo = list(
            conn.execute(
                "SELECT periodo, COALESCE(SUM(neto), 0) AS neto, COALESCE(SUM(iva), 0) AS iva, COALESCE(SUM(total), 0) AS total FROM compras WHERE empresa_id = ? GROUP BY periodo ORDER BY periodo DESC",
                (empresa_id,),
            ).fetchall()
        )
        top_proveedores = list(
            conn.execute(
                "SELECT razon_social, COALESCE(SUM(total), 0) AS total FROM compras WHERE empresa_id = ? GROUP BY razon_social ORDER BY total DESC LIMIT 10",
                (empresa_id,),
            ).fetchall()
        )
        top_clientes = list(
            conn.execute(
                "SELECT razon_social, COALESCE(SUM(total), 0) AS total FROM ventas WHERE empresa_id = ? GROUP BY razon_social ORDER BY total DESC LIMIT 10",
                (empresa_id,),
            ).fetchall()
        )
    conn.close()
    return {
        "ventas_por_periodo": ventas_por_periodo,
        "compras_por_periodo": compras_por_periodo,
        "top_proveedores": top_proveedores,
        "top_clientes": top_clientes,
    }


# -----------------------------
# Data helpers
# -----------------------------
def reset_database_for_empresa(empresa_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM compras WHERE empresa_id = ?", (empresa_id,))
    conn.execute("DELETE FROM ventas WHERE empresa_id = ?", (empresa_id,))
    conn.execute("DELETE FROM archivos_importados WHERE empresa_id = ?", (empresa_id,))
    conn.commit()
    conn.close()



def clp(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    return "$" + f"{number:,.0f}".replace(",", ".")


app.jinja_env.filters["clp"] = clp


BASE_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} - TributApp</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --primary: #0f766e;
      --dark: #0b1320;
      --border: #dbe3ea;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Arial, Helvetica, sans-serif; background: var(--bg); color: var(--text); }
    .nav { background: var(--dark); color: white; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
    .brand { font-weight: bold; }
    .nav-links a { color: white; text-decoration: none; margin-right: 16px; }
    .container { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .hero { background: linear-gradient(135deg, #0f766e, #0b1320); color: white; border-radius: 18px; padding: 28px; margin-bottom: 18px; }
    .card { background: white; border: 1px solid var(--border); border-radius: 16px; padding: 18px; margin-bottom: 16px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.04); }
    .grid { display: grid; gap: 16px; }
    .cards { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
    .metric { font-size: 28px; font-weight: bold; margin-top: 6px; }
    .btn { display: inline-block; background: var(--primary); color: white; padding: 10px 14px; text-decoration: none; border-radius: 10px; border: none; cursor: pointer; }
    .btn-secondary { background: #e5eef5; color: #0b1320; }
    .muted { color: var(--muted); }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; }
    th { background: #f9fafb; }
    input, select, textarea { width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 10px; }
    .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .flash { background: #ecfeff; border: 1px solid #99f6e4; padding: 12px; border-radius: 10px; margin-bottom: 12px; }
    .pill { display: inline-block; padding: 6px 10px; border-radius: 999px; background: #e6fffb; color: #115e59; font-size: 12px; }
    .empresa-switcher { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .empresa-switcher form { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:0; }
    .empresa-switcher select { min-width: 240px; }
    @media (max-width: 700px) { .form-row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="nav">
    <div class="brand">TributApp · SaaS MVP</div>
    <div class="nav-links">
      {% if current_user %}
      <a href="{{ url_for('index') }}">Dashboard</a>
      <a href="{{ url_for('empresas_view') }}">Empresas</a>
      <a href="{{ url_for('cargar_archivos') }}">Cargar archivos</a>
      <a href="{{ url_for('compras_view') }}">RCV Compras</a>
      <a href="{{ url_for('reportes_view') }}">Reportes</a>
      <a href="{{ url_for('calculadora_honorarios') }}">Honorarios</a>
      <a href="{{ url_for('calculadora_iva_view') }}">IVA</a>
      <a href="{{ url_for('logout_view') }}">Salir</a>
      {% else %}
      <a href="{{ url_for('login_view') }}">Ingresar</a>
      <a href="{{ url_for('register_view') }}">Crear cuenta</a>
      {% endif %}
    </div>
  </div>
  <div class="container">
    {% if current_user and empresas and empresa_activa %}
    <div class="card empresa-switcher">
      <span class="pill">Usuario: {{ current_user['name'] }}</span>
      <span class="pill">Empresa activa: {{ empresa_activa['nombre'] }} · {{ empresa_activa['rut'] }}</span>
      <form method="post" action="{{ url_for('set_active_empresa') }}">
        <select name="empresa_id">
          {% for emp in empresas %}
            <option value="{{ emp['id'] }}" {% if emp['id'] == empresa_activa['id'] %}selected{% endif %}>{{ emp['nombre'] }} · {{ emp['rut'] }}</option>
          {% endfor %}
        </select>
        <button class="btn btn-secondary" type="submit">Cambiar empresa</button>
      </form>
    </div>
    {% endif %}
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for message in messages %}
          <div class="flash">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    {{ body|safe }}
  </div>
</body>
</html>
"""


@app.context_processor
def inject_global_template_vars() -> dict[str, Any]:
    try:
        current_user = get_current_user()
        if current_user is None:
            return {"current_user": None, "empresas": [], "empresa_activa": None}
        empresas = get_empresas(user_id=int(current_user["id"]))
        empresa_activa = get_active_empresa()
        return {"current_user": current_user, "empresas": empresas, "empresa_activa": empresa_activa}
    except Exception:
        return {"current_user": None, "empresas": [], "empresa_activa": None}


# -----------------------------
# Routes
# -----------------------------
@app.route("/register", methods=["GET", "POST"])
def register_view():
    if get_current_user() is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or not password:
            flash("Completa todos los campos.")
            return redirect(url_for("register_view"))
        if get_user_by_email(email) is not None:
            flash("Ya existe un usuario con ese correo.")
            return redirect(url_for("register_view"))

        user_id = create_user(name, email, password)
        empresa_id = create_empresa(f"Empresa de {name}", "00000000-0", owner_user_id=user_id)
        session["user_id"] = user_id
        session["empresa_activa_id"] = empresa_id
        flash("Cuenta creada correctamente. Bienvenido a TributApp.")
        return redirect(url_for("index"))

    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Crear cuenta</h1><p style="margin:0;">Primer paso para convertir TributApp en SaaS real.</p></div>
        <div class="card">
          <form method="post">
            <p><label>Nombre</label><input type="text" name="name"></p>
            <p><label>Correo</label><input type="email" name="email"></p>
            <p><label>Contraseña</label><input type="password" name="password"></p>
            <p><button class="btn" type="submit">Crear cuenta</button></p>
          </form>
        </div>
        """
    )
    return render_template_string(BASE_HTML, title="Crear cuenta", body=body)


@app.route("/login", methods=["GET", "POST"])
def login_view():
    if get_current_user() is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_user_by_email(email)
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Correo o contraseña incorrectos.")
            return redirect(url_for("login_view"))
        session["user_id"] = int(user["id"])
        session["empresa_activa_id"] = get_default_empresa_id_for_user(int(user["id"]))
        flash(f"Bienvenido, {user['name']}.")
        return redirect(url_for("index"))

    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Iniciar sesión</h1><p style="margin:0;">Accede a tus empresas y tu información tributaria.</p></div>
        <div class="card">
          <form method="post">
            <p><label>Correo</label><input type="email" name="email"></p>
            <p><label>Contraseña</label><input type="password" name="password"></p>
            <p><button class="btn" type="submit">Ingresar</button></p>
          </form>
        </div>
        """
    )
    return render_template_string(BASE_HTML, title="Ingresar", body=body)


@app.route("/logout")
def logout_view():
    session.clear()
    flash("Sesión cerrada correctamente.")
    return redirect(url_for("login_view"))


@app.route("/empresa-activa", methods=["POST"])
@login_required
def set_active_empresa():
    empresa_id_raw = request.form.get("empresa_id", "").strip()
    user = get_current_user()
    if user is None or not empresa_id_raw.isdigit():
        flash("Empresa inválida.")
        return redirect(request.referrer or url_for("index"))
    empresa_id = int(empresa_id_raw)
    if not user_has_access_to_empresa(int(user["id"]), empresa_id):
        flash("No tienes acceso a esa empresa.")
        return redirect(request.referrer or url_for("index"))
    session["empresa_activa_id"] = empresa_id
    empresa = get_empresa_by_id(empresa_id)
    flash(f"Empresa activa actualizada a: {empresa['nombre']}")
    return redirect(request.referrer or url_for("index"))


@app.route("/reset", methods=["POST"])
@login_required
def reset_data():
    empresa = get_active_empresa()
    reset_database_for_empresa(int(empresa["id"]))
    flash("Todos los datos cargados de la empresa activa fueron eliminados.")
    return redirect(url_for("index"))


@app.route("/")
@login_required
def index():
    empresa = get_active_empresa()
    metrics = get_dashboard_metrics(int(empresa["id"]))
    imports = get_recent_imports(empresa_id=int(empresa["id"]))
    body = render_template_string(
        """
        <div class="hero">
          <h1 style="margin:0 0 8px;">Dashboard TributApp</h1>
          <p style="margin:0;">Software tributario y contable multiempresa.</p>
        </div>
        <div class="grid cards">
          <div class="card"><div class="muted">Ventas netas</div><div class="metric">{{ metrics['ventas_netas']|clp }}</div></div>
          <div class="card"><div class="muted">Compras netas</div><div class="metric">{{ metrics['compras_netas']|clp }}</div></div>
          <div class="card"><div class="muted">IVA débito</div><div class="metric">{{ metrics['iva_debito']|clp }}</div></div>
          <div class="card"><div class="muted">IVA crédito</div><div class="metric">{{ metrics['iva_credito']|clp }}</div></div>
          <div class="card"><div class="muted">IVA estimado</div><div class="metric">{{ metrics['iva_pagar']|clp }}</div></div>
        </div>
        <div class="grid" style="grid-template-columns: 2fr 1fr; margin-top: 16px;">
          <div class="card">
            <h3>Últimos archivos importados</h3>
            <table>
              <thead><tr><th>Archivo</th><th>Tipo</th><th>Período</th><th>Filas</th></tr></thead>
              <tbody>
              {% for item in imports %}
                <tr>
                  <td>{{ item['nombre_archivo'] }}</td>
                  <td>{{ item['tipo_archivo'] }}</td>
                  <td>{{ item['periodo'] }}</td>
                  <td>{{ item['total_filas'] }}</td>
                </tr>
              {% else %}
                <tr><td colspan="4">Aún no hay archivos.</td></tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
          <div class="card">
            <h3>Acciones rápidas</h3>
            <p><a class="btn" href="{{ url_for('empresas_view') }}">Gestionar empresas</a></p>
            <p><a class="btn btn-secondary" href="{{ url_for('cargar_archivos') }}">Subir archivos CSV</a></p>
            <p><a class="btn btn-secondary" href="{{ url_for('calculadora_honorarios') }}">Calcular honorarios</a></p>
            <p><a class="btn btn-secondary" href="{{ url_for('calculadora_iva_view') }}">Calcular IVA</a></p>
            <form method="post" action="{{ url_for('reset_data') }}" onsubmit="return confirm('¿Eliminar todos los datos cargados de la empresa activa?');">
              <button class="btn btn-secondary" type="submit">Eliminar datos de esta empresa</button>
            </form>
          </div>
        </div>
        """,
        metrics=metrics,
        imports=imports,
    )
    return render_template_string(BASE_HTML, title="Dashboard", body=body)


@app.route("/empresas", methods=["GET", "POST"])
@login_required
def empresas_view():
    user = get_current_user()
    assert user is not None

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        rut = request.form.get("rut", "").strip()
        if not nombre:
            flash("Debes ingresar el nombre de la empresa.")
            return redirect(url_for("empresas_view"))
        empresa_id = create_empresa(nombre, rut or "00000000-0", owner_user_id=int(user["id"]))
        session["empresa_activa_id"] = empresa_id
        flash("Empresa creada correctamente y seleccionada como activa.")
        return redirect(url_for("empresas_view"))

    empresas = get_empresas(user_id=int(user["id"]))
    body = render_template_string(
        """
        <div class="hero">
          <h1 style="margin:0 0 8px;">Gestión de empresas</h1>
          <p style="margin:0;">Cada usuario administra sus propias empresas.</p>
        </div>
        <div class="grid" style="grid-template-columns: 1.2fr 1fr;">
          <div class="card">
            <h3>Mis empresas</h3>
            <table>
              <thead><tr><th>ID</th><th>Nombre</th><th>RUT</th></tr></thead>
              <tbody>
              {% for empresa in empresas %}
                <tr><td>{{ empresa['id'] }}</td><td>{{ empresa['nombre'] }}</td><td>{{ empresa['rut'] }}</td></tr>
              {% else %}
                <tr><td colspan="3">No hay empresas.</td></tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
          <div class="card">
            <h3>Crear empresa</h3>
            <form method="post">
              <p><label>Nombre</label><input type="text" name="nombre" placeholder="Ejemplo: INY SpA"></p>
              <p><label>RUT</label><input type="text" name="rut" placeholder="Ejemplo: 77.801.453-K"></p>
              <p><button class="btn" type="submit">Guardar empresa</button></p>
            </form>
          </div>
        </div>
        """,
        empresas=empresas,
    )
    return render_template_string(BASE_HTML, title="Empresas", body=body)


@app.route("/cargar", methods=["GET", "POST"])
@login_required
def cargar_archivos():
    if request.method == "POST":
        file = request.files.get("archivo")
        if not file or not file.filename:
            flash("Selecciona un archivo CSV.")
            return redirect(url_for("cargar_archivos"))
        try:
            empresa = get_active_empresa()
            tipo, periodo, inserted = process_csv(file, empresa_id=int(empresa["id"]))
            flash(f"Archivo procesado correctamente. Tipo: {tipo}. Período: {periodo}. Filas insertadas: {inserted}.")
            return redirect(url_for("index"))
        except Exception as exc:
            flash(f"Error al procesar archivo: {exc}")
            return redirect(url_for("cargar_archivos"))

    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Carga de archivos del SII</h1><p style="margin:0;">Los archivos se cargan en la empresa activa.</p></div>
        <div class="card">
          <form method="post" enctype="multipart/form-data">
            <p><label>Archivo CSV</label><input type="file" name="archivo" accept=".csv" required></p>
            <p><button class="btn" type="submit">Procesar archivo</button></p>
          </form>
        </div>
        """
    )
    return render_template_string(BASE_HTML, title="Cargar archivos", body=body)


@app.route("/compras")
@login_required
def compras_view():
    empresa = get_active_empresa()
    compras = get_compras(empresa_id=int(empresa["id"]))
    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">RCV Compras</h1><p style="margin:0;">Listado de compras de la empresa activa.</p></div>
        <div class="card">
          <table>
            <thead><tr><th>Fecha</th><th>Tipo</th><th>Folio</th><th>RUT</th><th>Proveedor</th><th>Neto</th><th>IVA</th><th>Total</th></tr></thead>
            <tbody>
              {% for item in compras %}
              <tr>
                <td>{{ item['fecha'] }}</td>
                <td>{{ item['tipo_doc'] }}</td>
                <td>{{ item['folio'] }}</td>
                <td>{{ item['rut'] }}</td>
                <td>{{ item['razon_social'] }}</td>
                <td>{{ item['neto']|clp }}</td>
                <td>{{ item['iva']|clp }}</td>
                <td>{{ item['total']|clp }}</td>
              </tr>
              {% else %}
              <tr><td colspan="8">No hay compras.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        compras=compras,
    )
    return render_template_string(BASE_HTML, title="Compras", body=body)


@app.route("/calculadoras/honorarios", methods=["GET", "POST"])
@login_required
def calculadora_honorarios():
    resultado = None
    modo = "bruto"
    monto = ""
    tasa_porcentaje = "15,25"
    if request.method == "POST":
        modo = request.form.get("modo", "bruto")
        monto = request.form.get("monto", "").strip()
        tasa_porcentaje = request.form.get("tasa", "15,25").strip()
        monto_num = parse_decimal(monto)
        tasa_num = parse_percentage(tasa_porcentaje)
        resultado = calcular_honorarios_desde_liquido(monto_num, tasa_num) if modo == "liquido" else calcular_honorarios_desde_bruto(monto_num, tasa_num)

    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Calculadora de Honorarios</h1><p style="margin:0;">Calcula bruto, retención y líquido con la tasa vigente.</p></div>
        <div class="card">
          <form method="post">
            <div class="form-row">
              <div>
                <label>Modo</label>
                <select name="modo">
                  <option value="bruto" {% if modo == 'bruto' %}selected{% endif %}>Tengo bruto</option>
                  <option value="liquido" {% if modo == 'liquido' %}selected{% endif %}>Quiero líquido</option>
                </select>
              </div>
              <div>
                <label>Tasa (%)</label>
                <input type="text" name="tasa" value="{{ tasa_porcentaje }}">
              </div>
            </div>
            <p><label>Monto</label><input type="text" name="monto" value="{{ monto }}"></p>
            <p><button class="btn" type="submit">Calcular</button></p>
          </form>
        </div>
        {% if resultado %}
        <div class="grid cards">
          <div class="card"><div class="muted">Bruto</div><div class="metric">{{ resultado['bruto']|clp }}</div></div>
          <div class="card"><div class="muted">Retención</div><div class="metric">{{ resultado['retencion']|clp }}</div></div>
          <div class="card"><div class="muted">Líquido</div><div class="metric">{{ resultado['liquido']|clp }}</div></div>
        </div>
        {% endif %}
        """,
        resultado=resultado,
        modo=modo,
        monto=monto,
        tasa_porcentaje=tasa_porcentaje,
    )
    return render_template_string(BASE_HTML, title="Honorarios", body=body)


@app.route("/calculadoras/iva", methods=["GET", "POST"])
@login_required
def calculadora_iva_view():
    resultado = None
    ventas_netas = ""
    compras_netas = ""
    if request.method == "POST":
        ventas_netas = request.form.get("ventas_netas", "").strip()
        compras_netas = request.form.get("compras_netas", "").strip()
        resultado = calcular_iva(parse_decimal(ventas_netas), parse_decimal(compras_netas))

    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Calculadora IVA</h1><p style="margin:0;">Calcula débito, crédito e IVA estimado a pagar.</p></div>
        <div class="card">
          <form method="post">
            <div class="form-row">
              <div><label>Ventas netas</label><input type="text" name="ventas_netas" value="{{ ventas_netas }}"></div>
              <div><label>Compras netas</label><input type="text" name="compras_netas" value="{{ compras_netas }}"></div>
            </div>
            <p><button class="btn" type="submit">Calcular IVA</button></p>
          </form>
        </div>
        {% if resultado %}
        <div class="grid cards">
          <div class="card"><div class="muted">IVA débito</div><div class="metric">{{ resultado['iva_debito']|clp }}</div></div>
          <div class="card"><div class="muted">IVA crédito</div><div class="metric">{{ resultado['iva_credito']|clp }}</div></div>
          <div class="card"><div class="muted">IVA a pagar</div><div class="metric">{{ resultado['iva_pagar']|clp }}</div></div>
        </div>
        {% endif %}
        """,
        resultado=resultado,
        ventas_netas=ventas_netas,
        compras_netas=compras_netas,
    )
    return render_template_string(BASE_HTML, title="Calculadora IVA", body=body)


@app.route("/reportes")
@login_required
def reportes_view():
    empresa = get_active_empresa()
    data = get_reportes(empresa_id=int(empresa["id"]))
    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Reportes</h1><p style="margin:0;">Vista gerencial simple por período de la empresa activa.</p></div>
        <div class="grid" style="grid-template-columns: 1fr 1fr;">
          <div class="card">
            <h3>Ventas por período</h3>
            <table>
              <thead><tr><th>Período</th><th>Neto</th><th>IVA</th><th>Total</th></tr></thead>
              <tbody>
              {% for item in data['ventas_por_periodo'] %}
                <tr><td>{{ item['periodo'] }}</td><td>{{ item['neto']|clp }}</td><td>{{ item['iva']|clp }}</td><td>{{ item['total']|clp }}</td></tr>
              {% else %}
                <tr><td colspan="4">Sin datos.</td></tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
          <div class="card">
            <h3>Compras por período</h3>
            <table>
              <thead><tr><th>Período</th><th>Neto</th><th>IVA</th><th>Total</th></tr></thead>
              <tbody>
              {% for item in data['compras_por_periodo'] %}
                <tr><td>{{ item['periodo'] }}</td><td>{{ item['neto']|clp }}</td><td>{{ item['iva']|clp }}</td><td>{{ item['total']|clp }}</td></tr>
              {% else %}
                <tr><td colspan="4">Sin datos.</td></tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
        """,
        data=data,
    )
    return render_template_string(BASE_HTML, title="Reportes", body=body)


# -----------------------------
# Tests
# -----------------------------
class TributAppTests(unittest.TestCase):
    def test_parse_decimal(self) -> None:
        self.assertEqual(parse_decimal("1.234,56"), 1234.56)
        self.assertEqual(parse_decimal("1234.56"), 1234.56)
        self.assertEqual(parse_decimal("1,234.56"), 1234.56)
        self.assertEqual(parse_decimal(""), 0.0)
        self.assertEqual(parse_decimal(None), 0.0)

    def test_parse_percentage(self) -> None:
        self.assertEqual(parse_percentage("15,25"), 0.1525)
        self.assertEqual(parse_percentage("15.25"), 0.1525)
        self.assertEqual(parse_percentage("0,1525"), 0.1525)

    def test_create_user_and_lookup(self) -> None:
        global DB_PATH
        original_db_path = DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                DB_PATH = Path(tmp_dir) / "test_tributapp.db"
                init_db()
                user_id = create_user("Octavio", "octavio@example.com", "clave123")
                self.assertGreater(user_id, 0)
                user = get_user_by_email("octavio@example.com")
                self.assertIsNotNone(user)
                self.assertEqual(user["name"], "Octavio")
                self.assertTrue(check_password_hash(user["password_hash"], "clave123"))
        finally:
            DB_PATH = original_db_path

    def test_create_empresa(self) -> None:
        global DB_PATH
        original_db_path = DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                DB_PATH = Path(tmp_dir) / "test_tributapp.db"
                init_db()
                user_id = create_user("Octavio", "octa@test.com", "123456")
                empresa_id = create_empresa("INY SpA", "77801453-K", owner_user_id=user_id)
                self.assertGreater(empresa_id, 0)
                empresas = get_empresas(user_id=user_id)
                nombres = [e["nombre"] for e in empresas]
                self.assertIn("INY SpA", nombres)
        finally:
            DB_PATH = original_db_path

    def test_active_empresa_selection_with_session(self) -> None:
        global DB_PATH
        original_db_path = DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                DB_PATH = Path(tmp_dir) / "test_tributapp.db"
                init_db()
                user_id = create_user("Cliente", "cliente@test.com", "123456")
                empresa_id = create_empresa("Cliente Uno", "11111111-1", owner_user_id=user_id)
                with app.test_request_context("/"):
                    session.clear()
                    session["user_id"] = user_id
                    session["empresa_activa_id"] = empresa_id
                    empresa = get_active_empresa()
                    self.assertEqual(empresa["nombre"], "Cliente Uno")
        finally:
            DB_PATH = original_db_path

    def test_detect_tipo_archivo(self) -> None:
        self.assertEqual(detect_tipo_archivo("RCV_COMPRA_123_202601.csv"), "compra")
        self.assertEqual(detect_tipo_archivo("RCV_VENTA_123_202601.csv"), "venta")
        self.assertEqual(detect_tipo_archivo("otro.csv"), "desconocido")

    def test_detect_periodo(self) -> None:
        self.assertEqual(detect_periodo("RCV_COMPRA_12345678_202601.csv"), "202601")

    def test_map_columns_accepts_common_headers(self) -> None:
        headers = ["Tipo Doc", "Folio", "Fecha", "RUT", "Razón Social", "Monto Neto", "IVA", "Total"]
        mapping = map_columns(headers)
        for column in REQUIRED_COLUMNS:
            self.assertIn(column, mapping)

    def test_calcular_honorarios_desde_bruto(self) -> None:
        resultado = calcular_honorarios_desde_bruto(1000000, 0.1525)
        self.assertEqual(resultado["retencion"], 152500.0)
        self.assertEqual(resultado["liquido"], 847500.0)

    def test_calcular_honorarios_desde_liquido(self) -> None:
        resultado = calcular_honorarios_desde_liquido(1000000, 0.1525)
        self.assertAlmostEqual(resultado["bruto"], 1179941.0, places=0)
        self.assertAlmostEqual(resultado["retencion"], 179941.0, places=0)

    def test_calcular_iva(self) -> None:
        resultado = calcular_iva(3000000, 1000000)
        self.assertEqual(resultado["iva_debito"], 570000.0)
        self.assertEqual(resultado["iva_credito"], 190000.0)
        self.assertEqual(resultado["iva_pagar"], 380000.0)


if __name__ == "__main__":
    init_db()
    if "--test" in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    elif "--runserver" in sys.argv:
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
    else:
        print("Base de datos inicializada en:", DB_PATH)
        print("Para ejecutar pruebas: python tributapp_app.py --test")
        print("Para levantar la web: python tributapp_app.py --runserver")
