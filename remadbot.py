#!/usr/bin/env python3
"""
remadbot.py — Bot de Telegram para monitorizar el catálogo de ReMAD
https://remad.madrid.es/REMAD_FTP/#/catalogo

Detecta artículos nuevos con reserved=False que coincidan con las
palabras clave configuradas y envía una notificación por Telegram.

Opcionalmente intenta autoreservar artículos que coincidan con
AUTORESERVE_KEYWORDS, completando el SMS de verificación
automáticamente (vía webhook Tasker) o manualmente (vía Telegram).

Watchlist: monitoriza artículos concretos 24/7 (por hash o URL) y
activa autoreserva en cuanto pasan de reservado → disponible.
"""

import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from random import uniform

import requests
from dotenv import load_dotenv

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.WARNING,          # sobreescrito en main() tras leer .env
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("remadbot")

# ── Endpoints API ─────────────────────────────────────────────────────────────

API_BASE   = "https://remad.madrid.es/REMAD_RSP/api/v1"
CATALOG_EP = f"{API_BASE}/antiquities/catalog"
DETAIL_EP  = f"{API_BASE}/antiquities"       # GET /antiquities/{hash}
IMAGE_BASE = f"{API_BASE}/files/download"
AUTH_EP    = f"{API_BASE}/auth/login"
PHONE_EP   = f"{API_BASE}/users/phone/generate"
RESERVE_EP = f"{API_BASE}/antiquities/reservations"
ITEM_URL   = "https://remad.madrid.es/REMAD_FTP/#/detalleAntique/{hash}"

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept":       "application/json",
}

# ── Sesión HTTP persistente ───────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update(HEADERS)

# ── Estado compartido para SMS (entre hilo Flask y hilo principal) ────────────

_sms_lock  = threading.Lock()
_sms_event = threading.Event()
_sms_code: str | None = None

# ── Offset de Telegram updates ────────────────────────────────────────────────

_tg_offset: int = 0

# ── Estado de la watchlist (hash → último estado reserved conocido) ───────────

_watchlist_estado: dict[str, bool | None] = {}


# ── Modelos ───────────────────────────────────────────────────────────────────

@dataclass
class Articulo:
    hash:        str
    nombre:      str
    location:    str
    categoria:   str
    reserved:    bool
    created_at:  str
    file_name:   str | None

    @property
    def disponible(self) -> bool:
        return not self.reserved

    @property
    def url(self) -> str:
        return ITEM_URL.format(hash=self.hash)

    @property
    def imagen_url(self) -> str | None:
        if self.file_name:
            return f"{IMAGE_BASE}/{self.file_name}"
        return None

    def coincide_keywords(self, keywords: list[str]) -> bool:
        texto = (self.nombre + " " + self.categoria + " " + self.location).lower()
        return any(kw.lower() in texto for kw in keywords)

    def __repr__(self) -> str:
        estado = "DISPONIBLE" if self.disponible else "RESERVADO"
        return f"[{estado}] {self.nombre!r} en {self.location!r}"


def parsear_articulo(raw: dict) -> Articulo:
    return Articulo(
        hash=raw.get("hash", ""),
        nombre=raw.get("name", "Sin nombre"),
        location=raw.get("location", ""),
        categoria=raw.get("category", {}).get("name", ""),
        reserved=raw.get("reserved", True),
        created_at=raw.get("createdAt", ""),
        file_name=(raw.get("file") or {}).get("name"),
    )


# ── Horario de apertura ───────────────────────────────────────────────────────

def en_horario_apertura(festivos_file: Path) -> bool:
    """
    Devuelve True si ahora es horario de operación de los Puntos Limpios.

      Lunes–viernes : 15:00 – 20:00
      Sábados       : 08:00 – 20:00
      Domingos y festivos : 09:00 – 14:00
    """
    now = datetime.now()
    dow = now.isoweekday()          # 1=lun … 6=sáb … 7=dom
    hora_min = now.hour * 60 + now.minute
    hoy = now.strftime("%Y-%m-%d")

    es_festivo = False
    if festivos_file.exists():
        try:
            festivos = {l.strip() for l in festivos_file.read_text().splitlines() if l.strip()}
            es_festivo = hoy in festivos
        except OSError:
            pass

    if es_festivo or dow == 7:      # domingo / festivo: 09:00–14:00
        return 540 <= hora_min < 840
    elif dow == 6:                   # sábado: 08:00–20:00
        return 480 <= hora_min < 1200
    else:                            # lunes–viernes: 15:00–20:00
        return 900 <= hora_min < 1200


