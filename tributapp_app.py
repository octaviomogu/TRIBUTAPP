from __future__ import annotations

import csv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import io
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, flash, make_response, redirect, render_template_string, request, session, url_for
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



def calcular_f29_resumen(
    ventas_netas: float,
    compras_netas: float,
    ppm_rate: float = 0.0,
    retenciones_honorarios: float = 0.0,
    otros_impuestos: float = 0.0,
) -> dict[str, float]:
    iva = calcular_iva(ventas_netas, compras_netas)
    ppm = round(ventas_netas * ppm_rate, 2)
    subtotal = round(iva["iva_pagar"] + ppm + retenciones_honorarios + otros_impuestos, 2)
    return {
        "codigo_538_iva_debito": iva["iva_debito"],
        "codigo_511_iva_credito": iva["iva_credito"],
        "codigo_089_ppm": ppm,
        "codigo_151_ret_honorarios": round(retenciones_honorarios, 2),
        "otros_impuestos": round(otros_impuestos, 2),
        "total_estimado_pagar": subtotal,
    }



IGC_2026_TRAMOS = [
    (0.0, 11265804.00, 0.00, 0.00),
    (11265804.01, 25035120.00, 0.04, 450632.16),
    (25035120.01, 41725200.00, 0.08, 1452036.96),
    (41725200.01, 58415280.00, 0.135, 3746922.96),
    (58415280.01, 75105360.00, 0.23, 9296374.56),
    (75105360.01, 100140480.00, 0.304, 14854171.20),
    (100140480.01, 258696240.00, 0.35, 19460633.28),
    (258696240.01, float("inf"), 0.40, 32395445.28),
]

GASTO_PRESUNTO_HONORARIOS_TOPE_2026 = 12517560.0
APV_TOPE_2026 = 23836776.0


def calcular_igc_2026(base_imponible: float) -> float:
    renta = max(round(base_imponible, 2), 0.0)
    for desde, hasta, factor, rebaja in IGC_2026_TRAMOS:
        if desde <= renta <= hasta:
            impuesto = round(renta * factor - rebaja, 2)
            return max(impuesto, 0.0)
    return 0.0



def calcular_f22_persona_completo(
    honorarios_brutos: float = 0.0,
    honorarios_con_retencion: float = 0.0,
    remuneraciones_afectas: float = 0.0,
    iusc_retenido: float = 0.0,
    otras_rentas_afectas: float = 0.0,
    gastos_modo: str = "presunto",
    gastos_efectivos: float = 0.0,
    apv_rebaja: float = 0.0,
    retenciones_honorarios: float = 0.0,
    ppm_pagado: float = 0.0,
) -> dict[str, float | str]:
    honorarios_brutos = round(float(honorarios_brutos), 2)
    honorarios_con_retencion = round(float(honorarios_con_retencion), 2)
    remuneraciones_afectas = round(float(remuneraciones_afectas), 2)
    iusc_retenido = round(float(iusc_retenido), 2)
    otras_rentas_afectas = round(float(otras_rentas_afectas), 2)
    gastos_efectivos = round(float(gastos_efectivos), 2)
    apv_rebaja = min(round(float(apv_rebaja), 2), APV_TOPE_2026)
    retenciones_honorarios = round(float(retenciones_honorarios), 2)
    ppm_pagado = round(float(ppm_pagado), 2)

    if gastos_modo == "efectivo":
        gasto_rebajable = min(gastos_efectivos, honorarios_brutos)
        modo_gasto = "Gasto efectivo"
    else:
        gasto_rebajable = min(round(honorarios_brutos * 0.30, 2), GASTO_PRESUNTO_HONORARIOS_TOPE_2026)
        modo_gasto = "Gasto presunto 30%"

    honorarios_netos = max(honorarios_brutos - gasto_rebajable, 0.0)
    base_imponible = max(remuneraciones_afectas + honorarios_netos + otras_rentas_afectas - apv_rebaja, 0.0)
    impuesto_estimado = calcular_igc_2026(base_imponible)
    creditos = round(iusc_retenido + retenciones_honorarios + ppm_pagado, 2)
    saldo = round(impuesto_estimado - creditos, 2)

    return {
        "perfil": "Persona natural completa",
        "modo_gasto": modo_gasto,
        "honorarios_brutos": honorarios_brutos,
        "gasto_rebajable": round(gasto_rebajable, 2),
        "honorarios_netos": round(honorarios_netos, 2),
        "remuneraciones_afectas": remuneraciones_afectas,
        "otras_rentas_afectas": otras_rentas_afectas,
        "apv_rebaja_aplicada": apv_rebaja,
        "base_imponible": round(base_imponible, 2),
        "impuesto_estimado": round(impuesto_estimado, 2),
        "creditos": creditos,
        "saldo_estimado": saldo,
        "utilidad_tributaria": round(honorarios_netos + otras_rentas_afectas, 2),
    }



