"""Puntos de entrada en Azure Functions (modelo de programación Python v2).

  GET/POST /api/whatsapp/webhook   -> Meta (verificación y mensajes entrantes)
  cola     mensajes-entrantes      -> procesa cada conversación con el agente
  timer    08:00 Lima              -> actualiza el tipo de cambio del BCRP
  GET      /api/salud              -> chequeo rápido
  GET      /api/privacidad         -> política de privacidad pública (requisito de Meta para publicar la app)
  GET      /api/admin/cotizaciones -> CSV para Excel / Power BI   (requiere clave de función)
  GET/POST /api/admin/catalogo     -> descargar / cargar la lista de precios (requiere clave de función)
"""
from __future__ import annotations

import json
import logging

import azure.functions as func

from cotizador import __version__, cargar_config, crear_servicio
from cotizador.catalogo import a_csv
from cotizador.privacidad import pagina_privacidad
from cotizador.whatsapp import verificar_firma

app = func.FunctionApp()
_servicio = None


def servicio():
    """Una instancia por proceso: reutiliza conexiones y la caché del catálogo."""
    global _servicio
    if _servicio is None:
        _servicio = crear_servicio()
    return _servicio


# ----------------------------------------------------------------- WhatsApp

@app.route(route="whatsapp/webhook", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def whatsapp_webhook(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "GET":
        config = cargar_config()
        modo = req.params.get("hub.mode")
        token = req.params.get("hub.verify_token")
        reto = req.params.get("hub.challenge", "")
        if modo == "subscribe" and config.wa_verify_token and token == config.wa_verify_token:
            return func.HttpResponse(reto, status_code=200, mimetype="text/plain")
        return func.HttpResponse("Token de verificación inválido", status_code=403)

    s = servicio()
    cuerpo = req.get_body()
    if not verificar_firma(cuerpo, req.headers.get("X-Hub-Signature-256"), s.config.wa_app_secret):
        logging.warning("Webhook con firma inválida")
        return func.HttpResponse("firma inválida", status_code=401)
    try:
        payload = json.loads(cuerpo or b"{}")
    except json.JSONDecodeError:
        return func.HttpResponse("json inválido", status_code=400)
    try:
        encolados = s.recibir_webhook(payload)
    except Exception:
        logging.exception("Error registrando el webhook")
        return func.HttpResponse("error", status_code=500)  # Meta reintentará
    return func.HttpResponse(json.dumps({"ok": True, "encolados": encolados}), mimetype="application/json")


@app.queue_trigger(arg_name="mensaje", queue_name="mensajes-entrantes", connection="AzureWebJobsStorage")
def procesar_mensaje(mensaje: func.QueueMessage) -> None:
    datos = json.loads(mensaje.get_body().decode("utf-8"))
    resultado = servicio().procesar(datos["wa_id"], datos["msg_id"])
    logging.info("Mensaje %s de %s: %s", datos["msg_id"][-12:], datos["wa_id"][-4:], resultado)


# ----------------------------------------------------------------- tareas programadas

@app.timer_trigger(arg_name="temporizador", schedule="0 0 13 * * *", run_on_startup=False)
def actualizar_tipo_cambio(temporizador: func.TimerRequest) -> None:
    """13:00 UTC = 08:00 en Lima."""
    tc = servicio().tc.actualizar_desde_bcrp()
    logging.info("Tipo de cambio actualizado: %s", tc)


# ----------------------------------------------------------------- utilidades

@app.route(route="salud", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def salud(req: func.HttpRequest) -> func.HttpResponse:
    config = cargar_config()
    estado = {
        "ok": True,
        "version": __version__,
        "almacen": config.almacen,
        "llm": config.llm_proveedor,
        "modo_envio": config.modo_envio,
        "whatsapp_configurado": bool(config.wa_token and config.wa_phone_number_id),
        "firma_webhook_configurada": bool(config.wa_app_secret),
        "admins": len(config.admin_numeros),
    }
    return func.HttpResponse(json.dumps(estado), mimetype="application/json")


@app.route(route="privacidad", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def privacidad(req: func.HttpRequest) -> func.HttpResponse:
    """Política de privacidad pública (Meta la exige para publicar la app)."""
    return func.HttpResponse(pagina_privacidad(cargar_config()), mimetype="text/html", charset="utf-8")


@app.route(route="admin/cotizaciones", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def exportar_cotizaciones(req: func.HttpRequest) -> func.HttpResponse:
    detalle = req.params.get("detalle", "0") in {"1", "true", "si"}
    csv_texto = servicio().exportar_csv(detalle=detalle)
    nombre = "cotizaciones_detalle.csv" if detalle else "cotizaciones.csv"
    return func.HttpResponse("﻿" + csv_texto, mimetype="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={nombre}"})


@app.route(route="admin/catalogo", methods=["GET", "POST"], auth_level=func.AuthLevel.FUNCTION)
def catalogo(req: func.HttpRequest) -> func.HttpResponse:
    s = servicio()
    if req.method == "GET":
        return func.HttpResponse("﻿" + a_csv(list(s.catalogo().productos.values())), mimetype="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=lista_precios.csv"})
    reemplazar = req.params.get("reemplazar", "0") in {"1", "true", "si"}
    cuerpo = req.get_body()
    try:
        texto = cuerpo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = cuerpo.decode("cp1252")  # CSV guardado desde Excel en español
    resultado = s.cargar_catalogo_csv(texto, reemplazar=reemplazar)
    return func.HttpResponse(json.dumps(resultado, ensure_ascii=False), mimetype="application/json",
                             status_code=200 if resultado.get("cargados") else 400)
