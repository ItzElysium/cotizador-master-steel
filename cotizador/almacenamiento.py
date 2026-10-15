"""Persistencia serverless.

- RepositorioTablas: Azure Table Storage (pago por uso, centavos al mes).
  Es compatible con Azure Cosmos DB for Table si luego se necesita más escala:
  solo cambia la cadena de conexión.
- ArchivosBlob: PDFs de cotizaciones en Azure Blob Storage.
- ColaAzure: Azure Queue Storage para procesar mensajes fuera del webhook.

Cada clase tiene su gemela en memoria/disco local para pruebas y para el
simulador de chat, de modo que el flujo completo corre sin Azure.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .modelos import Conversacion, Cotizacion, MensajeEntrante, Producto, a_iso, ahora_utc, de_iso

log = logging.getLogger(__name__)

_TROZO = 30000  # caracteres por propiedad (límite de Table Storage: 64 KB por propiedad)
_TROZOS_MAX = 20


def clave_segura(texto: str) -> str:
    """Las claves de Table Storage no admiten / \\ # ?; los wamid sí pueden traerlos."""
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()


def _partir(prefijo: str, texto: str) -> dict[str, str]:
    partes = [texto[i:i + _TROZO] for i in range(0, len(texto), _TROZO)] or [""]
    if len(partes) > _TROZOS_MAX:
        raise ValueError(f"{prefijo} demasiado grande para almacenar")
    datos = {f"{prefijo}_{i}": p for i, p in enumerate(partes)}
    datos[f"{prefijo}_n"] = len(partes)
    return datos


def _unir(prefijo: str, entidad: dict[str, Any]) -> str:
    n = int(entidad.get(f"{prefijo}_n", 0) or 0)
    return "".join(entidad.get(f"{prefijo}_{i}", "") for i in range(n))


# =================================================================== memoria