# ── Acceso al catálogo ────────────────────────────────────────────────────────

PAGE_SIZE = 9
MAX_PAGES = 50


def buscar_articulos(texto: str | None, disponible: bool | None = None) -> list[Articulo]:
    """Itera todas las páginas del catálogo con los filtros dados."""
    articulos: list[Articulo] = []
    page = 0

    while page < MAX_PAGES:
        for intento in range(2):
            try:
                resp = _session.post(
                    f"{CATALOG_EP}?userHash=&pageIndex={page}",
                    json={"text": texto, "disponible": disponible, "orderBy": None},
                    timeout=20,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if intento == 0:
                    log.warning(f"Error en página {page}, reintentando en 2s... ({e})")
                    time.sleep(2)
                else:
                    log.error(f"Error definitivo en página {page}: {e}")
                    return articulos
        else:
            return articulos

        batch = resp.json()
        if not batch:
            break

        articulos.extend(parsear_articulo(r) for r in batch)
        log.info(f"   página {page}: {len(batch)} artículos, acumulados={len(articulos)}")

        if len(batch) < PAGE_SIZE:
            break

        page += 1
        time.sleep(0.5)

    return articulos


# ── Watchlist ─────────────────────────────────────────────────────────────────

def cargar_watchlist(ruta: Path) -> list[str]:
    """
    Lee watchlist.txt y devuelve lista de hashes.
    Acepta tanto hashes directos como URLs completas del tipo:
      https://remad.madrid.es/REMAD_FTP/#/detalleAntique/{hash}
    Las líneas vacías y las que empiezan por # se ignoran.
    """
    if not ruta.exists():
        return []
    hashes = []
    for line in ruta.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # URL: extraer la parte final tras la última /
        h = line.rstrip("/").split("/")[-1] if "/" in line else line
        if h:
            hashes.append(h)
    return hashes


def obtener_articulo(art_hash: str) -> Articulo | None:
    """
    GET /antiquities/{hash} — devuelve el Articulo o None si no existe/error.
    No requiere autenticación.
    """
    try:
        resp = _session.get(f"{DETAIL_EP}/{art_hash}", timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parsear_articulo(resp.json())
    except Exception as e:
        log.warning(f"   Error consultando artículo {art_hash[:12]}…: {e}")
        return None


def ciclo_watchlist(config: dict) -> None:
    """
    Comprueba el estado de cada artículo en watchlist.txt.
    Si alguno pasa de reservado → disponible, notifica y (si procede) autoreserva.
    Se ejecuta 24/7, independientemente del horario de apertura.
    """
    global _watchlist_estado

    watchlist = cargar_watchlist(config["watchlist_file"])
    if not watchlist:
        return

    log.info(f"👁️  Watchlist: comprobando {len(watchlist)} artículo(s)...")

    for art_hash in watchlist:
        art = obtener_articulo(art_hash)
        if art is None:
            log.info(f"   {art_hash[:12]}… — no encontrado / inactivo")
            _watchlist_estado.pop(art_hash, None)
            continue

        estado_anterior = _watchlist_estado.get(art_hash)  # None = primera vez
        _watchlist_estado[art_hash] = art.reserved

        estado_str = "RESERVADO" if art.reserved else "DISPONIBLE"
        log.info(f"   {art.nombre!r} — {estado_str}")

        if estado_anterior is None:
            # Primera vez que vemos este artículo
            if not art.reserved:
                log.warning(f"🎯 Watchlist: {art.nombre!r} ya está DISPONIBLE")
                _notificar_watchlist(config, art)
                if _debe_autoreservar(config, art):
                    intentar_autoreserva(config, art)
        elif estado_anterior is True and not art.reserved:
            # Transición: reservado → disponible
            log.warning(f"🎯 Watchlist: {art.nombre!r} ¡LIBERADO!")
            _notificar_watchlist(config, art)
            if _debe_autoreservar(config, art):
                intentar_autoreserva(config, art)

        time.sleep(0.3)


def _notificar_watchlist(config: dict, art: Articulo) -> None:
    """Envía notificación especial de watchlist a Telegram."""
    lines = [
        "🎯 *¡Artículo de tu watchlist DISPONIBLE\\!*",
        "",
        f"📦 [{esc_md(art.nombre)}]({art.url})",
        f"📍 {esc_md(art.location)}",
        f"🏷️ {esc_md(art.categoria)}",
    ]
    tg_token = config["token"]
    chat_id  = config["chat_id"]
    mensaje  = "\n".join(lines)

    if art.imagen_url:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendPhoto",
                data={"chat_id": chat_id, "photo": art.imagen_url,
                      "caption": mensaje, "parse_mode": "MarkdownV2"},
                timeout=20,
            )
            if r.ok:
                return
        except Exception:
            pass
    _enviar_texto(tg_token, chat_id, mensaje)


def _debe_autoreservar(config: dict, art: Articulo) -> bool:
    """True si el artículo cumple las condiciones para autoreserva."""
    return (
        config["autoreserve_enabled"]
        and bool(config["autoreserve_kws"])
        and art.coincide_keywords(config["autoreserve_kws"])
    )


# ── Persistencia ──────────────────────────────────────────────────────────────

def cargar_hashes_enviados(ruta: Path) -> set[str]:
    if not ruta.exists():
        return set()
    return {line.strip() for line in ruta.read_text().splitlines() if line.strip()}


def guardar_hash_enviado(ruta: Path, hash_: str) -> None:
    with ruta.open("a") as f:
        f.write(f"{hash_}\n")


# ── Telegram helpers ──────────────────────────────────────────────────────────

def esc_md(t: str | None) -> str:
    """Escapa caracteres especiales para MarkdownV2 de Telegram."""
    if not t:
        return ""
    for c in r"_*[]()~`>#+-=|{}.!":
        t = t.replace(c, "\\" + c)
    return t


def _enviar_texto(tg_token: str, chat_id: str, texto: str) -> bool:
    """Envía un mensaje de texto plano (MarkdownV2) a Telegram."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            data={"chat_id": chat_id, "text": texto, "parse_mode": "MarkdownV2"},
            timeout=20,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Error enviando texto Telegram: {e}")
        return False


def construir_mensaje(art: Articulo) -> str:
    lines = [
        "🆕 *Nuevo artículo en ReMAD*",
        "",
        f"📦 [{esc_md(art.nombre)}]({art.url})",
        f"📍 {esc_md(art.location)}",
        f"🏷️ {esc_md(art.categoria)}",
    ]
    return "\n".join(lines)


def enviar_telegram(token: str, chat_id: str, art: Articulo) -> bool:
    """Envía notificación de artículo (con foto si está disponible)."""
    api = f"https://api.telegram.org/bot{token}"
    mensaje = construir_mensaje(art)

    if art.imagen_url:
        try:
            r = requests.post(
                f"{api}/sendPhoto",
                data={"chat_id": chat_id, "photo": art.imagen_url,
                      "caption": mensaje, "parse_mode": "MarkdownV2"},
                timeout=20,
            )
            if r.ok:
                return True
            log.warning(f"sendPhoto falló ({r.status_code}): {r.text[:100]}")
        except requests.RequestException as e:
            log.warning(f"Error enviando foto: {e}")

    # Fallback: solo texto
    try:
        r = requests.post(
            f"{api}/sendMessage",
            data={"chat_id": chat_id, "text": mensaje, "parse_mode": "MarkdownV2"},
            timeout=20,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Error enviando mensaje: {e}")
        return False


def _obtener_updates(tg_token: str, offset: int, timeout: int = 3) -> list:
    """Llama a getUpdates de Telegram y devuelve la lista de updates."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{tg_token}/getUpdates",
            params={"offset": offset, "timeout": timeout,
                    "allowed_updates": ["message"]},
            timeout=timeout + 5,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def _drenar_updates(tg_token: str) -> None:
    """Consume todos los updates pendientes para ignorar mensajes antiguos."""
    global _tg_offset
    updates = _obtener_updates(tg_token, offset=_tg_offset, timeout=0)
    if updates:
        _tg_offset = updates[-1]["update_id"] + 1


# ── Webhook SMS (Tasker) ──────────────────────────────────────────────────────

def _iniciar_webhook(port: int) -> None:
    """
    Arranca un mini-servidor Flask en un hilo daemon.
    Tasker debe hacer: POST http://<IP-PI>:<port>/sms
    con body JSON: {"code": "123456"}
    o body de texto libre que contenga el código numérico.
    """
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        log.warning(
            "Flask no instalado — webhook Tasker desactivado. "
            "Instala con: pip install flask"
        )
        return

    app = Flask(__name__)
    # Silenciar los logs de werkzeug
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.route("/sms", methods=["POST"])
    def recibir_sms():
        global _sms_code
        data = request.get_json(silent=True) or {}
        # Acepta {"code": "123456"} o {"text": "Tu código ReMAD es 123456"} o body libre
        texto = (
            data.get("code")
            or data.get("text")
            or data.get("body")
            or request.get_data(as_text=True)
            or ""
        )
        m = re.search(r'\b(\d{4,8})\b', str(texto))
        if m:
            with _sms_lock:
                _sms_code = m.group(1)
            _sms_event.set()
            log.warning(f"   📡 Código SMS recibido vía webhook Tasker")
            return jsonify({"ok": True}), 200
        return jsonify({"ok": False, "error": "No se encontró código numérico"}), 400

    hilo = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="sms-webhook",
    )
    hilo.start()
    log.warning(f"📡 Webhook SMS escuchando en :{port}/sms")


