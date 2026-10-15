"""WhatsApp, tipo de cambio, adaptadores de IA, almacenamiento y PDF."""
import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from conftest import RESPUESTA_BCRP, FakeHttp
from cotizador.almacenamiento import RepositorioMemoria, _partir, _unir, clave_segura
from cotizador.llm import LLMAnthropic, LLMAzureOpenAI
from cotizador.modelos import Cotizacion, LineaCotizacion, TipoCambio
from cotizador.pdf import generar_pdf
from cotizador.tipo_cambio import ProveedorTipoCambio, parsear_bcrp
from cotizador.whatsapp import parsear_webhook, partir_texto, verificar_firma


# ------------------------------------------------------------- WhatsApp
def test_firma_webhook():
    cuerpo = b'{"entry": []}'
    firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
    assert verificar_firma(cuerpo, firma, "secreto")
    assert not verificar_firma(cuerpo, firma, "otro")
    assert not verificar_firma(cuerpo, None, "secreto")


def test_parsear_tipos_de_mensaje():
    payload = {"entry": [{"changes": [
        {"field": "messages", "value": {
            "contacts": [{"wa_id": "519", "profile": {"name": "Ana"}}],
            "messages": [
                {"from": "519", "id": "m1", "timestamp": "1", "type": "text", "text": {"body": "hola"}},
                {"from": "519", "id": "m2", "timestamp": "2", "type": "image",
                 "image": {"id": "MED1", "mime_type": "image/jpeg", "caption": "mi lista"}},
                {"from": "519", "id": "m3", "timestamp": "3", "type": "audio", "audio": {"id": "MED2", "mime_type": "audio/ogg"}},
                {"from": "519", "id": "m4", "timestamp": "4", "type": "interactive",
                 "interactive": {"button_reply": {"id": "aprobar:COT-1", "title": "Enviar"}}},
            ]}},
        {"field": "smb_message_echoes", "value": {"message_echoes": [
            {"from": "51100", "to": "519", "type": "text", "text": {"body": "le llamo ahora"}}]}},
    ]}]}
    mensajes, ecos = parsear_webhook(payload)
    assert [m.tipo for m in mensajes] == ["text", "image", "audio", "interactive"]
    assert mensajes[0].nombre == "Ana" and mensajes[1].media_id == "MED1" and mensajes[1].texto == "mi lista"
    assert mensajes[3].boton_id == "aprobar:COT-1"
    assert ecos[0].wa_id_cliente == "519" and ecos[0].texto == "le llamo ahora"


def test_partir_texto_largo():
    partes = partir_texto("linea\n" * 2000, limite=500)
    assert all(len(p) <= 500 for p in partes) and len(partes) > 1


# ------------------------------------------------------------- tipo de cambio
def test_parsear_bcrp_ignora_nd():
    tc = parsear_bcrp(RESPUESTA_BCRP)
    assert tc.valor == Decimal("3.457") and tc.fecha == "2026-10-09"


def test_tc_manual_manda_en_el_dia(config):
    repo = RepositorioMemoria()
    http = FakeHttp()
    proveedor = ProveedorTipoCambio(config, repo, http_get=http)
    assert proveedor.obtener().valor == Decimal("3.457")
    proveedor.fijar_manual(Decimal("3.80"))
    assert proveedor.obtener().valor == Decimal("3.80")
    assert http.llamadas == 1


def test_tc_sin_red_usa_respaldo(config):
    config.tc_manual = Decimal("3.70")
    proveedor = ProveedorTipoCambio(config, RepositorioMemoria(), http_get=FakeHttp(falla=True))
    tc = proveedor.obtener()
    assert tc.valor == Decimal("3.70") and "respaldo" in tc.fuente


# ------------------------------------------------------------- adaptadores IA
NEUTRO = [
    {"rol": "usuario", "contenido": [{"tipo": "texto", "texto": "100 fierros de media"},
                                     {"tipo": "imagen", "media_type": "image/jpeg", "datos_b64": "AAA"}]},
    {"rol": "asistente", "texto": "", "llamadas": [{"id": "t1", "nombre": "calcular_cotizacion", "argumentos": {"items": []}}]},
    {"rol": "herramienta", "resultados": [{"id": "t1", "nombre": "calcular_cotizacion", "contenido": "{}"}]},
    {"rol": "usuario", "contenido": [{"tipo": "texto", "texto": "ok"}]},
]


def test_conversion_anthropic_une_turnos_seguidos():
    salida = LLMAnthropic.convertir(NEUTRO)
    assert [m["role"] for m in salida] == ["user", "assistant", "user"]
    assert salida[0]["content"][1]["type"] == "image"
    assert salida[1]["content"][0]["type"] == "tool_use"
    assert [b["type"] for b in salida[2]["content"]] == ["tool_result", "text"]


