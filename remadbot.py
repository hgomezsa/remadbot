#!/usr/bin/env python3
"""
remadbot.py — Bot de Telegram para monitorizar el catálogo de ReMAD
https://remad.madrid.es/REMAD_FTP/#/catalogo

Detecta artículos nuevos con reserved=False que coincidan con las
palabras clave configuradas y envía una notificación por Telegram.
"""

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
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

# ── API ───────────────────────────────────────────────────────────────────────

API_BASE   = "https://remad.madrid.es/REMAD_RSP/api/v1"
CATALOG_EP = f"{API_BASE}/antiquities/catalog"
IMAGE_BASE = f"{API_BASE}/files/download"
ITEM_URL   = "https://remad.madrid.es/REMAD_FTP/#/detalleAntique/{hash}"

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept":       "application/json",
}


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


# ── Acceso a la API ───────────────────────────────────────────────────────────

PAGE_SIZE = 9    # artículos por página que devuelve la API
MAX_PAGES = 50   # tope de seguridad (~450 artículos)

# Sesión persistente — reutiliza la conexión TCP+SSL entre peticiones
_session = requests.Session()
_session.headers.update(HEADERS)


def buscar_articulos(texto: str | None, disponible: bool | None = None) -> list[Articulo]:
    """
    Itera todas las páginas del catálogo con los filtros dados.
    Para cuando recibe una página vacía o con menos de PAGE_SIZE artículos.
    """
    articulos: list[Articulo] = []
    page = 0

    while page < MAX_PAGES:
        # Reintento único en caso de error 5xx
        for intento in range(2):
            try:
                resp = _session.post(
                    f"{CATALOG_EP}?userHash=&pageIndex={page}",
                    json={"text": texto, "disponible": disponible, "orderBy": None},
                    timeout=20,
                )
                resp.raise_for_status()
                break  # éxito
            except requests.RequestException as e:
                if intento == 0:
                    log.warning(f"   Error en página {page}, reintentando en 2s... ({e})")
                    time.sleep(2)
                else:
                    log.error(f"   Error definitivo en página {page}: {e}")
                    return articulos  # devolver lo que tenemos
        else:
            return articulos

        batch = resp.json()
        if not batch:
            break

        articulos.extend(parsear_articulo(r) for r in batch)
        log.info(f"   página {page}: {len(batch)} artículos acumulados={len(articulos)}")

        if len(batch) < PAGE_SIZE:
            break

        page += 1
        time.sleep(0.5)  # pausa entre páginas para no saturar el servidor

    return articulos


# ── Persistencia ──────────────────────────────────────────────────────────────

def cargar_hashes_enviados(ruta: Path) -> set[str]:
    if not ruta.exists():
        return set()
    return {line.strip() for line in ruta.read_text().splitlines() if line.strip()}


def guardar_hash_enviado(ruta: Path, hash_: str) -> None:
    with ruta.open("a") as f:
        f.write(f"{hash_}\n")


# ── Telegram ──────────────────────────────────────────────────────────────────

def esc_md(t: str | None) -> str:
    """Escapa caracteres especiales para MarkdownV2 de Telegram."""
    if not t:
        return ""
    for c in r"_*[]()~`>#+-=|{}.!":
        t = t.replace(c, "\\" + c)
    return t


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
    api = f"https://api.telegram.org/bot{token}"
    mensaje = construir_mensaje(art)

    # Intentar con foto
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
            data={"chat_id": chat_id, "text": mensaje,
                  "parse_mode": "MarkdownV2"},
            timeout=20,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Error enviando mensaje: {e}")
        return False


# ── Configuración ─────────────────────────────────────────────────────────────

def cargar_config() -> dict:
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")

    token    = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    kw_raw   = os.getenv("KEYWORDS", "").strip()
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
    db_file  = os.getenv("DB_FILE", "sent_hashes.txt")

    if not token:
        log.error("Falta TELEGRAM_BOT_TOKEN en .env")
        sys.exit(1)
    if not chat_id:
        log.error("Falta TELEGRAM_CHAT_ID en .env")
        sys.exit(1)

    keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
    if not keywords:
        log.warning("Sin KEYWORDS — se notificarán TODOS los artículos disponibles")

    log_level_str = os.getenv("LOG_LEVEL", "WARNING").upper()
    log_level = getattr(logging, log_level_str, logging.WARNING)

    return {
        "token":     token,
        "chat_id":   chat_id,
        "keywords":  keywords,
        "interval":  interval,
        "db_file":   Path(db_file),
        "log_level": log_level,
    }


# ── Ciclo principal ───────────────────────────────────────────────────────────

def ciclo(config: dict) -> None:
    log.info("⏳ Comprobando catálogo de ReMAD...")
    keywords: list[str] = config["keywords"]

    try:
        if keywords:
            # Una búsqueda por keyword en el servidor, deduplicar por hash
            vistos: dict[str, Articulo] = {}
            for kw in keywords:
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

    log.info(f"   {len(todos)} artículo(s) encontrados con los filtros")

    enviados = cargar_hashes_enviados(config["db_file"])

    candidatos = [
        a for a in todos
        if a.disponible and a.hash not in enviados
    ]

    if not candidatos:
        log.info("   Sin artículos nuevos disponibles")
        return

    log.warning(f"🔔 {len(candidatos)} artículo(s) nuevo(s) para notificar")
    for art in candidatos:
        log.warning(f"   → {art}")
        ok = enviar_telegram(config["token"], config["chat_id"], art)
        if ok:
            guardar_hash_enviado(config["db_file"], art.hash)
            log.warning("   ✅ Enviado a Telegram")
        else:
            log.error("   ❌ Fallo al enviar a Telegram")
        time.sleep(uniform(1.0, 2.0))


def main() -> None:
    config = cargar_config()
    logging.getLogger().setLevel(config["log_level"])

    kw_str = ", ".join(config["keywords"]) if config["keywords"] else "(todos)"
    log.warning("🤖 remadbot arrancado  [LOG_LEVEL=%s]", logging.getLevelName(config["log_level"]))
    log.info(f"   Keywords : {kw_str}")
    log.info(f"   Intervalo: {config['interval']}s")
    log.info(f"   DB       : {config['db_file']}")

    while True:
        ciclo(config)
        log.info(f"   💤 Esperando {config['interval']}s...")
        time.sleep(config["interval"])


if __name__ == "__main__":
    main()
