"""Prueba de integración contra Azurite (emulador oficial de Azure Storage).

Se ejecuta solo si AZURITE=1. En CI se levanta Azurite como contenedor de servicio.
Localmente:  npx azurite --silent --skipApiVersionCheck  &&  AZURITE=1 pytest tests/test_azure_azurite.py
"""
import base64
import json
import os
import uuid

import pytest

from conftest import ADMIN, CLIENTE, AHORA, FakeHttp, FakeLLM, llamada, texto, webhook_boton, webhook_texto
from cotizador.almacenamiento import ArchivosBlob, ColaAzure, RepositorioTablas
from cotizador.servicio import Servicio
from cotizador.tipo_cambio import ProveedorTipoCambio
from cotizador.whatsapp import ClienteConsola

pytestmark = pytest.mark.skipif(os.environ.get("AZURITE") != "1", reason="requiere Azurite (AZURITE=1)")

AZURITE = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
           "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
           "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;"
           "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;")


@pytest.fixture
def servicio_azure(config, tmp_path):
    sufijo = uuid.uuid4().hex[:6]
    config.storage_connection_string = AZURITE
    config.cola_mensajes = f"mensajes-{sufijo}"
    config.contenedor_pdfs = f"cotizaciones-{sufijo}"
    config.prefijo_tablas = f"t{sufijo}"
    repo = RepositorioTablas(config)
    llm = FakeLLM([llamada("generar_cotizacion", items=[{"sku": "FC-0500", "cantidad": 1, "unidad_pedida": "tonelada"}],
                           cliente_nombre="Constructora Andina S.A.C.", cliente_ruc="20601234567"),
                   texto("Su cotización está lista.")])
    return Servicio(config=config, repo=repo, archivos=ArchivosBlob(config), cola=ColaAzure(config),
                    whatsapp=ClienteConsola(tmp_path, imprimir=False), llm=llm,
                    tc=ProveedorTipoCambio(config, repo, http_get=FakeHttp()), reloj=lambda: AHORA)


def _vaciar_cola(s):
    procesados = []
    for _ in range(10):
        mensajes = list(s.cola.cliente.receive_messages(visibility_timeout=5, max_messages=32))
        if not mensajes:
            break
        for m in mensajes:
            datos = json.loads(base64.b64decode(m.content).decode())  # el trigger de Functions hace lo mismo
            procesados.append(s.procesar(datos["wa_id"], datos["msg_id"]))
            s.cola.cliente.delete_message(m)
    return procesados


def test_flujo_completo_en_azure_storage(servicio_azure):
    s = servicio_azure
    s.config.debounce_segundos = 0
    assert s.recibir_webhook(webhook_texto(CLIENTE, "una tonelada de fierro de media", "wamid.ABC/+1==", ts=200)) == 1
    assert s.recibir_webhook(webhook_texto(CLIENTE, "una tonelada de fierro de media", "wamid.ABC/+1==", ts=200)) == 0
    assert _vaciar_cola(s) == ["respondido"]

    cot = s.repo.obtener_cotizacion("COT-000001")
    assert cot.estado == "pendiente_aprobacion" and cot.lineas[0].cantidad == 112
    assert s.archivos.leer(cot.pdf_ruta).startswith(b"%PDF")
    assert len(s.repo.listar_productos()) == 53                     # catálogo sembrado desde el CSV

    s.recibir_webhook(webhook_boton(ADMIN, "aprobar:COT-000001", "wamid.BTN1"))
    _vaciar_cola(s)
    assert s.repo.obtener_cotizacion("COT-000001").estado == "enviada"
    assert s.repo.siguiente_correlativo("cotizacion") == 2
    conv = s.repo.obtener_conversacion(CLIENTE)
    assert conv.cliente_ruc == "20601234567" and len(conv.historial) >= 4
    assert not s.repo.listar_pendientes(CLIENTE)


def test_candado_y_modo_en_table_storage(servicio_azure):
    from datetime import timedelta
    repo = servicio_azure.repo
    token = repo.adquirir_candado(CLIENTE, 60, AHORA)
    assert token and repo.adquirir_candado(CLIENTE, 60, AHORA) is None          # ocupado
    assert repo.adquirir_candado(CLIENTE, 60, AHORA + timedelta(seconds=61))     # vencido: se toma
    repo.liberar_candado(CLIENTE, token)                                         # token viejo: no libera
    assert repo.adquirir_candado(CLIENTE, 60, AHORA + timedelta(seconds=62)) is None
    repo.fijar_modo(CLIENTE, "humano", "")
    assert repo.obtener_modo(CLIENTE) == ("humano", "")
    assert not repo.mensaje_registrado("wamid.Z/+")
    repo.registrar_mensaje("wamid.Z/+")
    assert repo.mensaje_registrado("wamid.Z/+")