def test_conversion_azure_openai():
    salida = LLMAzureOpenAI.convertir("sistema", NEUTRO)
    assert [m["role"] for m in salida] == ["system", "user", "assistant", "tool", "user"]
    assert salida[1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert json.loads(salida[2]["tool_calls"][0]["function"]["arguments"]) == {"items": []}


# ------------------------------------------------------------- almacenamiento y PDF
def test_partir_y_unir_textos_grandes():
    texto = "x" * 75000
    partes = _partir("h", texto)
    assert partes["h_n"] == 3 and _unir("h", partes) == texto


def test_clave_segura_sin_caracteres_prohibidos():
    clave = clave_segura("wamid.HBgL/+abc==")
    assert "/" not in clave and len(clave) == 40


def _cotizacion() -> Cotizacion:
    linea = LineaCotizacion("FC-0500", 'Fierro corrugado 1/2" x 9 m', "varilla 9 m", Decimal("10"), Decimal("8.95"),
                            Decimal("89.50"), Decimal("8.50"), Decimal("85.00"), "disponible")
    return Cotizacion(id="COT-000001", fecha="2026-10-14T15:00:00+00:00", wa_id="51987654321",
                      cliente_nombre="Ñandú & Hijos <S.A.C.>", cliente_ruc="", tipo_cliente="publico", lineas=[linea],
                      subtotal_bruto_usd=Decimal("85.00"), descuento_pct=Decimal("0"), descuento_usd=Decimal("0"),
                      base_imponible_usd=Decimal("85.00"), igv_usd=Decimal("15.30"), total_usd=Decimal("100.30"),
                      peso_total_kg=Decimal("89.50"), tipo_cambio=TipoCambio(Decimal("3.457"), "2026-10-09", "BCRP"),
                      total_pen=Decimal("346.74"), vigencia_hasta="2026-10-16T15:00:00+00:00")


def test_cotizacion_ida_y_vuelta_json():
    cot = _cotizacion()
    copia = Cotizacion.de_json(cot.a_json())
    assert copia.total_usd == cot.total_usd and copia.lineas[0].cantidad == 10 and copia.tipo_cambio.valor == Decimal("3.457")


def test_pdf_se_genera_con_caracteres_especiales(config):
    pdf = generar_pdf(_cotizacion(), config)
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def test_sin_app_secret_se_rechaza_todo():
    assert not verificar_firma(b"{}", "sha256=abc", "")


def test_proveedores_compatibles_con_openai(monkeypatch):
    from cotizador.config import cargar_config
    from cotizador.llm import LLMCompatibleOpenAI, crear_llm

    monkeypatch.setenv("LLM_PROVEEDOR", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "clave-de-prueba")
    monkeypatch.delenv("LLM_MODELO", raising=False)
    config = cargar_config()
    llm = crear_llm(config)
    assert isinstance(llm, LLMCompatibleOpenAI) and llm.modelo == "gemini-3.8-flash"
    assert str(llm.cliente.base_url).startswith("https://generativelanguage.googleapis.com/v1beta/openai")

    assert llm.modelo_respaldo == "gemini-3.5-flash-lite"

    monkeypatch.setenv("LLM_PROVEEDOR", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "otra-clave")
    deepseek = crear_llm(cargar_config())
    assert deepseek.modelo == "deepseek-flash" and deepseek.modelo_respaldo == ""


def test_modelo_saturado_pasa_al_respaldo_y_descansa(monkeypatch):
    from types import SimpleNamespace as N

    from cotizador import llm as modulo

    class Saturado(Exception):
        status_code = 503

    class Invalido(Exception):
        status_code = 400

    class Completions:
        def __init__(self, error):
            self.modelos, self.error = [], error

        def create(self, **kwargs):
            self.modelos.append(kwargs["model"])
            if kwargs["model"] == "principal":
                raise self.error
            return N(choices=[N(finish_reason="stop", message=N(content="hola", tool_calls=None))], usage=None)

    monkeypatch.setattr(modulo, "_pausados_hasta", {})
    completions = Completions(Saturado("503 UNAVAILABLE"))
    llm = modulo.LLMFormatoOpenAI(N(chat=N(completions=completions)), "principal", 500, "respaldo")
    assert llm.completar("s", NEUTRO, []).texto == "hola"
    assert llm.completar("s", NEUTRO, []).texto == "hola"
    # 1er turno: principal falla -> respaldo. 2do turno: va directo al respaldo (el principal descansa).
    assert completions.modelos == ["principal", "respaldo", "respaldo"]

    # Un error de la petición (400) no se esconde con el respaldo.
    monkeypatch.setattr(modulo, "_pausados_hasta", {})
    malo = modulo.LLMFormatoOpenAI(N(chat=N(completions=Completions(Invalido("400")))), "principal", 500, "respaldo")
    with pytest.raises(Invalido):
        malo.completar("s", NEUTRO, [])


def test_adaptador_compatible_ejecuta_herramientas():
    from types import SimpleNamespace as N

    from cotizador.llm import LLMFormatoOpenAI

    class Completions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            llamada = N(id="c1", function=N(name="buscar_productos", arguments='{"consulta": "fierro de media"}'))
            return N(choices=[N(finish_reason="tool_calls", message=N(content=None, tool_calls=[llamada]))],
                     usage=N(prompt_tokens=10, completion_tokens=5))

    completions = Completions()
    llm = LLMFormatoOpenAI(N(chat=N(completions=completions)), "gemini-3.8-flash", 500)
    r = llm.completar(["estable", "turno"], NEUTRO, [{"nombre": "buscar_productos", "descripcion": "d",
                                                       "parametros": {"type": "object"}}], forzar_texto=True)
    assert r.llamadas[0].argumentos == {"consulta": "fierro de media"}
    assert completions.kwargs["tool_choice"] == "none" and completions.kwargs["model"] == "gemini-3.8-flash"
    assert completions.kwargs["messages"][0]["content"] == "estable\n\nturno"


def test_politica_de_privacidad_con_datos_de_la_empresa(config):
    from dataclasses import replace

    from cotizador.privacidad import pagina_privacidad

    empresa = replace(config.empresa, nombre="Aceros <Ñandú> & Cía", email="ventas@ejemplo.pe", ruc="20123456789")
    html = pagina_privacidad(replace(config, empresa=empresa, llm_proveedor="gemini"))
    assert "Aceros &lt;Ñandú&gt; &amp; Cía" in html and "<Ñandú>" not in html  # escapado
    assert "RUC 20123456789" in html and "mailto:ventas@ejemplo.pe" in html
    assert "Google (modelo Gemini)" in html and "Ley N.° 29733" in html and 'id="eliminacion"' in html


def test_ruta_de_privacidad_responde_html():
    import azure.functions as func

    import function_app

    r = function_app.privacidad(func.HttpRequest("GET", "/api/privacidad", body=b""))
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert "Política de privacidad" in r.get_body().decode("utf-8")


def test_ninguna_ruta_usa_el_prefijo_reservado_admin():
    # Azure Functions reserva /admin para su API interna: una ruta "admin/..." no carga en Azure.
    from pathlib import Path

    codigo = (Path(__file__).resolve().parent.parent / "function_app.py").read_text(encoding="utf-8")
    assert 'route="admin' not in codigo


def test_gemini3_recibe_de_vuelta_su_firma_de_razonamiento(monkeypatch):
    """Gemini 3 responde 400 si la llamada a herramienta no vuelve con su thought_signature."""
    from types import SimpleNamespace as N

    from cotizador import llm as modulo
    from cotizador.agente import Agente

    firma = {"google": {"thought_signature": "FIRMA-REAL"}}

    class Completions:
        def __init__(self):
            self.pedidos = []

        def create(self, **kwargs):
            self.pedidos.append(kwargs)
            if len(self.pedidos) == 1:
                llamada = N(id="c1", function=N(name="buscar_productos", arguments='{"consulta": "fierro 1/2"}'),
                            extra_content=firma)
                return N(choices=[N(finish_reason="tool_calls", message=N(content=None, tool_calls=[llamada]))],
                         usage=None)
            return N(choices=[N(finish_reason="stop", message=N(content="El fierro de 1/2 está a US$ 8.95", tool_calls=None))],
                     usage=None)

    monkeypatch.setattr(modulo, "_pausados_hasta", {})
    completions = Completions()
    llm = modulo.LLMFormatoOpenAI(N(chat=N(completions=completions)), "gemini-3.8-flash", 500)
    agente = Agente(llm, [{"nombre": "buscar_productos", "descripcion": "d", "parametros": {"type": "object"}}])
    texto, historial, _ = agente.responder("s", [], [{"tipo": "texto", "texto": "fierro de media"}], lambda n, a: "{}")

    assert texto.startswith("El fierro")
    asistente = [m for m in completions.pedidos[1]["messages"] if m["role"] == "assistant"][0]
    assert asistente["tool_calls"][0]["extra_content"] == firma  # se devolvió tal cual
    assert historial[1]["llamadas"][0]["extra"] == firma  # y queda en el historial guardado


def test_firma_rechazada_se_reintenta_con_comodin(monkeypatch):
    from types import SimpleNamespace as N

    from cotizador import llm as modulo

    class ErrorFirma(Exception):
        status_code = 400

    class Completions:
        def __init__(self):
            self.pedidos = []

        def create(self, **kwargs):
            self.pedidos.append(kwargs)
            if len(self.pedidos) == 1:
                raise ErrorFirma("Function call is missing a thought_signature in functionCall parts")
            return N(choices=[N(finish_reason="stop", message=N(content="ok", tool_calls=None))], usage=None)

    monkeypatch.setattr(modulo, "_pausados_hasta", {})
    completions = Completions()
    llm = modulo.LLMFormatoOpenAI(N(chat=N(completions=completions)), "gemini-3.8-flash", 500)
    historial = NEUTRO + [{"rol": "asistente", "texto": "", "llamadas": [
        {"id": "c1", "nombre": "buscar_productos", "argumentos": {}}]},
        {"rol": "herramienta", "resultados": [{"id": "c1", "nombre": "buscar_productos", "contenido": "{}"}]}]
    assert llm.completar("s", historial, []).texto == "ok"
    reintento = [m for m in completions.pedidos[1]["messages"] if m.get("tool_calls")][-1]
    assert reintento["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == modulo.FIRMA_COMODIN
