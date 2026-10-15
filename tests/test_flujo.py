"""Flujo completo: webhook -> cola -> agente -> cotización -> aprobación -> cliente."""
import json
from decimal import Decimal

from conftest import ADMIN, CLIENTE, llamada, procesar_cola, texto, webhook_boton, webhook_texto

PEDIDO = [{"sku": "FC-0500", "cantidad": 100}, {"sku": "AL-16", "cantidad": 10, "unidad_pedida": "kg"}]


def _enviados(servicio, para, tipo=None):
    return [e for e in servicio.whatsapp.enviados if e["para"] == para and (tipo is None or e["tipo"] == tipo)]


def test_mensajes_seguidos_se_responden_juntos(armar):
    s = armar([texto("¡Hola Juan! ¿Cuántas varillas de 1/2\" necesita?")])
    s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1", ts=100))
    s.recibir_webhook(webhook_texto(CLIENTE, "quiero fierro de media", "m2", ts=101))
    assert [d for _, d in s.cola.mensajes] == [6, 6]           # espera de agrupación
    assert procesar_cola(s) == ["omitido", "respondido"]
    turno = s.llm.llamadas[0]["mensajes"][-1]["contenido"]
    assert [c["texto"] for c in turno] == ["hola", "quiero fierro de media"]
    assert "fierro corrugado" in s.llm.llamadas[0]["sistema"].lower()  # catálogo en el prompt
    assert s.llm.llamadas[0]["sistema"].index("CATÁLOGO") < s.llm.llamadas[0]["sistema"].index("CONTEXTO DEL TURNO")
    assert _enviados(s, CLIENTE, "texto")[0]["texto"].startswith("¡Hola Juan!")


def test_webhook_duplicado_se_ignora(armar):
    s = armar([texto("ok")])
    assert s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1")) == 1
    assert s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1")) == 0


def test_cotizacion_con_aprobacion_del_vendedor(armar):
    s = armar([
        llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="Juan Pérez", tipo_cliente="maestro"),
        lambda sis, msgs, h: texto("Listo, su cotización " + json.loads(msgs[-1]["resultados"][0]["contenido"])["cotizacion"]
                                   + " está lista. Un asesor se la envía en minutos."),
    ])
    s.recibir_webhook(webhook_texto(ADMIN, "/pendientes", "a0", ts=90))   # el vendedor abre su ventana del día
    procesar_cola(s)
    s.recibir_webhook(webhook_texto(CLIENTE, "cotízame 100 fierros de media y 10 kilos de alambre", "m1"))
    procesar_cola(s)

    cot = s.repo.obtener_cotizacion("COT-000001")
    assert cot.estado == "pendiente_aprobacion" and cot.tipo_cliente == "maestro"
    assert cot.descuento_pct > 0                                   # descuento de maestro
    assert _enviados(s, ADMIN, "documento") and _enviados(s, ADMIN, "botones")
    assert not _enviados(s, CLIENTE, "documento")                  # aún no llega al cliente
    assert "COT-000001" in _enviados(s, CLIENTE, "texto")[0]["texto"]

    s.recibir_webhook(webhook_boton(ADMIN, "aprobar:COT-000001", "b1"))
    procesar_cola(s)
    assert s.repo.obtener_cotizacion("COT-000001").estado == "enviada"
    assert _enviados(s, CLIENTE, "documento")[0]["archivo"].endswith("Cotizacion_COT-000001.pdf")
    historial = s.repo.obtener_conversacion(CLIENTE).historial
    assert any("COT-000001" in json.dumps(m, ensure_ascii=False) for m in historial)

    # un segundo clic no reenvía
    s.recibir_webhook(webhook_boton(ADMIN, "aprobar:COT-000001", "b2", ts=1760454200))
    procesar_cola(s)
    assert len(_enviados(s, CLIENTE, "documento")) == 1


