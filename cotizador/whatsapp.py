"""Integración con WhatsApp Cloud API (conexión directa con Meta, sin intermediarios).

Incluye:
- verificación de la firma X-Hub-Signature-256 de cada webhook,
- lectura de mensajes entrantes (texto, foto, audio, PDF, botones),
- envío de texto, botones, documentos PDF y plantillas,
- un cliente de consola para simular sin Meta.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .modelos import MensajeEntrante

log = logging.getLogger(__name__)

LIMITE_TEXTO = 4000          # Meta permite 4096
LIMITE_TITULO_BOTON = 20
CODIGO_FUERA_DE_VENTANA = 131047  # mensaje libre fuera de la ventana de 24 h


class ErrorWhatsApp(RuntimeError):
    def __init__(self, mensaje: str, codigo: int | None = None):
        super().__init__(mensaje)
        self.codigo = codigo


# ------------------------------------------------------------------ webhook

def verificar_firma(cuerpo: bytes, firma_header: str | None, app_secret: str) -> bool:
    """Sin WA_APP_SECRET se rechaza todo: un webhook sin firma podría hacerse pasar por un administrador."""
    if not app_secret:
        log.error("WA_APP_SECRET no está configurado: se rechazan los webhooks")
        return False
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    esperado = hmac.new(app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, firma_header.split("=", 1)[1])


@dataclass
class EventoEco:
    """Mensaje que el vendedor envió desde la app WhatsApp Business (coexistencia)."""
    wa_id_cliente: str
    texto: str


def parsear_webhook(payload: dict[str, Any]) -> tuple[list[MensajeEntrante], list[EventoEco]]:
    mensajes: list[MensajeEntrante] = []
    ecos: list[EventoEco] = []
    for entrada in payload.get("entry", []) or []:
        for cambio in entrada.get("changes", []) or []:
            valor = cambio.get("value", {}) or {}
            campo = cambio.get("field", "messages")
            if campo == "smb_message_echoes":
                for eco in valor.get("message_echoes", []) or []:
                    ecos.append(EventoEco(wa_id_cliente=str(eco.get("to", "")),
                                          texto=(eco.get("text") or {}).get("body", "")))
                continue
            nombres = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in valor.get("contacts", []) or []}
            for m in valor.get("messages", []) or []:
                mensajes.append(_parsear_mensaje(m, nombres))
    return mensajes, ecos


@dataclass
class EventoFallo:
    """Meta informa de forma asíncrona (statuses) que un mensaje no se pudo entregar."""
    wa_id: str
    msg_id: str
    codigo: int | None
    detalle: str


def parsear_fallos(payload: dict[str, Any]) -> list[EventoFallo]:
    fallos: list[EventoFallo] = []
    for entrada in payload.get("entry", []) or []:
        for cambio in entrada.get("changes", []) or []:
            for estado in (cambio.get("value", {}) or {}).get("statuses", []) or []:
                if estado.get("status") != "failed":
                    continue
                error = (estado.get("errors") or [{}])[0]
                fallos.append(EventoFallo(wa_id=str(estado.get("recipient_id", "")), msg_id=str(estado.get("id", "")),
                                          codigo=error.get("code"),
                                          detalle=error.get("title") or error.get("message") or "error desconocido"))
    return fallos


def _parsear_mensaje(m: dict[str, Any], nombres: dict[str, str]) -> MensajeEntrante:
    tipo = m.get("type", "otro")
    wa_id = str(m.get("from", ""))
    base = dict(id=str(m.get("id", "")), wa_id=wa_id, nombre=nombres.get(wa_id, ""),
                timestamp=str(m.get("timestamp", "")))
    if tipo == "text":
        return MensajeEntrante(tipo="text", texto=(m.get("text") or {}).get("body", ""), **base)
    if tipo in {"image", "audio", "document", "video", "sticker"}:
        media = m.get(tipo) or {}
        return MensajeEntrante(tipo=tipo, texto=media.get("caption", ""), media_id=media.get("id", ""),
                               mime=media.get("mime_type", ""), nombre_archivo=media.get("filename", ""), **base)
    if tipo == "interactive":
        inter = m.get("interactive") or {}
        resp = inter.get("button_reply") or inter.get("list_reply") or {}
        return MensajeEntrante(tipo="interactive", texto=resp.get("title", ""), boton_id=resp.get("id", ""), **base)
    if tipo == "button":  # respuesta rápida de una plantilla
        btn = m.get("button") or {}
        return MensajeEntrante(tipo="button", texto=btn.get("text", ""), boton_id=btn.get("payload", ""), **base)
    if tipo == "location":
        loc = m.get("location") or {}
        texto = f"[Ubicación compartida: {loc.get('name') or ''} {loc.get('address') or ''} " \
                f"({loc.get('latitude')}, {loc.get('longitude')})]"
        return MensajeEntrante(tipo="location", texto=texto.strip(), **base)
    return MensajeEntrante(tipo="otro", **base)


def partir_texto(texto: str, limite: int = LIMITE_TEXTO) -> list[str]:
    texto = texto.strip()
    if len(texto) <= limite:
        return [texto] if texto else []
    partes, actual = [], ""
    for parrafo in texto.split("\n"):
        if len(actual) + len(parrafo) + 1 > limite and actual:
            partes.append(actual.rstrip())
            actual = ""
        while len(parrafo) > limite:
            partes.append(parrafo[:limite])
            parrafo = parrafo[limite:]
        actual += parrafo + "\n"
    if actual.strip():
        partes.append(actual.rstrip())
    return partes


# ------------------------------------------------------------------ envío

class ClienteWhatsApp:
    def __init__(self, config: Config, sesion: requests.Session | None = None):
        self.config = config
        self.sesion = sesion or requests.Session()
        self.base = f"https://graph.facebook.com/{config.wa_graph_version}"

    @property
    def _cabeceras(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.wa_token}"}

    def _post(self, ruta: str, **kwargs: Any) -> dict[str, Any]:
        try:
            r = self.sesion.post(f"{self.base}/{ruta}", headers=self._cabeceras, timeout=20, **kwargs)
        except requests.RequestException as exc:  # timeout, DNS, conexión cortada
            raise ErrorWhatsApp(f"WhatsApp sin respuesta: {exc}") from exc
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            raise ErrorWhatsApp(f"WhatsApp {r.status_code}: {err.get('message', r.text[:300])}", err.get("code"))
        return r.json()

    def _mensaje(self, para: str, cuerpo: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"{self.config.wa_phone_number_id}/messages",
                          json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": para, **cuerpo})

    def enviar_texto(self, para: str, texto: str) -> None:
        for parte in partir_texto(texto):
            self._mensaje(para, {"type": "text", "text": {"body": parte, "preview_url": False}})

    def enviar_botones(self, para: str, texto: str, botones: list[tuple[str, str]]) -> None:
        self._mensaje(para, {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": texto[:1024]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": bid[:256], "title": titulo[:LIMITE_TITULO_BOTON]}}
                    for bid, titulo in botones[:3]
                ]},
            },
        })

    def enviar_documento(self, para: str, datos: bytes, nombre_archivo: str, leyenda: str = "") -> None:
        media_id = self.subir_media(datos, "application/pdf", nombre_archivo)
        self._mensaje(para, {"type": "document",
                             "document": {"id": media_id, "filename": nombre_archivo, "caption": leyenda[:1024]}})

    def enviar_plantilla(self, para: str, nombre: str, parametros: list[str]) -> None:
        self._mensaje(para, {
            "type": "template",
            "template": {
                "name": nombre,
                "language": {"code": self.config.wa_plantilla_idioma},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in parametros]}],
            },
        })

    def subir_media(self, datos: bytes, mime: str, nombre_archivo: str) -> str:
        r = self._post(f"{self.config.wa_phone_number_id}/media",
                       data={"messaging_product": "whatsapp", "type": mime},
                       files={"file": (nombre_archivo, datos, mime)})
        return r["id"]

    def descargar_media(self, media_id: str) -> tuple[bytes, str]:
        try:
            info = self.sesion.get(f"{self.base}/{media_id}", headers=self._cabeceras, timeout=20)
            info.raise_for_status()
            datos = info.json()
            archivo = self.sesion.get(datos["url"], headers=self._cabeceras, timeout=60)
            archivo.raise_for_status()
        except requests.RequestException as exc:
            raise ErrorWhatsApp(f"No se pudo descargar el archivo {media_id}: {exc}") from exc
        return archivo.content, datos.get("mime_type", "")

    def marcar_leido(self, msg_id: str, escribiendo: bool = True) -> None:
        """Doble check azul + indicador 'escribiendo...' mientras el agente piensa."""
        cuerpo: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read", "message_id": msg_id}
        if escribiendo:
            cuerpo["typing_indicator"] = {"type": "text"}
        try:
            self._post(f"{self.config.wa_phone_number_id}/messages", json=cuerpo)
        except ErrorWhatsApp as exc:  # no es crítico
            log.info("No se pudo marcar como leído: %s", exc)


class ClienteConsola:
    """Imita al ClienteWhatsApp imprimiendo en pantalla y guardando los PDFs en disco."""

    def __init__(self, carpeta: str | Path = "salida/enviados", imprimir: bool = True):
        self.carpeta = Path(carpeta)
        self.imprimir = imprimir
        self.enviados: list[dict[str, Any]] = []
        self.medias: dict[str, tuple[bytes, str]] = {}

    def _registrar(self, para: str, tipo: str, **datos: Any) -> None:
        self.enviados.append({"para": para, "tipo": tipo, **datos})

    def enviar_texto(self, para: str, texto: str) -> None:
        self._registrar(para, "texto", texto=texto)
        if self.imprimir:
            print(f"\n🤖 → {para}:\n{texto}\n")

    def enviar_botones(self, para: str, texto: str, botones: list[tuple[str, str]]) -> None:
        self._registrar(para, "botones", texto=texto, botones=botones)
        if self.imprimir:
            opciones = "  ".join(f"[{t}] (responder: {b})" for b, t in botones)
            print(f"\n🤖 → {para} (botones):\n{texto}\n{opciones}\n")

    def enviar_documento(self, para: str, datos: bytes, nombre_archivo: str, leyenda: str = "") -> None:
        self.carpeta.mkdir(parents=True, exist_ok=True)
        ruta = self.carpeta / f"{para}_{nombre_archivo}"
        ruta.write_bytes(datos)
        self._registrar(para, "documento", archivo=str(ruta), leyenda=leyenda)
        if self.imprimir:
            print(f"\n📄 → {para}: {nombre_archivo} guardado en {ruta}\n{leyenda}\n")

    def enviar_plantilla(self, para: str, nombre: str, parametros: list[str]) -> None:
        self._registrar(para, "plantilla", nombre=nombre, parametros=parametros)

    def descargar_media(self, media_id: str) -> tuple[bytes, str]:
        return self.medias[media_id]

    def marcar_leido(self, msg_id: str, escribiendo: bool = True) -> None:
        pass