# ── Espera del código SMS ─────────────────────────────────────────────────────

def _esperar_codigo_sms(tg_token: str, chat_id: str, timeout_s: int) -> str | None:
    """
    Espera un código SMS de 4-8 dígitos con dos fuentes en paralelo:
      1. Webhook Tasker (hilo Flask ya corriendo si SMS_WEBHOOK_PORT está configurado)
      2. Respuesta manual del usuario por Telegram
    Devuelve el código o None si se agota el timeout.
    """
    global _sms_code, _tg_offset

    # Resetear estado del webhook
    _sms_event.clear()
    with _sms_lock:
        _sms_code = None

    # Ignorar mensajes de Telegram anteriores al inicio de la espera
    _drenar_updates(tg_token)

    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        # 1. Comprobar webhook Tasker (no bloquea)
        if _sms_event.is_set():
            with _sms_lock:
                codigo = _sms_code
                _sms_code = None
            _sms_event.clear()
            return codigo

        # 2. Comprobar respuesta manual por Telegram
        updates = _obtener_updates(tg_token, offset=_tg_offset, timeout=2)
        for upd in updates:
            _tg_offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            if str(msg.get("chat", {}).get("id")) == str(chat_id):
                texto = msg.get("text", "").strip()
                if re.match(r'^\d{4,8}$', texto):
                    log.warning("   📱 Código SMS recibido vía Telegram manual")
                    return texto

        time.sleep(1)

    return None  # timeout