def test_modo_automatico_envia_pdf_directo(armar):
    s = armar([llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="Juan"), texto("Le envié su cotización.")],
              modo_envio="automatico")
    s.recibir_webhook(webhook_texto(CLIENTE, "100 fierros de media", "m1"))
    procesar_cola(s)
    assert s.repo.obtener_cotizacion("COT-000001").estado == "enviada"
    assert len(_enviados(s, CLIENTE, "documento")) == 1 and not _enviados(s, ADMIN)


def test_errores_del_motor_vuelven_al_agente(armar):
    s = armar([llamada("generar_cotizacion", items=[{"sku": "PG-090", "cantidad": 3}], cliente_nombre="Juan"),
               texto("Esa plancha está agotada, ¿le cotizo la de 0.50 mm?")])
    s.recibir_webhook(webhook_texto(CLIENTE, "3 planchas galvanizadas 0.9", "m1"))
    procesar_cola(s)
    resultado = json.loads(s.llm.llamadas[1]["mensajes"][-1]["resultados"][0]["contenido"])
    assert resultado["ok"] is False and "agotado" in resultado["errores"][0]
    assert not s.repo.listar_cotizaciones()


def test_derivar_a_asesor_pausa_el_bot(armar):
    s = armar([llamada("derivar_a_asesor", motivo="Quiere crédito a 30 días"), texto("Un asesor lo contactará.")])
    s.recibir_webhook(webhook_texto(CLIENTE, "me dan crédito?", "m1"))
    procesar_cola(s)
    assert "crédito" in _enviados(s, ADMIN, "texto")[0]["texto"]
    s.recibir_webhook(webhook_texto(CLIENTE, "aló?", "m2", ts=1760454999))
    assert procesar_cola(s) == ["pausado"]
    assert len(s.llm.llamadas) == 2                                  # no se volvió a llamar al modelo

    s.recibir_webhook(webhook_texto(ADMIN, f"/bot {CLIENTE}", "a1"))
    procesar_cola(s)
    assert s.repo.obtener_conversacion(CLIENTE).modo == "bot"


def test_eco_de_la_app_business_pausa_el_bot(armar):
    s = armar([])
    s.recibir_webhook({"entry": [{"changes": [{"field": "smb_message_echoes", "value": {"message_echoes": [
        {"from": "51100", "to": CLIENTE, "type": "text", "text": {"body": "Hola, soy Carlos de ventas"}}]}}]}]})
    conv = s.repo.obtener_conversacion(CLIENTE)
    assert conv.modo == "humano" and "Carlos" in conv.historial[-1]["texto"]


def test_falla_del_modelo_avisa_y_no_se_cae(armar):
    s = armar([RuntimeError("timeout")])
    s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1"))
    assert procesar_cola(s) == ["respondido"]
    assert "inconveniente" in _enviados(s, CLIENTE, "texto")[0]["texto"]
    assert "no pudo responder" in _enviados(s, ADMIN, "texto")[0]["texto"]


def test_comandos_de_administrador(armar):
    s = armar([llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="Juan"), texto("Lista.")])
    s.recibir_webhook(webhook_texto(CLIENTE, "100 fierros de media", "m1"))
    procesar_cola(s)

    comandos = ["/tc 3.80", "/precio FC-0500 9.10", "/stock FC-0500 consultar", "/buscar fierro de media",
                "/ganada COT-000001", "/resumen 7", "/comando-raro"]
    for i, comando in enumerate(comandos):
        s.recibir_webhook(webhook_texto(ADMIN, comando, f"a{i}", ts=1760455000 + i))
        procesar_cola(s)
    respuestas = [e["texto"] for e in _enviados(s, ADMIN, "texto")]
    assert "3.80" in respuestas[0]
    assert "9.10" in respuestas[1] and s.catalogo().obtener("FC-0500").precio_usd == Decimal("9.10")
    assert "consultar" in respuestas[2]
    assert "FC-0500" in respuestas[3] and "9.10" in respuestas[3]
    assert s.repo.obtener_cotizacion("COT-000001").estado == "ganada"
    assert "Tasa de cierre: 100%" in respuestas[5]
    assert "No conozco" in respuestas[6]
    assert len(s.llm.llamadas) == 2                                  # los comandos no pasan por la IA