def calcular_f22_estimado(
    perfil: str,
    ingresos: float,
    costos_gastos: float = 0.0,
    retiros: float = 0.0,
    honorarios_retencion: float = 0.0,
    ppm_pagado: float = 0.0,
    base_personal_otras_rentas: float = 0.0,
) -> dict[str, float | str]:
    ingresos = round(float(ingresos), 2)
    costos_gastos = round(float(costos_gastos), 2)
    retiros = round(float(retiros), 2)
    honorarios_retencion = round(float(honorarios_retencion), 2)
    ppm_pagado = round(float(ppm_pagado), 2)
    base_personal_otras_rentas = round(float(base_personal_otras_rentas), 2)

    if perfil == "persona_honorarios":
        return calcular_f22_persona_completo(
            honorarios_brutos=ingresos,
            honorarios_con_retencion=ingresos,
            remuneraciones_afectas=0.0,
            iusc_retenido=0.0,
            otras_rentas_afectas=base_personal_otras_rentas,
            gastos_modo="presunto",
            gastos_efectivos=0.0,
            apv_rebaja=0.0,
            retenciones_honorarios=honorarios_retencion,
            ppm_pagado=ppm_pagado,
        )

    utilidad = round(max(ingresos - costos_gastos, 0), 2)

    if perfil == "empresa_propyme_general":
        impuesto_primera_categoria = round(utilidad * 0.125, 2)
        saldo = round(impuesto_primera_categoria - ppm_pagado, 2)
        return {
            "perfil": "Empresa Pro Pyme General",
            "base_imponible": utilidad,
            "impuesto_estimado": impuesto_primera_categoria,
            "creditos": ppm_pagado,
            "saldo_estimado": saldo,
            "utilidad_tributaria": utilidad,
        }

    if perfil == "empresa_propyme_transparente":
        base_duenos = round(max(retiros if retiros > 0 else utilidad, 0), 2)
        impuesto_empresa = 0.0
        saldo = round(0.0 - ppm_pagado, 2)
        return {
            "perfil": "Empresa Pro Pyme Transparente",
            "base_imponible": base_duenos,
            "impuesto_estimado": impuesto_empresa,
            "creditos": ppm_pagado,
            "saldo_estimado": saldo,
            "utilidad_tributaria": utilidad,
        }

    if perfil == "empresa_regimen_general_14a":
        impuesto_primera_categoria = round(utilidad * 0.27, 2)
        saldo = round(impuesto_primera_categoria - ppm_pagado, 2)
        return {
            "perfil": "Empresa Régimen General 14 A",
            "base_imponible": utilidad,
            "impuesto_estimado": impuesto_primera_categoria,
            "creditos": ppm_pagado,
            "saldo_estimado": saldo,
            "utilidad_tributaria": utilidad,
        }

    raise ValueError("Perfil F22 no soportado")


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



def get_balance_general(empresa_id: int) -> dict[str, float]:
    conn = get_connection()
    ventas_total = conn.execute(
        "SELECT COALESCE(SUM(total), 0) AS total FROM ventas WHERE empresa_id = ?",
        (empresa_id,),
    ).fetchone()["total"]
    compras_total = conn.execute(
        "SELECT COALESCE(SUM(total), 0) AS total FROM compras WHERE empresa_id = ?",
        (empresa_id,),
    ).fetchone()["total"]
    iva_debito = conn.execute(
        "SELECT COALESCE(SUM(iva), 0) AS total FROM ventas WHERE empresa_id = ?",
        (empresa_id,),
    ).fetchone()["total"]
    iva_credito = conn.execute(
        "SELECT COALESCE(SUM(iva), 0) AS total FROM compras WHERE empresa_id = ?",
        (empresa_id,),
    ).fetchone()["total"]
    conn.close()

    caja_bancos = round(float(ventas_total) - float(compras_total), 2)
    iva_por_pagar = round(float(iva_debito) - float(iva_credito), 2)
    activos = round(caja_bancos + max(float(iva_credito) - float(iva_debito), 0), 2)
    pasivos = round(max(iva_por_pagar, 0), 2)
    patrimonio = round(activos - pasivos, 2)

    return {
        "caja_bancos": caja_bancos,
        "iva_debito": float(iva_debito),
        "iva_credito": float(iva_credito),
        "iva_por_pagar": iva_por_pagar,
        "activos": activos,
        "pasivos": pasivos,
        "patrimonio": patrimonio,
    }