class RepositorioMemoria:
    """Implementación en memoria (tests, simulador local)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.procesados: set[str] = set()
        self.pendientes: dict[str, dict[str, MensajeEntrante]] = {}
        self.ultimos: dict[str, str] = {}
        self.conversaciones: dict[str, Conversacion] = {}
        self.productos: dict[str, Producto] = {}
        self.cotizaciones: dict[str, Cotizacion] = {}
        self.config: dict[str, dict[str, Any]] = {}
        self.contadores: dict[str, int] = {}
        self.candados: dict[str, tuple[str, datetime]] = {}
        self.modos: dict[str, tuple[str, str]] = {}
        self.entrantes: dict[str, str] = {}

    def mensaje_registrado(self, msg_id: str) -> bool:
        return msg_id in self.procesados

    def registrar_mensaje(self, msg_id: str) -> None:
        self.procesados.add(msg_id)

    def adquirir_candado(self, wa_id: str, segundos: int, ahora: datetime) -> str | None:
        with self._lock:
            actual = self.candados.get(wa_id)
            if actual and actual[1] > ahora:
                return None
            token = uuid.uuid4().hex
            self.candados[wa_id] = (token, ahora + timedelta(seconds=segundos))
            return token

    def liberar_candado(self, wa_id: str, token: str) -> None:
        with self._lock:
            if self.candados.get(wa_id, ("",))[0] == token:
                self.candados.pop(wa_id, None)

    def obtener_modo(self, wa_id: str) -> tuple[str, str]:
        return self.modos.get(wa_id, ("bot", ""))

    def fijar_modo(self, wa_id: str, modo: str, hasta: str = "") -> None:
        self.modos[wa_id] = (modo, hasta)

    def marcar_entrante(self, wa_id: str, cuando: str) -> None:
        self.entrantes[wa_id] = cuando

    def obtener_entrante(self, wa_id: str) -> str:
        return self.entrantes.get(wa_id, "")

    def guardar_pendiente(self, m: MensajeEntrante) -> None:
        with self._lock:
            self.pendientes.setdefault(m.wa_id, {})[m.id] = m

    def listar_pendientes(self, wa_id: str) -> list[MensajeEntrante]:
        with self._lock:
            return sorted(self.pendientes.get(wa_id, {}).values(), key=lambda m: (m.timestamp, m.id))

    def borrar_pendientes(self, wa_id: str, ids: list[str]) -> None:
        with self._lock:
            for i in ids:
                self.pendientes.get(wa_id, {}).pop(i, None)

    def fijar_ultimo(self, wa_id: str, msg_id: str) -> None:
        self.ultimos[wa_id] = msg_id

    def obtener_ultimo(self, wa_id: str) -> str:
        return self.ultimos.get(wa_id, "")

    def obtener_conversacion(self, wa_id: str) -> Conversacion | None:
        conv = self.conversaciones.get(wa_id)
        if not conv:
            return None
        copia = Conversacion(**json.loads(json.dumps(conv.__dict__)))
        copia.modo, copia.humano_hasta = self.obtener_modo(wa_id)
        return copia

    def guardar_conversacion(self, conv: Conversacion) -> None:
        """No guarda el modo bot/humano: ese dato vive aparte (fijar_modo) para que un turno
        largo del agente no pise una pausa puesta por el vendedor mientras tanto."""
        conv.actualizado = a_iso(ahora_utc())
        self.conversaciones[conv.wa_id] = Conversacion(**json.loads(json.dumps(conv.__dict__)))

    def listar_productos(self) -> list[Producto]:
        return list(self.productos.values())

    def guardar_productos(self, productos: list[Producto], reemplazar: bool = False) -> None:
        if reemplazar:
            self.productos = {}
        for p in productos:
            self.productos[p.sku.upper()] = p

    def siguiente_correlativo(self, nombre: str) -> int:
        with self._lock:
            self.contadores[nombre] = self.contadores.get(nombre, 0) + 1
            return self.contadores[nombre]

    def guardar_cotizacion(self, cot: Cotizacion) -> None:
        self.cotizaciones[cot.id] = Cotizacion.de_json(cot.a_json())

    def obtener_cotizacion(self, cot_id: str) -> Cotizacion | None:
        cot = self.cotizaciones.get(cot_id.upper())
        return Cotizacion.de_json(cot.a_json()) if cot else None

    def listar_cotizaciones(self) -> list[Cotizacion]:
        return sorted(self.cotizaciones.values(), key=lambda c: c.fecha)

    def obtener_config(self, clave: str) -> dict[str, Any] | None:
        return dict(self.config[clave]) if clave in self.config else None

    def guardar_config(self, clave: str, valor: dict[str, Any]) -> None:
        self.config[clave] = dict(valor)


class ArchivosLocales:
    def __init__(self, base: str | Path = "salida"):
        self.base = Path(base)

    def guardar(self, ruta: str, datos: bytes, content_type: str = "application/octet-stream") -> str:
        destino = self.base / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(datos)
        return ruta

    def leer(self, ruta: str) -> bytes:
        return (self.base / ruta).read_bytes()


class ColaLista:
    """Cola en memoria. Si se pasa `al_recibir`, procesa al instante (simulador)."""

    def __init__(self, al_recibir: Callable[[dict[str, Any]], None] | None = None):
        self.mensajes: list[tuple[dict[str, Any], int]] = []
        self.al_recibir = al_recibir

    def enviar(self, mensaje: dict[str, Any], retraso_segundos: int = 0) -> None:
        self.mensajes.append((mensaje, retraso_segundos))
        if self.al_recibir:
            self.al_recibir(mensaje)


# =================================================================== Azure

def _credencial():
    from azure.identity import DefaultAzureCredential

    # En Azure usa la identidad administrada (AZURE_CLIENT_ID); en local, `az login`.
    return DefaultAzureCredential()


class RepositorioTablas:
    TABLAS = ["procesados", "pendientes", "control", "conversaciones", "productos",
              "cotizaciones", "config", "contadores"]

    def __init__(self, config: Config):
        from azure.data.tables import TableServiceClient

        if config.storage_connection_string:
            servicio = TableServiceClient.from_connection_string(config.storage_connection_string)
        else:
            servicio = TableServiceClient(
                endpoint=f"https://{config.storage_account_name}.table.core.windows.net",
                credential=_credencial(),
            )
        from azure.core.exceptions import HttpResponseError

        self.t = {}
        for nombre in self.TABLAS:
            real = f"{config.prefijo_tablas}{nombre}"  # prefijo = varios clientes en una misma cuenta
            try:
                servicio.create_table_if_not_exists(real)
            except HttpResponseError as exc:  # sin permiso de crear: se asume creada por la infraestructura
                log.warning("No se pudo verificar/crear la tabla %s: %s", real, exc.message)
            self.t[nombre] = servicio.get_table_client(real)

    # ---- deduplicación de webhooks (Meta puede reenviar el mismo mensaje)
    def mensaje_registrado(self, msg_id: str) -> bool:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self.t["procesados"].get_entity("msg", clave_segura(msg_id))
            return True
        except ResourceNotFoundError:
            return False

    def registrar_mensaje(self, msg_id: str) -> None:
        self.t["procesados"].upsert_entity({"PartitionKey": "msg", "RowKey": clave_segura(msg_id),
                                            "wamid": msg_id[:500], "recibido": a_iso(ahora_utc())})

    # ---- candado por cliente: un solo turno del agente a la vez por conversación
    def adquirir_candado(self, wa_id: str, segundos: int, ahora: datetime) -> str | None:
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        tabla = self.t["control"]
        token = uuid.uuid4().hex
        entidad = {"PartitionKey": wa_id, "RowKey": "candado", "token": token,
                   "expira": a_iso(ahora + timedelta(seconds=segundos))}
        try:
            tabla.create_entity(entidad)
            return token
        except ResourceExistsError:
            pass
        try:
            actual = tabla.get_entity(wa_id, "candado")
        except ResourceNotFoundError:
            return None  # se liberó justo ahora; el reintento lo tomará
        expira = de_iso(actual.get("expira"))
        if expira and expira > ahora:
            return None
        try:  # candado vencido (un proceso murió): se toma con concurrencia optimista
            tabla.update_entity(entidad, mode=UpdateMode.REPLACE, etag=actual.metadata["etag"],
                                match_condition=MatchConditions.IfNotModified)
            return token
        except HttpResponseError:
            return None

    def liberar_candado(self, wa_id: str, token: str) -> None:
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError

        tabla = self.t["control"]
        try:
            actual = tabla.get_entity(wa_id, "candado")
            if actual.get("token") == token:
                tabla.delete_entity(wa_id, "candado", etag=actual.metadata["etag"],
                                    match_condition=MatchConditions.IfNotModified)
        except HttpResponseError:
            pass

    # ---- modo bot/humano y última vez que escribió (ventana de 24 h)
    def obtener_modo(self, wa_id: str) -> tuple[str, str]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            e = self.t["control"].get_entity(wa_id, "modo")
            return e.get("modo", "bot"), e.get("hasta", "")
        except ResourceNotFoundError:
            return "bot", ""

    def fijar_modo(self, wa_id: str, modo: str, hasta: str = "") -> None:
        self.t["control"].upsert_entity({"PartitionKey": wa_id, "RowKey": "modo", "modo": modo, "hasta": hasta})

    def marcar_entrante(self, wa_id: str, cuando: str) -> None:
        self.t["control"].upsert_entity({"PartitionKey": wa_id, "RowKey": "entrante", "fecha": cuando})

    def obtener_entrante(self, wa_id: str) -> str:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.t["control"].get_entity(wa_id, "entrante").get("fecha", "")
        except ResourceNotFoundError:
            return ""

    # ---- mensajes pendientes de responder (se agrupan por cliente)
    def guardar_pendiente(self, m: MensajeEntrante) -> None:
        orden = f"{int(m.timestamp or 0):012d}-{clave_segura(m.id)[:16]}"
        self.t["pendientes"].upsert_entity({
            "PartitionKey": m.wa_id, "RowKey": orden, "datos": json.dumps(m.a_dict(), ensure_ascii=False),
        })

    def listar_pendientes(self, wa_id: str) -> list[MensajeEntrante]:
        filas = self.t["pendientes"].query_entities("PartitionKey eq @pk", parameters={"pk": wa_id})
        filas = sorted(filas, key=lambda e: e["RowKey"])
        return [MensajeEntrante.de_dict(json.loads(e["datos"])) for e in filas]

    def borrar_pendientes(self, wa_id: str, ids: list[str]) -> None:
        objetivos = {clave_segura(i)[:16] for i in ids}
        filas = self.t["pendientes"].query_entities("PartitionKey eq @pk", parameters={"pk": wa_id})
        for e in filas:
            if e["RowKey"].split("-", 1)[-1] in objetivos:
                self.t["pendientes"].delete_entity(partition_key=wa_id, row_key=e["RowKey"])

    def fijar_ultimo(self, wa_id: str, msg_id: str) -> None:
        self.t["control"].upsert_entity({"PartitionKey": wa_id, "RowKey": "ultimo", "msg_id": msg_id})

    def obtener_ultimo(self, wa_id: str) -> str:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.t["control"].get_entity(wa_id, "ultimo").get("msg_id", "")
        except ResourceNotFoundError:
            return ""

    # ---- conversaciones
    def obtener_conversacion(self, wa_id: str) -> Conversacion | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            e = self.t["conversaciones"].get_entity("conv", wa_id)
        except ResourceNotFoundError:
            return None
        historial = _unir("historial", e)
        modo, hasta = self.obtener_modo(wa_id)
        return Conversacion(
            wa_id=wa_id, nombre=e.get("nombre", ""), historial=json.loads(historial) if historial else [],
            modo=modo, humano_hasta=hasta,
            cliente_nombre=e.get("cliente_nombre", ""), cliente_ruc=e.get("cliente_ruc", ""),
            tipo_cliente=e.get("tipo_cliente", "publico"), actualizado=e.get("actualizado", ""),
        )

    def guardar_conversacion(self, conv: Conversacion) -> None:
        from azure.data.tables import UpdateMode

        conv.actualizado = a_iso(ahora_utc())
        entidad = {  # el modo bot/humano se guarda aparte con fijar_modo
            "PartitionKey": "conv", "RowKey": conv.wa_id, "nombre": conv.nombre, "cliente_nombre": conv.cliente_nombre,
            "cliente_ruc": conv.cliente_ruc, "tipo_cliente": conv.tipo_cliente, "actualizado": conv.actualizado,
            **_partir("historial", json.dumps(conv.historial, ensure_ascii=False)),
        }
        self.t["conversaciones"].upsert_entity(entidad, mode=UpdateMode.REPLACE)

    # ---- catálogo
    def listar_productos(self) -> list[Producto]:
        filas = self.t["productos"].query_entities("PartitionKey eq 'prod'")
        return [Producto.de_dict(dict(e)) for e in filas]

    def guardar_productos(self, productos: list[Producto], reemplazar: bool = False) -> None:
        from azure.data.tables import UpdateMode

        tabla = self.t["productos"]
        if reemplazar:
            nuevos = {p.sku.upper() for p in productos}
            for e in tabla.query_entities("PartitionKey eq 'prod'"):
                if e["RowKey"] not in nuevos:
                    tabla.delete_entity("prod", e["RowKey"])
        for p in productos:
            tabla.upsert_entity({"PartitionKey": "prod", "RowKey": p.sku.upper(), **p.a_dict()}, mode=UpdateMode.REPLACE)

    # ---- correlativo de cotizaciones (concurrencia optimista con ETag)
    def siguiente_correlativo(self, nombre: str) -> int:
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        tabla = self.t["contadores"]
        for _ in range(15):
            try:
                e = tabla.get_entity("contador", nombre)
                nuevo = int(e["valor"]) + 1
                tabla.update_entity({"PartitionKey": "contador", "RowKey": nombre, "valor": nuevo},
                                    mode=UpdateMode.REPLACE, etag=e.metadata["etag"],
                                    match_condition=MatchConditions.IfNotModified)
                return nuevo
            except ResourceNotFoundError:
                try:
                    tabla.create_entity({"PartitionKey": "contador", "RowKey": nombre, "valor": 1})
                    return 1
                except ResourceExistsError:
                    continue
            except HttpResponseError as exc:
                if exc.status_code == 412:  # otro proceso ganó la carrera; reintentar
                    continue
                raise
        raise RuntimeError("No se pudo obtener el correlativo")

    # ---- cotizaciones
    def guardar_cotizacion(self, cot: Cotizacion) -> None:
        from azure.data.tables import UpdateMode

        self.t["cotizaciones"].upsert_entity({
            "PartitionKey": "cot", "RowKey": cot.id, "fecha": cot.fecha, "wa_id": cot.wa_id,
            "cliente": cot.cliente_nombre, "ruc": cot.cliente_ruc, "tipo_cliente": cot.tipo_cliente,
            "total_usd": float(cot.total_usd), "peso_kg": float(cot.peso_total_kg), "estado": cot.estado,
            **_partir("json", cot.a_json()),
        }, mode=UpdateMode.REPLACE)

    def obtener_cotizacion(self, cot_id: str) -> Cotizacion | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            e = self.t["cotizaciones"].get_entity("cot", cot_id.upper())
        except ResourceNotFoundError:
            return None
        return Cotizacion.de_json(_unir("json", e))

    def listar_cotizaciones(self) -> list[Cotizacion]:
        filas = self.t["cotizaciones"].query_entities("PartitionKey eq 'cot'")
        return sorted((Cotizacion.de_json(_unir("json", e)) for e in filas), key=lambda c: c.fecha)

    # ---- configuración dinámica (tipo de cambio, etc.)
    def obtener_config(self, clave: str) -> dict[str, Any] | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(self.t["config"].get_entity("config", clave)["valor"])
        except ResourceNotFoundError:
            return None

    def guardar_config(self, clave: str, valor: dict[str, Any]) -> None:
        self.t["config"].upsert_entity({"PartitionKey": "config", "RowKey": clave,
                                        "valor": json.dumps(valor, ensure_ascii=False)})


class ArchivosBlob:
    def __init__(self, config: Config):
        from azure.storage.blob import BlobServiceClient

        if config.storage_connection_string:
            servicio = BlobServiceClient.from_connection_string(config.storage_connection_string)
        else:
            servicio = BlobServiceClient(f"https://{config.storage_account_name}.blob.core.windows.net",
                                         credential=_credencial())
        from azure.core.exceptions import HttpResponseError, ResourceExistsError

        self.contenedor = servicio.get_container_client(config.contenedor_pdfs)
        try:
            self.contenedor.create_container()
        except ResourceExistsError:
            pass
        except HttpResponseError as exc:
            log.warning("No se pudo verificar/crear el contenedor %s: %s", config.contenedor_pdfs, exc.message)

    def guardar(self, ruta: str, datos: bytes, content_type: str = "application/octet-stream") -> str:
        from azure.storage.blob import ContentSettings

        self.contenedor.upload_blob(ruta, datos, overwrite=True,
                                    content_settings=ContentSettings(content_type=content_type))
        return ruta

    def leer(self, ruta: str) -> bytes:
        return self.contenedor.download_blob(ruta).readall()


class ColaAzure:
    def __init__(self, config: Config):
        from azure.storage.queue import QueueClient, TextBase64EncodePolicy

        politica = TextBase64EncodePolicy()  # el trigger de Functions espera Base64
        if config.storage_connection_string:
            self.cliente = QueueClient.from_connection_string(
                config.storage_connection_string, config.cola_mensajes, message_encode_policy=politica)
        else:
            self.cliente = QueueClient(f"https://{config.storage_account_name}.queue.core.windows.net",
                                       config.cola_mensajes, credential=_credencial(),
                                       message_encode_policy=politica)
        from azure.core.exceptions import HttpResponseError, ResourceExistsError

        try:
            self.cliente.create_queue()
        except ResourceExistsError:
            pass
        except HttpResponseError as exc:
            log.warning("No se pudo verificar/crear la cola %s: %s", config.cola_mensajes, exc.message)

    def enviar(self, mensaje: dict[str, Any], retraso_segundos: int = 0) -> None:
        self.cliente.send_message(json.dumps(mensaje), visibility_timeout=max(0, retraso_segundos) or None)