def test_audio_sin_transcriptor_pide_texto(armar):
    s = armar([texto("¿Me lo puede escribir?")])
    s.recibir_webhook({"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": CLIENTE, "profile": {"name": "Juan"}}],
        "messages": [{"from": CLIENTE, "id": "au1", "timestamp": "5", "type": "audio",
                      "audio": {"id": "MED", "mime_type": "audio/ogg"}}]}}]}]})
    procesar_cola(s)
    contenido = s.llm.llamadas[0]["mensajes"][-1]["contenido"][0]["texto"]
    assert "audio" in contenido and "escriba" in contenido


def test_exportar_csv(armar):
    s = armar([llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="Juan"), texto("ok")])
    s.recibir_webhook(webhook_texto(CLIENTE, "pedido", "m1"))
    procesar_cola(s)
    resumen = s.exportar_csv().splitlines()
    detalle = s.exportar_csv(detalle=True).splitlines()
    assert resumen[0].startswith("cotizacion,fecha,estado") and len(resumen) == 2
    assert len(detalle) == 3


def test_carga_parcial_de_precios_conserva_lo_demas(armar):
    s = armar([])
    antes = s.catalogo().obtener("FC-0500")
    resultado = s.cargar_catalogo_csv("sku;precio_usd\nfc-0500;8,75\nNUEVO-1;5\n")
    despues = s.catalogo().obtener("FC-0500")
    assert resultado["cargados"] == 1 and "sin descripción" in resultado["errores"][0]
    assert despues.precio_usd == Decimal("8.75") and despues.sinonimos == antes.sinonimos
    assert despues.peso_kg == antes.peso_kg and despues.actualizado == "2026-10-14"
    assert len(s.catalogo()) == 53


# ---------------------------------------------------------------- robustez (revisión de código)

def test_turno_en_curso_reencola(armar):
    s = armar([texto("hola")])
    s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1"))
    s.repo.adquirir_candado(CLIENTE, 180, s.reloj())          # otro proceso atiende a este cliente
    mensaje, _ = s.cola.mensajes.pop(0)
    assert s.procesar(mensaje["wa_id"], mensaje["msg_id"]) == "reencolado"
    assert s.cola.mensajes == [({"wa_id": CLIENTE, "msg_id": "m1"}, 8)]
    s.reloj.avanzar(seconds=181)                                # el candado vence (proceso caído)
    assert procesar_cola(s) == ["respondido"]


def test_si_falla_la_cola_meta_puede_reintentar(armar):
    s = armar([texto("hola")])
    original = s.cola.enviar

    def falla(*args, **kwargs):
        raise ConnectionError("cola caída")
    s.cola.enviar = falla
    try:
        s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1"))
    except ConnectionError:
        pass
    s.cola.enviar = original
    assert s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1")) == 1   # el reintento de Meta sí entra
    assert procesar_cola(s) == ["respondido"]


def test_cierre_forzado_envia_herramientas_sin_usarlas(armar):
    s = armar([llamada("buscar_productos", consulta="fierro")] * 6 + [texto("Le confirmo en breve.")])
    s.recibir_webhook(webhook_texto(CLIENTE, "fierro", "m1"))
    procesar_cola(s)
    ultima = s.llm.llamadas[-1]
    assert ultima["forzar_texto"] is True and ultima["herramientas"]
    assert _enviados(s, CLIENTE, "texto")[0]["texto"] == "Le confirmo en breve."


def test_respuesta_vacia_tiene_respaldo(armar):
    s = armar([texto("")])
    s.recibir_webhook(webhook_texto(CLIENTE, "...", "m1"))
    procesar_cola(s)
    assert "no le entendí" in _enviados(s, CLIENTE, "texto")[0]["texto"]