def generate_report_csv_content(reportes: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    writer.writerow(["VENTAS POR PERIODO"])
    writer.writerow(["periodo", "neto", "iva", "total"])
    for row in reportes["ventas_por_periodo"]:
        writer.writerow([row["periodo"], row["neto"], row["iva"], row["total"]])
    writer.writerow([])

    writer.writerow(["COMPRAS POR PERIODO"])
    writer.writerow(["periodo", "neto", "iva", "total"])
    for row in reportes["compras_por_periodo"]:
        writer.writerow([row["periodo"], row["neto"], row["iva"], row["total"]])
    writer.writerow([])

    writer.writerow(["TOP PROVEEDORES"])
    writer.writerow(["razon_social", "total"])
    for row in reportes["top_proveedores"]:
        writer.writerow([row["razon_social"], row["total"]])
    writer.writerow([])

    writer.writerow(["TOP CLIENTES"])
    writer.writerow(["razon_social", "total"])
    for row in reportes["top_clientes"]:
        writer.writerow([row["razon_social"], row["total"]])

    return output.getvalue()



def generate_balance_csv_content(balance: dict[str, float]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["BALANCE GENERAL"])
    writer.writerow(["concepto", "monto"])
    for key, value in balance.items():
        writer.writerow([key, value])
    return output.getvalue()


def generate_balance_pdf_content(balance: dict[str, float], empresa_nombre: str) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 60
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, "TributApp - Balance General")

    y -= 30
    c.setFont("Helvetica", 12)
    c.drawString(50, y, f"Empresa: {empresa_nombre}")

    y -= 25
    c.drawString(50, y, f"Fecha reporte: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    y -= 40
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Concepto")
    c.drawString(300, y, "Monto")

    y -= 15
    c.setFont("Helvetica", 11)

    for key, value in balance.items():
        c.drawString(50, y, key.replace('_', ' ').title())
        c.drawRightString(500, y, f"${value:,.0f}".replace(",", "."))
        y -= 18

    y -= 40
    c.setFont("Helvetica", 10)
    c.drawString(50, y, "Generado automáticamente por TributApp")

    c.showPage()
    c.save()

    buffer.seek(0)
    return buffer.read()



def get_periodos_empresa(empresa_id: int) -> list[str]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT periodo FROM (
            SELECT periodo FROM ventas WHERE empresa_id = ?
            UNION
            SELECT periodo FROM compras WHERE empresa_id = ?
        ) t
        WHERE periodo IS NOT NULL AND periodo <> ''
        ORDER BY periodo DESC
        """,
        (empresa_id, empresa_id),
    ).fetchall()
    conn.close()
    return [row["periodo"] for row in rows]



def get_resumen_periodo_empresa(empresa_id: int, periodo: str) -> dict[str, float]:
    conn = get_connection()
    ventas_netas = conn.execute(
        "SELECT COALESCE(SUM(neto), 0) AS total FROM ventas WHERE empresa_id = ? AND periodo = ?",
        (empresa_id, periodo),
    ).fetchone()["total"]
    compras_netas = conn.execute(
        "SELECT COALESCE(SUM(neto), 0) AS total FROM compras WHERE empresa_id = ? AND periodo = ?",
        (empresa_id, periodo),
    ).fetchone()["total"]
    iva_debito = conn.execute(
        "SELECT COALESCE(SUM(iva), 0) AS total FROM ventas WHERE empresa_id = ? AND periodo = ?",
        (empresa_id, periodo),
    ).fetchone()["total"]
    iva_credito = conn.execute(
        "SELECT COALESCE(SUM(iva), 0) AS total FROM compras WHERE empresa_id = ? AND periodo = ?",
        (empresa_id, periodo),
    ).fetchone()["total"]
    conn.close()
    return {
        "ventas_netas": float(ventas_netas),
        "compras_netas": float(compras_netas),
        "iva_debito": float(iva_debito),
        "iva_credito": float(iva_credito),
        "iva_pagar": float(round(iva_debito - iva_credito, 2)),
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
  <title>{{ title }} - TributApp | Calculadora IVA, Honorarios, F29 y F22 Chile</title>
  <meta name="description" content="TributApp es un software tributario para Chile que permite calcular IVA, boletas de honorarios, estimar F29 mensual y proyectar F22 anual. Incluye calculadoras gratuitas y gestión contable basada en RCV del SII.">
  <meta name="keywords" content="calculadora IVA Chile, boleta honorarios calculadora, F29 Chile, F22 renta Chile, software tributario Chile, RCV SII, cálculo IVA débito crédito, estimar impuestos Chile">
  <meta name="author" content="TributApp">
  <meta name="robots" content="index, follow">
  <meta property="og:title" content="TributApp - Software tributario para Chile">
  <meta property="og:description" content="Calcula IVA, honorarios, F29 y proyecta F22 automáticamente desde tus libros RCV del SII.">
  <meta property="og:type" content="website">
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
      <a href="{{ url_for('f29_view') }}">F29</a>
      <a href="{{ url_for('f22_view') }}">F22</a>
      <a href="{{ url_for('logout_view') }}">Salir</a>
      {% else %}
      <a href="{{ url_for('login_view') }}">Ingresar</a>
      <a href="{{ url_for('register_view') }}">Crear cuenta</a>
      <a href="{{ url_for('calculadora_honorarios') }}">Honorarios</a>
      <a href="{{ url_for('calculadora_iva_view') }}">IVA</a>
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
def index():
    current_user = get_current_user()
    if current_user is None:
        body = render_template_string(
            """
            <div class="hero">
              <div style="display:grid; grid-template-columns: 1.25fr 0.95fr; gap:24px; align-items:center;">
                <div>
                  <div class="pill" style="background:rgba(255,255,255,.14); color:white;">Hecho para Chile · IVA, Honorarios, F29 y RCV</div>
                  <h1 style="margin:14px 0 10px; font-size:42px; line-height:1.05;">El software tributario chileno que convierte cálculos gratis en gestión real.</h1>
                  <p style="margin:0; font-size:18px; max-width:720px; color:rgba(255,255,255,.92);">Usa gratis las calculadoras de IVA y boletas de honorarios, y cuando estés listo da el salto a una plataforma con empresas, usuarios, RCV del SII y F29 automático.</p>
                  <div style="margin-top:20px; display:flex; gap:12px; flex-wrap:wrap;">
                    <a class="btn" href="{{ url_for('register_view') }}" style="background:white; color:#0b1320; font-weight:bold;">Crear cuenta gratis</a>
                    <a class="btn btn-secondary" href="{{ url_for('login_view') }}" style="background:rgba(255,255,255,.14); color:white; border:1px solid rgba(255,255,255,.18);">Ingresar</a>
                  </div>
                  <div style="margin-top:18px; display:flex; gap:18px; flex-wrap:wrap; color:rgba(255,255,255,.92); font-size:14px;">
                    <span>✔ Calculadoras gratuitas</span>
                    <span>✔ Multiempresa</span>
                    <span>✔ F29 operativo</span>
                  </div>
                </div>
                <div class="card" style="margin:0; background:rgba(255,255,255,.97);">
                  <div class="muted">Vista rápida</div>
                  <h3 style="margin:8px 0 14px;">Lo que TributApp ya resuelve</h3>
                  <div class="grid cards" style="grid-template-columns:1fr 1fr; gap:12px;">
                    <div style="padding:14px; border:1px solid #dbe3ea; border-radius:14px;">
                      <div class="muted">RCV</div>
                      <div style="font-size:22px; font-weight:bold;">Compras y ventas</div>
                    </div>
                    <div style="padding:14px; border:1px solid #dbe3ea; border-radius:14px;">
                      <div class="muted">F29</div>
                      <div style="font-size:22px; font-weight:bold;">Estimación mensual</div>
                    </div>
                    <div style="padding:14px; border:1px solid #dbe3ea; border-radius:14px;">
                      <div class="muted">Usuarios</div>
                      <div style="font-size:22px; font-weight:bold;">Acceso privado</div>
                    </div>
                    <div style="padding:14px; border:1px solid #dbe3ea; border-radius:14px;">
                      <div class="muted">Empresas</div>
                      <div style="font-size:22px; font-weight:bold;">Múltiples clientes</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div class="grid cards" style="margin-top: 4px;">
              <div class="card" style="position:relative; overflow:hidden;">
                <div class="muted">Herramienta gratuita</div>
                <h3 style="margin:8px 0;">Calculadora de Honorarios</h3>
                <p>Calcula bruto, retención y líquido con una interfaz simple, útil y lista para atraer tráfico orgánico.</p>
                <p style="margin:14px 0 0;"><a class="btn" href="{{ url_for('calculadora_honorarios') }}">Usar calculadora</a></p>
              </div>
              <div class="card" style="position:relative; overflow:hidden;">
                <div class="muted">Herramienta gratuita</div>
                <h3 style="margin:8px 0;">Calculadora IVA</h3>
                <p>Obtén IVA débito, crédito e IVA estimado a pagar en segundos. Ideal para captar visitas con intención real.</p>
                <p style="margin:14px 0 0;"><a class="btn" href="{{ url_for('calculadora_iva_view') }}">Usar calculadora</a></p>
              </div>
              <div class="card" style="position:relative; overflow:hidden;">
                <div class="muted">Próximo gran módulo</div>
                <h3 style="margin:8px 0;">F29 Automático</h3>
                <p>Convierte tus libros y RCV en una estimación operativa del F29 para cada período y empresa.</p>
                <p style="margin:14px 0 0;"><a class="btn btn-secondary" href="{{ url_for('register_view') }}">Crear cuenta para usarlo</a></p>
              </div>
            </div>

            <div class="grid" style="grid-template-columns: 1.15fr 0.85fr; margin-top:16px;">
              <div class="card">
                <h3 style="margin-top:0;">Por qué TributApp puede ganar espacio en Chile</h3>
                <div class="grid" style="grid-template-columns:1fr 1fr; gap:14px; margin-top:14px;">
                  <div>
                    <div class="pill">Atracción</div>
                    <p style="margin:10px 0 0;">Calculadoras públicas para captar búsquedas de alto valor.</p>
                  </div>
                  <div>
                    <div class="pill">Conversión</div>
                    <p style="margin:10px 0 0;">De herramienta gratuita a cuenta creada en pocos clics.</p>
                  </div>
                  <div>
                    <div class="pill">Operación</div>
                    <p style="margin:10px 0 0;">RCV, empresas, usuarios y panel privado en una sola app.</p>
                  </div>
                  <div>
                    <div class="pill">Especialización</div>
                    <p style="margin:10px 0 0;">Enfoque tributario chileno: F29 hoy, F22 después.</p>
                  </div>
                </div>
              </div>
              <div class="card">
                <h3 style="margin-top:0;">Empieza ahora</h3>
                <p class="muted">Prueba gratis las calculadoras y luego entra al flujo completo.</p>
                <div style="display:grid; gap:10px; margin-top:16px;">
                  <a class="btn" href="{{ url_for('calculadora_honorarios') }}">Probar Honorarios</a>
                  <a class="btn btn-secondary" href="{{ url_for('calculadora_iva_view') }}">Probar IVA</a>
                  <a class="btn btn-secondary" href="{{ url_for('register_view') }}">Crear cuenta gratis</a>
                </div>
              </div>
            </div>
            """
        )
        return render_template_string(BASE_HTML, title="TributApp", body=body)

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


@app.route("/f29", methods=["GET", "POST"])
@login_required
def f29_view():
    empresa = get_active_empresa()
    periodos = get_periodos_empresa(int(empresa["id"]))
    periodo = request.form.get("periodo") if request.method == "POST" else (periodos[0] if periodos else "")
    ppm_porcentaje = request.form.get("ppm_porcentaje", "0") if request.method == "POST" else "0"
    ret_honorarios = request.form.get("ret_honorarios", "0") if request.method == "POST" else "0"
    otros_impuestos = request.form.get("otros_impuestos", "0") if request.method == "POST" else "0"

    resumen_periodo = None
    resumen_f29 = None

    if periodo:
        resumen_periodo = get_resumen_periodo_empresa(int(empresa["id"]), periodo)
        ppm_rate = parse_percentage(ppm_porcentaje)
        resumen_f29 = calcular_f29_resumen(
            ventas_netas=resumen_periodo["ventas_netas"],
            compras_netas=resumen_periodo["compras_netas"],
            ppm_rate=ppm_rate,
            retenciones_honorarios=parse_decimal(ret_honorarios),
            otros_impuestos=parse_decimal(otros_impuestos),
        )

    body = render_template_string(
        """
        <div class="hero">
          <h1 style="margin:0 0 8px;">F29 Automático</h1>
          <p style="margin:0;">TributApp estima el F29 de la empresa activa desde compras y ventas cargadas del período seleccionado.</p>
        </div>
        <div class="card">
          <form method="post">
            <div class="form-row">
              <div>
                <label>Período</label>
                <select name="periodo">
                  {% for p in periodos %}
                  <option value="{{ p }}" {% if p == periodo %}selected{% endif %}>{{ p }}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <label>PPM (%)</label>
                <input type="text" name="ppm_porcentaje" value="{{ ppm_porcentaje }}" placeholder="Ejemplo: 0,25">
              </div>
            </div>
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>Retenciones honorarios</label>
                <input type="text" name="ret_honorarios" value="{{ ret_honorarios }}" placeholder="Ejemplo: 152500">
              </div>
              <div>
                <label>Otros impuestos / ajustes</label>
                <input type="text" name="otros_impuestos" value="{{ otros_impuestos }}" placeholder="Ejemplo: 0">
              </div>
            </div>
            <p style="margin-top:14px;"><button class="btn" type="submit">Calcular F29</button></p>
          </form>
        </div>

        {% if resumen_periodo and resumen_f29 %}
        <div class="grid cards">
          <div class="card"><div class="muted">Ventas netas período</div><div class="metric">{{ resumen_periodo['ventas_netas']|clp }}</div></div>
          <div class="card"><div class="muted">Compras netas período</div><div class="metric">{{ resumen_periodo['compras_netas']|clp }}</div></div>
          <div class="card"><div class="muted">IVA débito</div><div class="metric">{{ resumen_f29['codigo_538_iva_debito']|clp }}</div></div>
          <div class="card"><div class="muted">IVA crédito</div><div class="metric">{{ resumen_f29['codigo_511_iva_credito']|clp }}</div></div>
        </div>

        <div class="card">
          <h3>Resumen F29 estimado</h3>
          <table>
            <thead><tr><th>Código</th><th>Concepto</th><th>Monto</th></tr></thead>
            <tbody>
              <tr><td>538</td><td>IVA débito fiscal</td><td>{{ resumen_f29['codigo_538_iva_debito']|clp }}</td></tr>
              <tr><td>511</td><td>IVA crédito fiscal</td><td>{{ resumen_f29['codigo_511_iva_credito']|clp }}</td></tr>
              <tr><td>089</td><td>PPM</td><td>{{ resumen_f29['codigo_089_ppm']|clp }}</td></tr>
              <tr><td>151</td><td>Retenciones honorarios</td><td>{{ resumen_f29['codigo_151_ret_honorarios']|clp }}</td></tr>
              <tr><td>-</td><td>Otros impuestos / ajustes</td><td>{{ resumen_f29['otros_impuestos']|clp }}</td></tr>
              <tr><td><strong>Total</strong></td><td><strong>Total estimado a pagar</strong></td><td><strong>{{ resumen_f29['total_estimado_pagar']|clp }}</strong></td></tr>
            </tbody>
          </table>
          <p class="muted" style="margin-top:12px;">Este módulo entrega una estimación operativa del F29 con base en los datos cargados. Los códigos adicionales dependen del régimen tributario y otras variables no capturadas todavía por el sistema.</p>
        </div>
        {% elif not periodos %}
        <div class="card"><p class="muted">Aún no hay períodos disponibles. Primero carga compras y ventas del SII para calcular el F29.</p></div>
        {% endif %}
        """,
        periodos=periodos,
        periodo=periodo,
        ppm_porcentaje=ppm_porcentaje,
        ret_honorarios=ret_honorarios,
        otros_impuestos=otros_impuestos,
        resumen_periodo=resumen_periodo,
        resumen_f29=resumen_f29,
    )
    return render_template_string(BASE_HTML, title="F29", body=body)


@app.route("/descargar/reportes")
@login_required
def descargar_reportes_view():
    empresa = get_active_empresa()
    reportes = get_reportes(empresa_id=int(empresa["id"]))
    content = generate_report_csv_content(reportes)
    response = make_response(content)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=reportes_{empresa['nombre'].replace(' ', '_')}.csv"
    return response


@app.route("/descargar/balance")
@login_required
def descargar_balance_view():
    empresa = get_active_empresa()
    balance = get_balance_general(int(empresa["id"]))
    content = generate_balance_csv_content(balance)
    response = make_response(content)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=balance_{empresa['nombre'].replace(' ', '_')}.csv"
    return response


@app.route("/descargar/balance/pdf")
@login_required
def descargar_balance_pdf_view():
    empresa = get_active_empresa()
    balance = get_balance_general(int(empresa["id"]))
    pdf_bytes = generate_balance_pdf_content(balance, empresa["nombre"])

    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"attachment; filename=balance_{empresa['nombre'].replace(' ', '_')}.pdf"
    return response


@app.route("/f22", methods=["GET", "POST"])
@login_required
def f22_view():
    empresa = get_active_empresa()
    periodos = get_periodos_empresa(int(empresa["id"]))
    periodo = request.form.get("periodo") if request.method == "POST" else (periodos[0] if periodos else "")
    perfil = request.form.get("perfil", "persona_honorarios") if request.method == "POST" else "persona_honorarios"
    honorarios_retencion = request.form.get("honorarios_retencion", "0") if request.method == "POST" else "0"
    ppm_pagado = request.form.get("ppm_pagado", "0") if request.method == "POST" else "0"
    otras_rentas = request.form.get("otras_rentas", "0") if request.method == "POST" else "0"
    retiros = request.form.get("retiros", "0") if request.method == "POST" else "0"
    remuneraciones_afectas = request.form.get("remuneraciones_afectas", "0") if request.method == "POST" else "0"
    iusc_retenido = request.form.get("iusc_retenido", "0") if request.method == "POST" else "0"
    gastos_modo = request.form.get("gastos_modo", "presunto") if request.method == "POST" else "presunto"
    gastos_efectivos = request.form.get("gastos_efectivos", "0") if request.method == "POST" else "0"
    apv_rebaja = request.form.get("apv_rebaja", "0") if request.method == "POST" else "0"

    resumen_periodo = None
    resumen_f22 = None

    if periodo:
        resumen_periodo = get_resumen_periodo_empresa(int(empresa["id"]), periodo)
        if perfil == "persona_honorarios":
            resumen_f22 = calcular_f22_persona_completo(
                honorarios_brutos=resumen_periodo["ventas_netas"],
                honorarios_con_retencion=resumen_periodo["ventas_netas"],
                remuneraciones_afectas=parse_decimal(remuneraciones_afectas),
                iusc_retenido=parse_decimal(iusc_retenido),
                otras_rentas_afectas=parse_decimal(otras_rentas),
                gastos_modo=gastos_modo,
                gastos_efectivos=parse_decimal(gastos_efectivos),
                apv_rebaja=parse_decimal(apv_rebaja),
                retenciones_honorarios=parse_decimal(honorarios_retencion),
                ppm_pagado=parse_decimal(ppm_pagado),
            )
        else:
            resumen_f22 = calcular_f22_estimado(
                perfil=perfil,
                ingresos=resumen_periodo["ventas_netas"],
                costos_gastos=resumen_periodo["compras_netas"],
                retiros=parse_decimal(retiros),
                honorarios_retencion=parse_decimal(honorarios_retencion),
                ppm_pagado=parse_decimal(ppm_pagado),
                base_personal_otras_rentas=parse_decimal(otras_rentas),
            )

    body = render_template_string(
        """
        <div class="hero">
          <h1 style="margin:0 0 8px;">F22 Renta Chile</h1>
          <p style="margin:0;">Estimación operativa del F22 para personas y empresas. Para personas naturales se usa tabla de Impuesto Global Complementario AT 2026, gasto presunto 30% con tope y rebaja APV.</p>
        </div>
        <div class="card">
          <form method="post">
            <div class="form-row">
              <div>
                <label>Período base</label>
                <select name="periodo">
                  {% for p in periodos %}
                  <option value="{{ p }}" {% if p == periodo %}selected{% endif %}>{{ p }}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <label>Perfil tributario</label>
                <select name="perfil">
                  <option value="persona_honorarios" {% if perfil == 'persona_honorarios' %}selected{% endif %}>Persona natural completa</option>
                  <option value="empresa_propyme_general" {% if perfil == 'empresa_propyme_general' %}selected{% endif %}>Empresa Pro Pyme General</option>
                  <option value="empresa_propyme_transparente" {% if perfil == 'empresa_propyme_transparente' %}selected{% endif %}>Empresa Pro Pyme Transparente</option>
                  <option value="empresa_regimen_general_14a" {% if perfil == 'empresa_regimen_general_14a' %}selected{% endif %}>Empresa Régimen General 14 A</option>
                </select>
              </div>
            </div>

            {% if perfil == 'persona_honorarios' %}
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>Remuneraciones afectas</label>
                <input type="text" name="remuneraciones_afectas" value="{{ remuneraciones_afectas }}" placeholder="Ejemplo: 12000000">
              </div>
              <div>
                <label>IUSC retenido</label>
                <input type="text" name="iusc_retenido" value="{{ iusc_retenido }}" placeholder="Ejemplo: 600000">
              </div>
            </div>
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>Retenciones honorarios</label>
                <input type="text" name="honorarios_retencion" value="{{ honorarios_retencion }}" placeholder="Ejemplo: 700000">
              </div>
              <div>
                <label>Otras rentas afectas</label>
                <input type="text" name="otras_rentas" value="{{ otras_rentas }}" placeholder="Ejemplo: 0">
              </div>
            </div>
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>Modo de gasto</label>
                <select name="gastos_modo">
                  <option value="presunto" {% if gastos_modo == 'presunto' %}selected{% endif %}>Presunto 30%</option>
                  <option value="efectivo" {% if gastos_modo == 'efectivo' %}selected{% endif %}>Gasto efectivo</option>
                </select>
              </div>
              <div>
                <label>Gastos efectivos</label>
                <input type="text" name="gastos_efectivos" value="{{ gastos_efectivos }}" placeholder="Solo si eliges gasto efectivo">
              </div>
            </div>
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>APV rebajable</label>
                <input type="text" name="apv_rebaja" value="{{ apv_rebaja }}" placeholder="Ejemplo: 1000000">
              </div>
              <div>
                <label>PPM / PPV pagado</label>
                <input type="text" name="ppm_pagado" value="{{ ppm_pagado }}" placeholder="Ejemplo: 0">
              </div>
            </div>
            {% else %}
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>PPM pagado en el año</label>
                <input type="text" name="ppm_pagado" value="{{ ppm_pagado }}" placeholder="Ejemplo: 150000">
              </div>
              <div>
                <label>Retenciones honorarios</label>
                <input type="text" name="honorarios_retencion" value="{{ honorarios_retencion }}" placeholder="Ejemplo: 300000">
              </div>
            </div>
            <div class="form-row" style="margin-top:12px;">
              <div>
                <label>Otras rentas personales</label>
                <input type="text" name="otras_rentas" value="{{ otras_rentas }}" placeholder="Ejemplo: 0">
              </div>
              <div>
                <label>Retiros / distribuciones</label>
                <input type="text" name="retiros" value="{{ retiros }}" placeholder="Ejemplo: 0">
              </div>
            </div>
            {% endif %}
            <p style="margin-top:14px;"><button class="btn" type="submit">Calcular F22</button></p>
          </form>
        </div>

        {% if resumen_periodo and resumen_f22 %}
        <div class="grid cards">
          <div class="card"><div class="muted">Ingresos base</div><div class="metric">{{ resumen_periodo['ventas_netas']|clp }}</div></div>
          <div class="card"><div class="muted">Costos / gastos base</div><div class="metric">{{ resumen_periodo['compras_netas']|clp }}</div></div>
          <div class="card"><div class="muted">Utilidad / renta neta</div><div class="metric">{{ resumen_f22['utilidad_tributaria']|clp }}</div></div>
          <div class="card"><div class="muted">Base imponible</div><div class="metric">{{ resumen_f22['base_imponible']|clp }}</div></div>
        </div>

        <div class="card">
          <h3>Resumen F22 estimado</h3>
          <table>
            <tbody>
              <tr><th>Perfil</th><td>{{ resumen_f22['perfil'] }}</td></tr>
              {% if resumen_f22.get('modo_gasto') %}
              <tr><th>Modo de gasto</th><td>{{ resumen_f22['modo_gasto'] }}</td></tr>
              <tr><th>Honorarios brutos</th><td>{{ resumen_f22['honorarios_brutos']|clp }}</td></tr>
              <tr><th>Gasto rebajable</th><td>{{ resumen_f22['gasto_rebajable']|clp }}</td></tr>
              <tr><th>Honorarios netos</th><td>{{ resumen_f22['honorarios_netos']|clp }}</td></tr>
              <tr><th>Remuneraciones afectas</th><td>{{ resumen_f22['remuneraciones_afectas']|clp }}</td></tr>
              <tr><th>APV rebaja aplicada</th><td>{{ resumen_f22['apv_rebaja_aplicada']|clp }}</td></tr>
              {% endif %}
              <tr><th>Base imponible estimada</th><td>{{ resumen_f22['base_imponible']|clp }}</td></tr>
              <tr><th>Impuesto estimado</th><td>{{ resumen_f22['impuesto_estimado']|clp }}</td></tr>
              <tr><th>Créditos / pagos provisionales</th><td>{{ resumen_f22['creditos']|clp }}</td></tr>
              <tr><th>Saldo estimado</th><td><strong>{{ resumen_f22['saldo_estimado']|clp }}</strong></td></tr>
            </tbody>
          </table>
          <p class="muted" style="margin-top:12px;">Estimación operativa para personas naturales y empresas. Requiere validación final con antecedentes oficiales del SII, DJ, créditos, rebajas y registros tributarios del contribuyente.</p>
        </div>
        {% elif not periodos %}
        <div class="card"><p class="muted">Aún no hay períodos cargados. Sube compras y ventas del SII para usar el motor F22.</p></div>
        {% endif %}
        """,
        periodos=periodos,
        periodo=periodo,
        perfil=perfil,
        ppm_pagado=ppm_pagado,
        honorarios_retencion=honorarios_retencion,
        otras_rentas=otras_rentas,
        retiros=retiros,
        remuneraciones_afectas=remuneraciones_afectas,
        iusc_retenido=iusc_retenido,
        gastos_modo=gastos_modo,
        gastos_efectivos=gastos_efectivos,
        apv_rebaja=apv_rebaja,
        resumen_periodo=resumen_periodo,
        resumen_f22=resumen_f22,
    )
    return render_template_string(BASE_HTML, title="F22", body=body)


@app.route("/reportes")
@login_required
def reportes_view():
    empresa = get_active_empresa()
    data = get_reportes(empresa_id=int(empresa["id"]))
    body = render_template_string(
        """
        <div class="hero"><h1 style="margin:0 0 8px;">Reportes</h1><p style="margin:0;">Vista gerencial simple por período de la empresa activa.</p><p style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap;"><a class="btn" href="{{ url_for('descargar_reportes_view') }}">Descargar reportes CSV</a><a class="btn btn-secondary" href="{{ url_for('descargar_balance_view') }}">Descargar balance CSV</a><a class="btn btn-secondary" href="{{ url_for('descargar_balance_pdf_view') }}">Descargar balance PDF</a></p></div>
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

    def test_calcular_f29_resumen(self) -> None:
        resultado = calcular_f29_resumen(
            ventas_netas=3000000,
            compras_netas=1000000,
            ppm_rate=0.0025,
            retenciones_honorarios=50000,
            otros_impuestos=10000,
        )
        self.assertEqual(resultado["codigo_538_iva_debito"], 570000.0)
        self.assertEqual(resultado["codigo_511_iva_credito"], 190000.0)
        self.assertEqual(resultado["codigo_089_ppm"], 7500.0)
        self.assertEqual(resultado["codigo_151_ret_honorarios"], 50000.0)
        self.assertEqual(resultado["total_estimado_pagar"], 447500.0)

    def test_calcular_f22_persona_honorarios(self) -> None:
        resultado = calcular_f22_estimado(
            perfil="persona_honorarios",
            ingresos=10000000,
            honorarios_retencion=500000,
        )
        self.assertEqual(resultado["base_imponible"], 7000000.0)
        self.assertEqual(resultado["creditos"], 500000.0)

    def test_calcular_f22_persona_completo_presunto(self) -> None:
        resultado = calcular_f22_persona_completo(
            honorarios_brutos=10000000,
            remuneraciones_afectas=5000000,
            iusc_retenido=200000,
            otras_rentas_afectas=1000000,
            gastos_modo="presunto",
            apv_rebaja=500000,
            retenciones_honorarios=700000,
            ppm_pagado=100000,
        )
        self.assertEqual(resultado["gasto_rebajable"], 3000000.0)
        self.assertEqual(resultado["honorarios_netos"], 7000000.0)
        self.assertEqual(resultado["base_imponible"], 12500000.0)
        self.assertEqual(resultado["creditos"], 1000000.0)

    def test_calcular_f22_empresa_propyme_general(self) -> None:
        resultado = calcular_f22_estimado(
            perfil="empresa_propyme_general",
            ingresos=12000000,
            costos_gastos=5000000,
            ppm_pagado=200000,
        )
        self.assertEqual(resultado["base_imponible"], 7000000.0)
        self.assertEqual(resultado["impuesto_estimado"], 875000.0)
        self.assertEqual(resultado["saldo_estimado"], 675000.0)

    def test_get_balance_general_structure(self) -> None:
        global DB_PATH
        original_db_path = DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                DB_PATH = Path(tmp_dir) / "test_tributapp.db"
                init_db()
                user_id = create_user("Octavio", "oct@test.com", "123456")
                empresa_id = create_empresa("INY SpA", "77801453-K", owner_user_id=user_id)
                balance = get_balance_general(empresa_id)
                self.assertIn("activos", balance)
                self.assertIn("pasivos", balance)
                self.assertIn("patrimonio", balance)
        finally:
            DB_PATH = original_db_path


# Inicializar base también cuando Gunicorn importa el módulo en producción
init_db()


if __name__ == "__main__":
    if "--test" in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    elif "--runserver" in sys.argv:
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
    else:
        print("Base de datos inicializada en:", DB_PATH)
        print("Para ejecutar pruebas: python tributapp_app.py --test")
        print("Para levantar la web: python tributapp_app.py --runserver")
