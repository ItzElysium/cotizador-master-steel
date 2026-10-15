"""Orquestador: une WhatsApp, el agente, el motor de precios y el almacenamiento.

No depende de Azure Functions; `function_app.py` solo lo invoca. Por eso el
mismo flujo corre en los tests y en el simulador de consola.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import time
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .agente import Agente, aligerar, nota_de_sistema, recortar
from .catalogo import Catalogo, ErrorCatalogo, leer_archivo, leer_csv
from .config import Config
from .modelos import Conversacion, Cotizacion, MensajeEntrante, a_iso, ahora_utc, de_iso
from .pdf import _fecha_local, generar_pdf, nombre_archivo
from .precios import MotorPrecios, fmt, fmt_cantidad, fmt_precio, q2
from .prompts import HERRAMIENTAS, construir_sistema
from .tipo_cambio import ProveedorTipoCambio
from .almacenamiento import clave_segura
from .whatsapp import CODIGO_FUERA_DE_VENTANA, ErrorWhatsApp, EventoEco, EventoFallo, parsear_fallos, parsear_webhook

log = logging.getLogger(__name__)

CACHE_CATALOGO_SEG = 120
SEGUNDOS_CANDADO = 180      # un turno del agente no debería tardar más; si el proceso muere, el candado vence
SEGUNDOS_REINTENTO = 8      # espera antes de reintentar si el cliente tiene un turno en curso
VENTANA_HORAS = 23.5        # margen sobre la ventana de 24 h de WhatsApp
MENSAJE_ERROR = ("Disculpe, tuve un inconveniente para procesar su mensaje. Un asesor le responderá en breve. 🙏")
TIPOS_IMAGEN = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class Servicio:
    def __init__(self, config: Config, repo, archivos, cola, whatsapp, llm=None, transcriptor=None,
                 tc: ProveedorTipoCambio | None = None, reloj: Callable = ahora_utc):
        self.config = config
        self.repo = repo
        self.archivos = archivos
        self.cola = cola
        self.whatsapp = whatsapp
        self.llm = llm
        self.transcriptor = transcriptor
        self.tc = tc or ProveedorTipoCambio(config, repo)
        self.reloj = reloj
        self.agente = Agente(llm, HERRAMIENTAS) if llm else None
        self.admin = ComandosAdmin(self)
        self._catalogo: Catalogo | None = None
        self._catalogo_ts = 0.0

    # =========================================================== catálogo
    def catalogo(self) -> Catalogo:
        if self._catalogo is None or time.monotonic() - self._catalogo_ts > CACHE_CATALOGO_SEG:
            productos = self.repo.listar_productos()
            if not productos and Path(self.config.catalogo_csv_inicial).is_file():
                productos, errores = leer_archivo(self.config.catalogo_csv_inicial)
                log.info("Catálogo vacío: se cargó %s (%d productos, %d errores)",
                         self.config.catalogo_csv_inicial, len(productos), len(errores))
                self.repo.guardar_productos(productos)
            self._catalogo = Catalogo(productos)
            self._catalogo_ts = time.monotonic()
        return self._catalogo

    def invalidar_catalogo(self) -> None:
        self._catalogo = None

    def motor(self) -> MotorPrecios:
        return MotorPrecios(self.config, self.catalogo())

    def cargar_catalogo_csv(self, texto: str, reemplazar: bool = False) -> dict[str, Any]:
        # Sin `reemplazar` se admite carga parcial (ej. solo sku,precio_usd) sobre el catálogo actual.
        existentes = None if reemplazar else self.catalogo().productos
        try:
            productos, errores = leer_csv(texto, existentes=existentes, hoy=_hoy_iso(self))
        except ErrorCatalogo as exc:
            return {"ok": False, "cargados": 0, "errores": [str(exc)]}
        if reemplazar and errores:
            # Reemplazar con filas inválidas borraría esos productos: no se toca nada.
            return {"ok": False, "cargados": 0, "errores": errores,
                    "nota": "No se aplicó el reemplazo: corrige los errores y vuelve a subir el archivo."}
        if productos:
            self.repo.guardar_productos(productos, reemplazar=reemplazar)
            self.invalidar_catalogo()
        return {"ok": not errores, "cargados": len(productos), "errores": errores}

    # =========================================================== webhook
    def recibir_webhook(self, payload: dict[str, Any]) -> int:
        """Registra los mensajes y los encola. Debe ser rápido: Meta espera un 200 en segundos.

        El mensaje se marca como recibido recién al final: si algo falla antes, respondemos 500,
        Meta reintenta y el mensaje no se pierde. Reprocesarlo es inocuo (todo es idempotente).
        """
        mensajes, ecos = parsear_webhook(payload)
        for eco in ecos:
            self._registrar_eco(eco)
        for fallo in parsear_fallos(payload):
            self._registrar_fallo(fallo)
        encolados = 0
        for m in mensajes:
            if not m.id or not m.wa_id or self.repo.mensaje_registrado(m.id):
                continue  # Meta reintentó un webhook ya recibido
            self.repo.guardar_pendiente(m)
            self.repo.fijar_ultimo(m.wa_id, m.id)
            self.repo.marcar_entrante(m.wa_id, a_iso(self.reloj()))
            inmediato = bool(m.boton_id) or self.config.es_admin(m.wa_id) and m.texto.strip().startswith("/")
            self.cola.enviar({"wa_id": m.wa_id, "msg_id": m.id}, 0 if inmediato else self.config.debounce_segundos)
            self.repo.registrar_mensaje(m.id)
            encolados += 1
        return encolados

    def _registrar_eco(self, eco: EventoEco) -> None:
        """El vendedor respondió desde la app WhatsApp Business: el bot se hace a un lado."""
        if not eco.wa_id_cliente:
            return
        self.pausar_bot(eco.wa_id_cliente)
        if eco.texto:
            conv = self.repo.obtener_conversacion(eco.wa_id_cliente) or Conversacion(wa_id=eco.wa_id_cliente)
            conv.historial.append({"rol": "asistente", "texto": f"[Respondió un asesor humano]: {eco.texto}",
                                   "llamadas": []})
            self.repo.guardar_conversacion(conv)

    def _registrar_fallo(self, fallo: EventoFallo) -> None:
        """Meta avisa que un mensaje nuestro no llegó (ej. ventana de 24 h cerrada)."""
        log.warning("Mensaje no entregado a %s (%s): %s", fallo.wa_id, fallo.codigo, fallo.detalle)
        if not fallo.wa_id or self.config.es_admin(fallo.wa_id):
            return  # no avisar a los admins de sus propios fallos (evita bucles)
        motivo = ("pasaron más de 24 h desde su último mensaje" if fallo.codigo == CODIGO_FUERA_DE_VENTANA
                  else fallo.detalle)
        self.notificar_admins(f"⚠️ No se pudo entregar un mensaje a +{fallo.wa_id}: {motivo}. "
                              f"Escríbele desde la app.")

    def ventana_abierta(self, wa_id: str) -> bool:
        """WhatsApp solo permite mensajes libres hasta 24 h después del último mensaje del usuario."""
        ultimo = de_iso(self.repo.obtener_entrante(wa_id))
        return bool(ultimo) and self.reloj() - ultimo < timedelta(hours=VENTANA_HORAS)

    # =========================================================== procesamiento
    def procesar(self, wa_id: str, msg_id: str) -> str:
        if self.repo.obtener_ultimo(wa_id) != msg_id:
            return "omitido"  # llegó un mensaje más nuevo; ese turno responderá todo junto
        token = self.repo.adquirir_candado(wa_id, SEGUNDOS_CANDADO, self.reloj())
        if not token:
            # Otro turno de este cliente está en curso: se reintenta cuando termine.
            self.cola.enviar({"wa_id": wa_id, "msg_id": msg_id}, SEGUNDOS_REINTENTO)
            return "reencolado"
        try:
            return self._procesar_con_candado(wa_id)
        finally:
            self.repo.liberar_candado(wa_id, token)

    def _procesar_con_candado(self, wa_id: str) -> str:
        pendientes = self.repo.listar_pendientes(wa_id)
        if not pendientes:
            return "vacio"  # un turno anterior ya respondió estos mensajes
        conv = self.repo.obtener_conversacion(wa_id) or Conversacion(wa_id=wa_id)
        conv.modo, conv.humano_hasta = self.repo.obtener_modo(wa_id)
        if pendientes[-1].nombre:
            conv.nombre = pendientes[-1].nombre
        es_admin = self.config.es_admin(wa_id)

        contenido: list[dict[str, Any]] = []
        for m in pendientes:
            if es_admin and m.boton_id:
                self.admin.manejar_boton(wa_id, m.boton_id)
            elif es_admin and m.texto.strip().startswith("/"):
                self.admin.ejecutar(wa_id, m.texto.strip())
            else:
                contenido.extend(self._contenido_de(m))

        resultado, respuesta = "admin", None
        if contenido:
            if conv.en_modo_humano(self.reloj()):
                conv.historial.append(aligerar([{"rol": "usuario", "contenido": contenido}])[0])
                resultado = "pausado"
            else:
                if conv.modo == "humano":  # la pausa venció
                    self.reactivar_bot(wa_id)
                self.whatsapp.marcar_leido(pendientes[-1].id)
                respuesta = self._responder_con_agente(conv, contenido, turno_id=pendientes[-1].id)
                resultado = "respondido"

        # Primero se guarda el estado y luego se envía el texto: si el envío falla, un reintento
        # de la cola no repite el turno (ni genera otra cotización).
        conv.historial = recortar(conv.historial, self.config.historial_max_mensajes)
        self.repo.guardar_conversacion(conv)
        self.repo.borrar_pendientes(wa_id, [m.id for m in pendientes])
        if respuesta:
            try:
                self.whatsapp.enviar_texto(wa_id, respuesta)
            except ErrorWhatsApp as exc:
                log.error("No se pudo enviar la respuesta a %s: %s", wa_id, exc)
        return resultado

    def _contenido_de(self, m: MensajeEntrante) -> list[dict[str, Any]]:
        texto = {"tipo": "texto", "texto": m.texto} if m.texto else None
        if m.tipo in {"text", "interactive", "button", "location"}:
            return [texto] if texto else []
        if m.tipo == "image":
            try:
                datos, mime = self.whatsapp.descargar_media(m.media_id)
                mime = (mime or m.mime or "image/jpeg").split(";")[0]
                if mime not in TIPOS_IMAGEN:
                    mime = "image/jpeg"
                imagen = {"tipo": "imagen", "media_type": mime, "datos_b64": base64.b64encode(datos).decode()}
                return [imagen] + ([texto] if texto else [])
            except Exception:
                log.exception("No se pudo descargar la imagen %s", m.media_id)
                return [{"tipo": "texto", "texto": "[El cliente envió una imagen que no se pudo abrir. Pídele que la reenvíe.]"}]
        if m.tipo == "audio":
            if self.transcriptor:
                try:
                    datos, mime = self.whatsapp.descargar_media(m.media_id)
                    transcrito = self.transcriptor.transcribir(datos, mime or m.mime or "audio/ogg")
                    if transcrito:
                        return [{"tipo": "texto", "texto": f"[Audio del cliente, transcrito]: {transcrito}"}]
                except Exception:
                    log.exception("No se pudo transcribir el audio %s", m.media_id)
            return [{"tipo": "texto", "texto": "[El cliente envió un audio de voz que no puedes escuchar. "
                                               "Pídele con amabilidad que escriba su pedido o envíe una foto de su lista.]"}]
        if m.tipo == "document":
            es_pdf = "pdf" in (m.mime or "") or m.nombre_archivo.lower().endswith(".pdf")
            if es_pdf and getattr(self.llm, "soporta_pdf", False):
                try:
                    datos, _ = self.whatsapp.descargar_media(m.media_id)
                    return [{"tipo": "pdf", "datos_b64": base64.b64encode(datos).decode()}] + ([texto] if texto else [])
                except Exception:
                    log.exception("No se pudo descargar el PDF %s", m.media_id)
            return [{"tipo": "texto", "texto": f"[El cliente envió el archivo '{m.nombre_archivo or 'documento'}' que no "
                                               f"puedes abrir. Pídele una foto de la lista o que la escriba.]"}]
        return [texto] if texto else []

    def _responder_con_agente(self, conv: Conversacion, contenido: list[dict[str, Any]],
                              turno_id: str = "") -> str | None:
        """Corre el agente y devuelve el texto para el cliente (lo envía `procesar`)."""
        if not self.agente:
            raise RuntimeError("No hay modelo de lenguaje configurado")
        catalogo = self.catalogo()
        resumen = (catalogo.resumen_para_prompt() if len(catalogo) <= self.config.catalogo_en_prompt_max
                   else "(Catálogo extenso: usa buscar_productos para ubicar cada producto.)")
        sistema = construir_sistema(self.config, resumen, conv)
        herramientas = Herramientas(self, conv, turno_id=turno_id)
        try:
            texto, mensajes, uso = self.agente.responder(sistema, conv.historial, contenido, herramientas.ejecutar)
            log.info("Agente respondió a %s · tokens %s", conv.wa_id, uso)
        except Exception:
            log.exception("Falló el agente para %s", conv.wa_id)
            conv.historial.append(aligerar([{"rol": "usuario", "contenido": contenido}])[0])
            self.notificar_admins(f"⚠️ El asistente no pudo responder a {conv.nombre or ''} (+{conv.wa_id}). "
                                  f"Revisa la conversación en la app.")
            return MENSAJE_ERROR
        conv.historial = aligerar(mensajes)
        if not texto.strip():
            texto = ("Listo. ¿Necesita cotizar algo más?" if herramientas.usadas
                     else "Disculpe, no le entendí bien. ¿Me indica qué producto y cantidad necesita?")
        return texto

    # =========================================================== cotizaciones
    def resumen_cotizacion(self, cot: Cotizacion, para_admin: bool = True) -> str:
        lineas = [f"🧾 *{cot.id}* — {cot.cliente_nombre} (+{cot.wa_id})"]
        if cot.cliente_ruc:
            lineas.append(f"RUC/DNI: {cot.cliente_ruc}")
        lineas.append(f"Tipo: {cot.tipo_cliente} · Peso: {fmt(cot.peso_total_kg, 0)} kg")
        for ln in cot.lineas[:12]:
            lineas.append(f"• {fmt_cantidad(ln.cantidad)} x {ln.descripcion} — US$ {fmt(ln.subtotal_usd)}")
        if len(cot.lineas) > 12:
            lineas.append(f"• … y {len(cot.lineas) - 12} ítems más")
        total = f"*Total: US$ {fmt(cot.total_usd)}* inc. IGV"
        if cot.total_pen is not None:
            total += f" (S/ {fmt(cot.total_pen)})"
        lineas.append(total)
        if cot.descuento_usd:
            lineas.append(f"Descuento aplicado: {fmt(cot.descuento_pct * 100, 1)}%")
        if para_admin:
            for alerta in cot.avisos + cot.alertas:
                lineas.append(f"⚠️ {alerta}")
        return "\n".join(lineas)

    def solicitar_aprobacion(self, cot: Cotizacion, pdf: bytes) -> bool:
        """Envía el PDF y los botones a cada vendedor. Si su ventana de 24 h está cerrada, usa la
        plantilla de utilidad (WA_PLANTILLA_APROBACION); el PDF le llegará al responder /pendientes."""
        avisado = False
        for admin in self.config.admin_numeros:
            try:
                if not self.ventana_abierta(admin):
                    if self.config.wa_plantilla_aprobacion:
                        self.whatsapp.enviar_plantilla(admin, self.config.wa_plantilla_aprobacion,
                                                       [cot.id, cot.cliente_nombre, f"US$ {fmt(cot.total_usd)}"])
                        avisado = True
                    else:
                        log.warning("Ventana de 24 h cerrada con el admin %s y no hay plantilla: la %s queda "
                                    "en /pendientes", admin, cot.id)
                    continue
                self.whatsapp.enviar_documento(admin, pdf, nombre_archivo(cot), self.resumen_cotizacion(cot))
                self.whatsapp.enviar_botones(admin, f"¿Envío la {cot.id} a {cot.cliente_nombre}?",
                                             [(f"aprobar:{cot.id}", "✅ Enviar"), (f"tomar:{cot.id}", "🙋 La atiendo yo")])
                avisado = True
            except ErrorWhatsApp as exc:
                log.warning("No se pudo avisar al admin %s: %s", admin, exc)
        return avisado

    def enviar_cotizacion_al_cliente(self, cot: Cotizacion, pdf: bytes | None = None) -> None:
        if not self.ventana_abierta(cot.wa_id):
            raise ErrorWhatsApp("ventana de 24 h cerrada", CODIGO_FUERA_DE_VENTANA)
        pdf = pdf or self.archivos.leer(cot.pdf_ruta)
        leyenda = (f"Cotización {cot.id} · Total US$ {fmt(cot.total_usd)} inc. IGV · "
                   f"válida hasta {_fecha_local(cot.vigencia_hasta, self.config.zona_horaria)}.")
        self.whatsapp.enviar_documento(cot.wa_id, pdf, nombre_archivo(cot), leyenda)
        cot.estado = "enviada"
        self.repo.guardar_cotizacion(cot)

    def notificar_admins(self, texto: str) -> None:
        for admin in self.config.admin_numeros:
            try:
                self.whatsapp.enviar_texto(admin, texto)
            except ErrorWhatsApp as exc:
                log.warning("No se pudo notificar a %s: %s", admin, exc)

    def pausar_bot(self, wa_id: str, horas: int | None = None) -> None:
        """horas=0: pausa indefinida hasta /bot."""
        horas = self.config.humano_pausa_horas if horas is None else horas
        self.repo.fijar_modo(wa_id, "humano", a_iso(self.reloj() + timedelta(hours=horas)) if horas > 0 else "")

    def reactivar_bot(self, wa_id: str) -> None:
        self.repo.fijar_modo(wa_id, "bot", "")

    # =========================================================== reportes
    def exportar_csv(self, detalle: bool = False) -> str:
        salida = io.StringIO()
        escritor = _EscritorSeguro(csv.writer(salida))
        cotizaciones = self.repo.listar_cotizaciones()
        if detalle:
            escritor.writerow(["cotizacion", "fecha", "estado", "cliente", "tipo_cliente", "sku", "descripcion",
                               "cantidad", "peso_kg", "precio_unitario_usd", "subtotal_usd"])
            for c in cotizaciones:
                for ln in c.lineas:
                    escritor.writerow([c.id, c.fecha, c.estado, c.cliente_nombre, c.tipo_cliente, ln.sku,
                                       ln.descripcion, ln.cantidad, ln.peso_total_kg, ln.precio_unitario_usd,
                                       ln.subtotal_usd])
        else:
            escritor.writerow(["cotizacion", "fecha", "estado", "wa_id", "cliente", "ruc", "tipo_cliente",
                               "items", "peso_kg", "subtotal_usd", "descuento_usd", "igv_usd", "total_usd",
                               "tipo_cambio", "total_pen", "motivo_cierre"])
            for c in cotizaciones:
                escritor.writerow([c.id, c.fecha, c.estado, c.wa_id, c.cliente_nombre, c.cliente_ruc, c.tipo_cliente,
                                   len(c.lineas), c.peso_total_kg, c.subtotal_bruto_usd, c.descuento_usd, c.igv_usd,
                                   c.total_usd, c.tipo_cambio.valor if c.tipo_cambio else "",
                                   c.total_pen if c.total_pen is not None else "", c.motivo_cierre])
        return salida.getvalue()


class _EscritorSeguro:
    """Evita inyección de fórmulas al abrir el CSV en Excel (un nombre que empiece con = + - @)."""

    def __init__(self, escritor):
        self.escritor = escritor

    def writerow(self, fila: list[Any]) -> None:
        self.escritor.writerow([f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v
                                for v in fila])


# ===================================================================== herramientas

class Herramientas:
    """Ejecuta las herramientas que el agente pide. Siempre devuelve JSON (texto)."""

    def __init__(self, servicio: Servicio, conv: Conversacion, turno_id: str = ""):
        self.s = servicio
        self.conv = conv
        self.turno_id = turno_id
        self.usadas: list[str] = []
        self._tc = None
        self._tc_cargado = False

    def _tipo_cambio(self):
        if not self._tc_cargado:
            self._tc = self.s.tc.obtener()
            self._tc_cargado = True
        return self._tc

    def ejecutar(self, nombre: str, argumentos: dict[str, Any]) -> str:
        funciones = {
            "buscar_productos": self.buscar_productos,
            "calcular_cotizacion": self.calcular_cotizacion,
            "generar_cotizacion": self.generar_cotizacion,
            "derivar_a_asesor": self.derivar_a_asesor,
        }
        funcion = funciones.get(nombre)
        self.usadas.append(nombre)
        if not funcion:
            resultado: dict[str, Any] = {"ok": False, "error": f"Herramienta desconocida: {nombre}"}
        else:
            resultado = funcion(argumentos or {})
        return json.dumps(resultado, ensure_ascii=False, default=str)

    def buscar_productos(self, args: dict[str, Any]) -> dict[str, Any]:
        motor = self.s.motor()
        tc = self._tipo_cambio()
        resultados = []
        for c in motor.catalogo.buscar(str(args.get("consulta", "")), limite=6):
            p = c.producto
            fila = {"sku": p.sku, "descripcion": p.descripcion, "unidad": p.unidad, "peso_kg": fmt(p.peso_kg),
                    "precio_usd_sin_igv": fmt_precio(motor.precio_sin_igv(p)),
                    "precio_usd_con_igv": fmt(motor.precio_con_igv(p)), "stock": p.stock}
            if tc and self.s.config.mostrar_soles:
                fila["precio_soles_con_igv_ref"] = fmt(q2(motor.precio_con_igv(p) * (tc.valor + self.s.config.tc_spread)))
            resultados.append(fila)
        if not resultados:
            return {"ok": False, "resultados": [], "nota": "Sin coincidencias. Pide más detalle o deriva a un asesor."}
        return {"ok": True, "resultados": resultados}

    def calcular_cotizacion(self, args: dict[str, Any]) -> dict[str, Any]:
        tipo = args.get("tipo_cliente") or self.conv.tipo_cliente
        res = self.s.motor().calcular(args.get("items") or [], tipo, self._tipo_cambio())
        return {"ok": res.ok, **res.para_agente()}

    def generar_cotizacion(self, args: dict[str, Any]) -> dict[str, Any]:
        s, conv = self.s, self.conv
        motor = s.motor()
        tipo = args.get("tipo_cliente") or conv.tipo_cliente
        res = motor.calcular(args.get("items") or [], tipo, self._tipo_cambio())
        if not res.ok:
            return {"ok": False, **res.para_agente(),
                    "instruccion": "No se generó la cotización. Resuelve los errores con el cliente."}
        nombre = str(args.get("cliente_nombre") or conv.cliente_nombre or conv.nombre or "Cliente").strip()
        ruc = str(args.get("cliente_ruc") or conv.cliente_ruc or "").strip()

        # Idempotencia: si la cola reintenta este mismo turno, no se crea otra cotización ni se reenvía.
        clave = ""
        if self.turno_id:
            firma = json.dumps([self.turno_id, args.get("items"), nombre], sort_keys=True, default=str)
            clave = f"turno-{clave_segura(firma)}"
            previa = s.repo.obtener_config(clave)
            if previa:
                return {**previa, "nota": "Cotización ya generada en este turno."}

        numero = s.repo.siguiente_correlativo("cotizacion")
        cot = motor.crear_cotizacion(res, f"COT-{numero:06d}", conv.wa_id, nombre, ruc,
                                     str(args.get("notas") or ""), ahora=s.reloj())
        pdf = generar_pdf(cot, s.config)
        cot.pdf_ruta = s.archivos.guardar(f"{cot.fecha[:4]}/{cot.fecha[5:7]}/{cot.id}.pdf", pdf, "application/pdf")
        conv.cliente_nombre, conv.cliente_ruc, conv.tipo_cliente = nombre, ruc, res.tipo_cliente

        resumen = {"cotizacion": cot.id, "total_usd_con_igv": fmt(cot.total_usd),
                   "valida_hasta": _fecha_local(cot.vigencia_hasta, s.config.zona_horaria)}
        if cot.total_pen is not None:
            resumen["total_soles_referencial"] = fmt(cot.total_pen)

        if s.config.es_aprobacion and s.config.admin_numeros:
            cot.estado = "pendiente_aprobacion"
            s.repo.guardar_cotizacion(cot)
            resultado = {"ok": True, "estado": "pendiente_aprobacion", **resumen,
                         "instruccion": "Dile al cliente que su cotización está lista, menciona el número y el total, "
                                        "y que un asesor confirma stock y se la envía en PDF en minutos dentro del "
                                        f"horario de atención ({s.config.horario_atencion})."}
            if clave:
                s.repo.guardar_config(clave, resultado)
            s.solicitar_aprobacion(cot, pdf)
            return resultado
        try:
            s.enviar_cotizacion_al_cliente(cot, pdf)
            resultado = {"ok": True, "estado": "enviada", **resumen,
                         "instruccion": "El PDF ya fue enviado. Confirma en una línea y pregunta si desea proceder "
                                        "con el pedido."}
        except ErrorWhatsApp as exc:
            cot.estado = "pendiente_aprobacion"
            s.repo.guardar_cotizacion(cot)
            s.notificar_admins(f"⚠️ No se pudo enviar {cot.id} a +{conv.wa_id}: {exc}")
            resultado = {"ok": False, "estado": "pendiente_aprobacion", **resumen,
                         "instruccion": "Hubo un problema al enviar el PDF; dile que un asesor se lo hará llegar."}
        if clave:
            s.repo.guardar_config(clave, resultado)
        return resultado

    def derivar_a_asesor(self, args: dict[str, Any]) -> dict[str, Any]:
        s, conv = self.s, self.conv
        motivo = str(args.get("motivo") or "Solicita atención de un asesor").strip()
        s.pausar_bot(conv.wa_id)
        conv.modo, conv.humano_hasta = s.repo.obtener_modo(conv.wa_id)
        s.notificar_admins(f"🙋 {conv.nombre or 'Un cliente'} (+{conv.wa_id}) necesita un asesor:\n{motivo}\n\n"
                           f"Escríbele desde la app. El asistente queda en pausa {s.config.humano_pausa_horas} h "
                           f"con este cliente (/bot {conv.wa_id} para reactivarlo).")
        return {"ok": True, "instruccion": "Dile al cliente que un asesor lo contactará en breve "
                                           f"(horario: {s.config.horario_atencion})."}


# ===================================================================== administración

AYUDA_ADMIN = """*Comandos de administrador*
/pendientes — cotizaciones por aprobar
/cot COT-000123 — ver una cotización
/ganada COT-000123 — marcar como venta cerrada
/perdida COT-000123 motivo — marcar como perdida (ej. precio)
/tc — ver tipo de cambio · /tc 3.75 — fijarlo hoy
/precio FC-0500 8.90 — actualizar precio (US$ sin IGV)
/stock FC-0500 agotado — disponible | consultar | agotado
/buscar fierro 1/2 — consultar catálogo y precios
/humano 51987654321 — pausar el bot con un cliente
/bot 51987654321 — reactivar el bot con un cliente
/resumen [hoy|7|30] — indicadores comerciales"""


class ComandosAdmin:
    def __init__(self, servicio: Servicio):
        self.s = servicio

    def _responder(self, admin: str, texto: str) -> None:
        self.s.whatsapp.enviar_texto(admin, texto)

    # ------------------------------------------------------------ botones
    def manejar_boton(self, admin: str, boton_id: str) -> None:
        if boton_id.strip().lower().replace(" ", "_") == "ver_pendientes":  # botón de la plantilla de aviso
            self.pendientes(admin)
            return
        accion, _, cot_id = boton_id.partition(":")
        cot = self.s.repo.obtener_cotizacion(cot_id) if cot_id else None
        if not cot:
            self._responder(admin, f"No encontré la cotización {cot_id}.")
            return
        if cot.estado != "pendiente_aprobacion":
            self._responder(admin, f"La {cot.id} ya fue procesada (estado: {cot.estado}).")
            return
        if accion == "aprobar":
            try:
                self.s.enviar_cotizacion_al_cliente(cot)
            except ErrorWhatsApp as exc:
                if exc.codigo == CODIGO_FUERA_DE_VENTANA:
                    self._responder(admin, f"No pude enviar la {cot.id}: pasaron más de 24 h desde el último mensaje "
                                           f"del cliente. Envíasela desde la app.")
                else:
                    self._responder(admin, f"No pude enviar la {cot.id}: {exc}")
                return
            self._anotar(cot.wa_id, f"Un asesor revisó la cotización {cot.id} y ya se envió el PDF al cliente.")
            self._responder(admin, f"✅ {cot.id} enviada a {cot.cliente_nombre}.")
        elif accion == "tomar":
            cot.estado = "asesor"
            self.s.repo.guardar_cotizacion(cot)
            self.s.pausar_bot(cot.wa_id)
            self._responder(admin, f"Listo. No envié la {cot.id}; el asistente queda en pausa "
                                   f"{self.s.config.humano_pausa_horas} h con {cot.cliente_nombre}. "
                                   f"Escríbele desde la app (/bot {cot.wa_id} para reactivarlo).")

    def _anotar(self, wa_id: str, texto: str) -> None:
        conv = self.s.repo.obtener_conversacion(wa_id)
        if conv:
            conv.historial.extend(nota_de_sistema(texto))
            self.s.repo.guardar_conversacion(conv)

    # ------------------------------------------------------------ comandos
    def ejecutar(self, admin: str, texto: str) -> None:
        partes = texto.split()
        comando, args = partes[0].lower(), partes[1:]
        acciones = {
            "/ayuda": lambda: self._responder(admin, AYUDA_ADMIN),
            "/help": lambda: self._responder(admin, AYUDA_ADMIN),
            "/pendientes": lambda: self.pendientes(admin),
            "/cot": lambda: self.ver(admin, args),
            "/ganada": lambda: self.cerrar(admin, args, "ganada"),
            "/perdida": lambda: self.cerrar(admin, args, "perdida"),
            "/tc": lambda: self.tipo_cambio(admin, args),
            "/precio": lambda: self.precio(admin, args),
            "/stock": lambda: self.stock(admin, args),
            "/buscar": lambda: self.buscar(admin, " ".join(args)),
            "/humano": lambda: self.modo(admin, args, humano=True),
            "/bot": lambda: self.modo(admin, args, humano=False),
            "/resumen": lambda: self.resumen(admin, args),
        }
        accion = acciones.get(comando)
        if not accion:
            self._responder(admin, f"No conozco el comando {comando}.\n\n{AYUDA_ADMIN}")
            return
        accion()

    def pendientes(self, admin: str) -> None:
        pendientes = [c for c in self.s.repo.listar_cotizaciones() if c.estado == "pendiente_aprobacion"]
        if not pendientes:
            self._responder(admin, "No hay cotizaciones pendientes de aprobación. 👍")
            return
        for cot in pendientes[-5:]:
            self.s.whatsapp.enviar_botones(admin, self.s.resumen_cotizacion(cot)[:1000],
                                           [(f"aprobar:{cot.id}", "✅ Enviar"), (f"tomar:{cot.id}", "🙋 La atiendo yo")])
        if len(pendientes) > 5:
            self._responder(admin, f"Hay {len(pendientes)} pendientes; te mostré las 5 más recientes.")

    def ver(self, admin: str, args: list[str]) -> None:
        cot = self.s.repo.obtener_cotizacion(args[0]) if args else None
        if not cot:
            self._responder(admin, "Uso: /cot COT-000123")
            return
        self._responder(admin, f"{self.s.resumen_cotizacion(cot)}\nEstado: {cot.estado}")

    def cerrar(self, admin: str, args: list[str], estado: str) -> None:
        cot = self.s.repo.obtener_cotizacion(args[0]) if args else None
        if not cot:
            self._responder(admin, f"Uso: /{estado} COT-000123" + (" motivo" if estado == "perdida" else ""))
            return
        cot.estado = estado
        cot.motivo_cierre = " ".join(args[1:])
        self.s.repo.guardar_cotizacion(cot)
        icono = "🎉" if estado == "ganada" else "📉"
        self._responder(admin, f"{icono} {cot.id} marcada como {estado}.")

    def tipo_cambio(self, admin: str, args: list[str]) -> None:
        if args:
            try:
                valor = Decimal(args[0].replace(",", "."))
                if not Decimal("2") < valor < Decimal("6"):
                    raise InvalidOperation
            except InvalidOperation:
                self._responder(admin, "Valor inválido. Ejemplo: /tc 3.75")
                return
            tc = self.s.tc.fijar_manual(valor)
            self._responder(admin, f"💱 Tipo de cambio fijado en S/ {tc.valor} para hoy.")
            return
        tc = self.s.tc.obtener()
        if tc:
            self._responder(admin, f"💱 TC vigente: S/ {tc.valor} ({tc.fuente}, {tc.fecha}). "
                                   f"Spread configurado: {self.s.config.tc_spread}.")
        else:
            self._responder(admin, "No hay tipo de cambio disponible. Fíjalo con /tc 3.75")

    def precio(self, admin: str, args: list[str]) -> None:
        if len(args) != 2:
            self._responder(admin, "Uso: /precio FC-0500 8.90  (US$ sin IGV, en la misma base de la lista)")
            return
        producto = self.s.catalogo().obtener(args[0])
        if not producto:
            self._responder(admin, f"No existe el código {args[0]}.")
            return
        try:
            nuevo = Decimal(args[1].replace(",", "."))
            if nuevo <= 0:
                raise InvalidOperation
        except InvalidOperation:
            self._responder(admin, "Precio inválido.")
            return
        anterior = producto.precio_usd
        producto.precio_usd = nuevo
        producto.actualizado = _hoy_iso(self.s)
        self.s.repo.guardar_productos([producto])
        self.s.invalidar_catalogo()
        base = "por kg" if producto.base_precio == "kg" else f"por {producto.unidad}"
        self._responder(admin, f"✅ {producto.sku}: US$ {anterior} → US$ {nuevo} {base} (sin IGV).")

    def stock(self, admin: str, args: list[str]) -> None:
        if len(args) != 2 or args[1].lower() not in {"disponible", "consultar", "agotado"}:
            self._responder(admin, "Uso: /stock FC-0500 disponible|consultar|agotado")
            return
        producto = self.s.catalogo().obtener(args[0])
        if not producto:
            self._responder(admin, f"No existe el código {args[0]}.")
            return
        producto.stock = args[1].lower()
        self.s.repo.guardar_productos([producto])
        self.s.invalidar_catalogo()
        self._responder(admin, f"✅ {producto.sku} ahora está '{producto.stock}'.")

    def buscar(self, admin: str, consulta: str) -> None:
        if not consulta:
            self._responder(admin, "Uso: /buscar plancha negra 1/8")
            return
        motor = self.s.motor()
        resultados = motor.catalogo.buscar(consulta, limite=6)
        if not resultados:
            self._responder(admin, "Sin coincidencias.")
            return
        lineas = [f"{c.producto.sku} · {c.producto.descripcion} · US$ {fmt_precio(motor.precio_sin_igv(c.producto))} "
                  f"sin IGV · {c.producto.stock}" for c in resultados]
        self._responder(admin, "\n".join(lineas))

    def modo(self, admin: str, args: list[str], humano: bool) -> None:
        if not args:
            self._responder(admin, "Uso: /humano 51987654321  o  /bot 51987654321")
            return
        numero = "".join(ch for ch in args[0] if ch.isdigit())
        if humano:
            self.s.pausar_bot(numero, horas=0)
            self._responder(admin, f"⏸️ Bot en pausa con +{numero} hasta que lo reactives con /bot {numero}.")
        else:
            self.s.reactivar_bot(numero)
            self._responder(admin, f"▶️ Bot reactivado con +{numero}.")

    def resumen(self, admin: str, args: list[str]) -> None:
        periodo = (args[0].lower() if args else "hoy")
        dias = 1 if periodo == "hoy" else int(periodo) if periodo.isdigit() else 1
        desde = self.s.reloj() - timedelta(days=dias)
        cots = [c for c in self.s.repo.listar_cotizaciones() if (de_iso(c.fecha) or desde) >= desde]
        if not cots:
            self._responder(admin, f"Sin cotizaciones en el periodo ({periodo}).")
            return
        estados = Counter(c.estado for c in cots)
        monto = sum((c.total_usd for c in cots), Decimal("0"))
        ganado = sum((c.total_usd for c in cots if c.estado == "ganada"), Decimal("0"))
        cerradas = estados["ganada"] + estados["perdida"]
        tasa = f"{estados['ganada'] / cerradas * 100:.0f}%" if cerradas else "s/d"
        kilos: dict[str, Decimal] = defaultdict(Decimal)
        for c in cots:
            for ln in c.lineas:
                kilos[ln.descripcion] += ln.peso_total_kg
        top = sorted(kilos.items(), key=lambda x: -x[1])[:3]
        motivos = Counter(c.motivo_cierre.lower() for c in cots if c.estado == "perdida" and c.motivo_cierre)
        texto = [f"📊 *Resumen ({'últimas 24 h' if dias == 1 else f'últimos {dias} días'})*",
                 f"Cotizaciones: {len(cots)} · US$ {fmt(monto)}",
                 f"Ganadas: {estados['ganada']} (US$ {fmt(ganado)}) · Perdidas: {estados['perdida']} · Tasa de cierre: {tasa}",
                 f"Pendientes de aprobar: {estados['pendiente_aprobacion']} · Enviadas sin cierre: {estados['enviada']}",
                 "Más cotizado (kg): " + "; ".join(f"{d} ({fmt(k, 0)})" for d, k in top)]
        if motivos:
            texto.append("Motivos de pérdida: " + "; ".join(f"{m} ({n})" for m, n in motivos.most_common(3)))
        self._responder(admin, "\n".join(texto))


def _hoy_iso(servicio: Servicio) -> str:
    from zoneinfo import ZoneInfo

    return servicio.reloj().astimezone(ZoneInfo(servicio.config.zona_horaria)).date().isoformat()