# ── ReMAD auth y reserva ──────────────────────────────────────────────────────

def _login_remad(email: str, password: str) -> dict:
    """
    Login en ReMAD. Devuelve dict con token, user_hash, email y phone.
    El accessToken caduca en ~15 min — hacer login justo antes de reservar.
    """
    resp = _session.post(
        AUTH_EP,
        json={"email": email, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "token":     data["accessToken"],
        "user_hash": data["profile"]["user"]["hash"],
        "email":     data["profile"]["user"]["email"],
        "phone":     data["profile"]["phone"],
    }


def _generar_sms_reserva(creds: dict) -> None:
    """
    Dispara el envío del SMS de verificación para una reserva.
    Lanza RuntimeError si el SMS está bloqueado por demasiados intentos.
    """
    r = _session.post(
        PHONE_EP,
        json={"email": creds["email"], "phone": creds["phone"], "type": "RESERVATION"},
        headers={"Authorization": f"Bearer {creds['token']}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("codeGenerationBlocked"):
        intentos = data.get("remainingAttempts", "?")
        raise RuntimeError(f"SMS bloqueado — intentos restantes: {intentos}")


def _confirmar_reserva(creds: dict, art_hash: str, codigo: str) -> dict:
    """
    Envía el código SMS y confirma la reserva.
    Devuelve el JSON de respuesta completo.
    """
    r = _session.post(
        f"{RESERVE_EP}?userHash={creds['user_hash']}",
        json={
            "antiquity":      {"hash": art_hash},
            "verifySmsCode":  codigo,
            "verifySmsEmail": creds["email"],
            "verifySmsPhone": creds["phone"],
        },
        headers={"Authorization": f"Bearer {creds['token']}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def intentar_autoreserva(config: dict, art: Articulo) -> None:
    """Orquesta el flujo completo de autoreserva para un artículo."""
    tg_token = config["token"]
    chat_id  = config["chat_id"]
    timeout  = config["sms_timeout"]

    log.warning(f"🔒 Iniciando autoreserva: {art.nombre!r}")

    # 1. Login
    try:
        creds = _login_remad(config["remad_email"], config["remad_pass"])
    except Exception as e:
        log.error(f"   Login ReMAD fallido: {e}")
        _enviar_texto(tg_token, chat_id,
                      f"❌ Autoreserva fallida \\(login\\): {esc_md(str(e))}")
        return

    # 2. Generar SMS
    try:
        _generar_sms_reserva(creds)
    except Exception as e:
        log.error(f"   Error generando SMS: {e}")
        _enviar_texto(tg_token, chat_id,
                      f"❌ No se pudo enviar SMS: {esc_md(str(e))}")
        return

    log.warning(f"   📱 SMS enviado — esperando código ({timeout}s)...")
    _enviar_texto(
        tg_token, chat_id,
        f"📱 SMS enviado para *{esc_md(art.nombre)}*\\.\n"
        f"Si no llega automáticamente, responde con el código\\. "
        f"Tienes {timeout}s\\."
    )

    # 3. Esperar código (Tasker o manual)
    codigo = _esperar_codigo_sms(tg_token, chat_id, timeout)

    if not codigo:
        log.warning("   ⏰ Timeout — sin código SMS")
        _enviar_texto(tg_token, chat_id,
                      f"⏰ Sin código SMS para *{esc_md(art.nombre)}* — reserva cancelada\\.")
        return

    log.warning(f"   Código recibido — confirmando reserva...")

    # 4. Confirmar reserva
    try:
        resp = _confirmar_reserva(creds, art.hash, codigo)
    except Exception as e:
        log.error(f"   Error confirmando reserva: {e}")
        _enviar_texto(tg_token, chat_id,
                      f"❌ Error al confirmar reserva: {esc_md(str(e))}")
        return

    ant = resp.get("antiquity", {})
    if ant.get("reserved"):
        fecha      = (resp.get("reservationDate") or "")[:10]
        cod_reserva = resp.get("code", "")
        log.warning(f"   ✅ RESERVADO: {art.nombre} — hasta {fecha}")
        _enviar_texto(
            tg_token, chat_id,
            f"✅ *¡RESERVADO\\!* [{esc_md(art.nombre)}]({art.url})\n"
            f"📅 Recoger antes del {esc_md(fecha)}\n"
            f"🔖 Código: `{esc_md(cod_reserva)}`"
        )
    else:
        log.warning(f"   😞 No reservado — ya cogido: {art.nombre}")
        _enviar_texto(
            tg_token, chat_id,
            f"😞 *Ya reservado por otro* — [{esc_md(art.nombre)}]({art.url})"
        )


# ── Configuración ─────────────────────────────────────────────────────────────

def cargar_config() -> dict:
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")

    # ── Telegram
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        log.error("Falta TELEGRAM_BOT_TOKEN en .env"); sys.exit(1)
    if not chat_id:
        log.error("Falta TELEGRAM_CHAT_ID en .env"); sys.exit(1)

    # ── Catálogo
    kw_raw   = os.getenv("KEYWORDS", "").strip()
    keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
    if not keywords:
        log.warning("Sin KEYWORDS — se notificarán TODOS los artículos disponibles")

    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
    db_file  = os.getenv("DB_FILE", "sent_hashes.txt")

    # ── Autoreserva
    autoreserve_enabled = os.getenv("AUTORESERVE_ENABLED", "false").lower() == "true"
    ar_raw   = os.getenv("AUTORESERVE", "").strip()
    ar_kws   = [k.strip() for k in ar_raw.split(",") if k.strip()]
    remad_email = os.getenv("REMAD_EMAIL", "").strip()
    remad_pass  = os.getenv("REMAD_PASS",  "").strip()
    sms_timeout = int(os.getenv("SMS_TIMEOUT", "120"))
    sms_port_raw = os.getenv("SMS_WEBHOOK_PORT", "").strip()
    sms_port = int(sms_port_raw) if sms_port_raw.isdigit() else None

    if autoreserve_enabled:
        if not remad_email or not remad_pass:
            log.error("AUTORESERVE_ENABLED=true pero falta REMAD_EMAIL o REMAD_PASS en .env")
            sys.exit(1)
        if not ar_kws:
            log.warning("AUTORESERVE_ENABLED=true pero AUTORESERVE está vacío — no se reservará nada")

    # ── Watchlist
    wl_raw = os.getenv("WATCHLIST_FILE", "watchlist.txt").strip()
    watchlist_file = Path(__file__).parent / wl_raw

    # ── Festivos (para lógica de horario interno)
    festivos_file = Path(__file__).parent / "festivos.txt"

    # ── Logging
    log_level_str = os.getenv("LOG_LEVEL", "WARNING").upper()
    log_level = getattr(logging, log_level_str, logging.WARNING)

    return {
        "token":               token,
        "chat_id":             chat_id,
        "keywords":            keywords,
        "interval":            interval,
        "db_file":             Path(__file__).parent / db_file,
        "autoreserve_enabled": autoreserve_enabled,
        "autoreserve_kws":     ar_kws,
        "remad_email":         remad_email,
        "remad_pass":          remad_pass,
        "sms_timeout":         sms_timeout,
        "sms_port":            sms_port,
        "watchlist_file":      watchlist_file,
        "festivos_file":       festivos_file,
        "log_level":           log_level,
    }


# ── Ciclo principal (catálogo) ────────────────────────────────────────────────

def ciclo(config: dict) -> None:
    log.info("⏳ Comprobando catálogo de ReMAD...")
    keywords: list[str] = config["keywords"]

    # Unión de keywords de notificación y autoreserva para la búsqueda
    todas_kws = list({*keywords, *config["autoreserve_kws"]})

    try:
        if todas_kws:
            vistos: dict[str, Articulo] = {}
            for kw in todas_kws:
                resultados = buscar_articulos(texto=kw, disponible=None)
                log.info(f"   '{kw}': {len(resultados)} resultado(s)")
                for art in resultados:
                    vistos[art.hash] = art
            todos = list(vistos.values())
        else:
            todos = buscar_articulos(texto=None, disponible=True)
    except Exception as e:
        log.error(f"Error accediendo a la API: {e}")
        return

    log.info(f"   {len(todos)} artículo(s) encontrados")

    enviados = cargar_hashes_enviados(config["db_file"])
    candidatos = [a for a in todos if a.disponible and a.hash not in enviados]

    if not candidatos:
        log.info("   Sin artículos nuevos disponibles")
        return

    log.warning(f"🔔 {len(candidatos)} artículo(s) nuevo(s)")
    for art in candidatos:
        log.warning(f"   → {art}")

        # Notificar siempre si coincide con KEYWORDS (o si no hay keywords)
        if not keywords or art.coincide_keywords(keywords):
            ok = enviar_telegram(config["token"], config["chat_id"], art)
            if ok:
                guardar_hash_enviado(config["db_file"], art.hash)
                log.warning("   ✅ Enviado a Telegram")
            else:
                log.error("   ❌ Fallo al enviar a Telegram")

        # Autoreserva si está habilitada y el artículo coincide
        if (config["autoreserve_enabled"]
                and config["autoreserve_kws"]
                and art.coincide_keywords(config["autoreserve_kws"])):
            # Guardar el hash aunque no hayamos notificado (evitar doble reserva)
            if art.hash not in enviados:
                guardar_hash_enviado(config["db_file"], art.hash)
            intentar_autoreserva(config, art)

        time.sleep(uniform(1.0, 2.0))


def main() -> None:
    config = cargar_config()
    logging.getLogger().setLevel(config["log_level"])

    kw_str = ", ".join(config["keywords"]) if config["keywords"] else "(todos)"
    ar_str = ", ".join(config["autoreserve_kws"]) if config["autoreserve_kws"] else "—"
    wl_str = str(config["watchlist_file"])
    log.warning("🤖 remadbot arrancado  [LOG_LEVEL=%s]",
                logging.getLevelName(config["log_level"]))
    log.info(f"   Keywords     : {kw_str}")
    log.info(f"   Autoreserva  : {ar_str} ({'ON' if config['autoreserve_enabled'] else 'OFF'})")
    log.info(f"   Watchlist    : {wl_str}")
    log.info(f"   Intervalo    : {config['interval']}s")
    log.info(f"   DB           : {config['db_file']}")

    # Arrancar webhook Tasker si está configurado
    if config["sms_port"]:
        _iniciar_webhook(config["sms_port"])

    while True:
        # 1. Watchlist: siempre, 24/7, independiente del horario
        ciclo_watchlist(config)

        # 2. Catálogo: solo en horario de apertura de los Puntos Limpios
        if en_horario_apertura(config["festivos_file"]):
            ciclo(config)
        else:
            log.info("🌙 Fuera de horario — catálogo pausado, watchlist activa")

        log.info(f"   💤 Esperando {config['interval']}s...")
        time.sleep(config["interval"])


if __name__ == "__main__":
    main()