def test_pausa_del_vendedor_no_se_pierde_durante_un_turno(armar):
    def eco_durante_el_turno(sistema, mensajes, herramientas):
        s._registrar_eco(__import__("cotizador.whatsapp", fromlist=["EventoEco"]).EventoEco(CLIENTE, "yo lo atiendo"))
        return texto("Claro, le ayudo.")
    s = armar([eco_durante_el_turno])
    s.recibir_webhook(webhook_texto(CLIENTE, "hola", "m1"))
    procesar_cola(s)
    assert s.repo.obtener_modo(CLIENTE)[0] == "humano"


def test_reintento_del_turno_no_duplica_cotizacion(armar):
    from cotizador.modelos import Conversacion
    from cotizador.servicio import Herramientas
    s = armar([], modo_envio="automatico")
    s.repo.marcar_entrante(CLIENTE, "2026-10-14T14:59:00+00:00")
    for _ in range(2):  # la cola reintenta el mismo turno
        h = Herramientas(s, Conversacion(wa_id=CLIENTE, nombre="Juan"), turno_id="wamid.X")
        h.ejecutar("generar_cotizacion", {"items": PEDIDO, "cliente_nombre": "Juan"})
    assert len(s.repo.listar_cotizaciones()) == 1 and len(_enviados(s, CLIENTE, "documento")) == 1


def test_ventana_cerrada_del_vendedor_usa_plantilla(armar):
    s = armar([llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="Juan"), texto("Lista.")],
              wa_plantilla_aprobacion="cotizacion_pendiente")
    s.recibir_webhook(webhook_texto(CLIENTE, "pedido", "m1"))
    procesar_cola(s)
    plantilla = _enviados(s, ADMIN, "plantilla")[0]
    assert plantilla["nombre"] == "cotizacion_pendiente" and plantilla["parametros"][0] == "COT-000001"
    assert not _enviados(s, ADMIN, "documento")
    # el vendedor toca "Ver pendientes" en la plantilla -> recibe los botones
    s.recibir_webhook({"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"from": ADMIN, "id": "t1", "timestamp": "9", "type": "button", "button": {"payload": "Ver pendientes",
                                                                                  "text": "Ver pendientes"}}]}}]}]})
    procesar_cola(s)
    assert _enviados(s, ADMIN, "botones")


def test_aprobar_con_ventana_del_cliente_vencida_avisa(armar):
    s = armar([llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="Juan"), texto("Lista.")])
    s.recibir_webhook(webhook_texto(CLIENTE, "pedido", "m1"))
    procesar_cola(s)
    s.reloj.avanzar(hours=30)
    s.recibir_webhook(webhook_boton(ADMIN, "aprobar:COT-000001", "b1"))
    procesar_cola(s)
    assert "24 h" in _enviados(s, ADMIN, "texto")[-1]["texto"]
    assert s.repo.obtener_cotizacion("COT-000001").estado == "pendiente_aprobacion"


def test_fallo_de_entrega_avisa_al_vendedor(armar):
    s = armar([])
    s.recibir_webhook({"entry": [{"changes": [{"field": "messages", "value": {"statuses": [
        {"id": "wamid.Y", "status": "failed", "recipient_id": CLIENTE,
         "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]})
    assert "24 h" in _enviados(s, ADMIN, "texto")[0]["texto"]


def test_reemplazo_con_errores_no_borra_productos(armar):
    s = armar([])
    resultado = s.cargar_catalogo_csv("sku,descripcion,peso_kg,precio_usd\nFC-0500,Fierro,8.95,8.5\nFC-0375,Fierro,x,4\n",
                                      reemplazar=True)
    assert resultado["ok"] is False and resultado["cargados"] == 0
    assert len(s.catalogo()) == 53


def test_exportacion_csv_sin_formulas(armar):
    s = armar([llamada("generar_cotizacion", items=PEDIDO, cliente_nombre="=HYPERLINK(\"http://x\")"), texto("ok")])
    s.recibir_webhook(webhook_texto(CLIENTE, "pedido", "m1"))
    procesar_cola(s)
    assert "'=HYPERLINK" in s.exportar_csv()
