"""Configuración central del cotizador.

Todo se lee de variables de entorno (App Settings en Azure, local.settings.json
en desarrollo). Así el mismo código sirve para cualquier cliente: solo cambia
la configuración y la lista de precios.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


def _env(nombre: str, defecto: str = "") -> str:
    return os.environ.get(nombre, defecto).strip()


def _env_bool(nombre: str, defecto: bool) -> bool:
    valor = _env(nombre, "")
    if not valor:
        return defecto
    return valor.lower() in {"1", "true", "si", "sí", "yes", "y"}


def _env_dec(nombre: str, defecto: str) -> Decimal:
    return Decimal(_env(nombre, defecto) or defecto)


def _env_int(nombre: str, defecto: int) -> int:
    try:
        return int(_env(nombre, str(defecto)))
    except ValueError:
        return defecto


def _parse_pares(texto: str) -> dict[str, Decimal]:
    """Convierte 'maestro:0.02,ferreteria:0.05' en {'maestro': Decimal('0.02'), ...}."""
    resultado: dict[str, Decimal] = {}
    for parte in texto.split(","):
        if ":" not in parte:
            continue
        clave, valor = parte.split(":", 1)
        resultado[clave.strip().lower()] = Decimal(valor.strip())
    return resultado


RAIZ_PROYECTO = Path(__file__).resolve().parent.parent


def ruta_proyecto(ruta: str) -> str:
    """Rutas relativas se resuelven desde la raíz del proyecto (en Azure: /home/site/wwwroot)."""
    p = Path(ruta)
    return str(p if p.is_absolute() else RAIZ_PROYECTO / p)


def _parse_lista(texto: str) -> list[str]:
    return [x.strip() for x in texto.replace(";", ",").split(",") if x.strip()]


@dataclass
class Empresa:
    nombre: str
    ruc: str
    direccion: str
    telefono: str
    email: str
    web: str
    cuentas: list[str]
    condiciones: list[str]
    flete: str
    logo_ruta: str
    color_primario: str = "#0168AA"
    color_acento: str = "#00A591"
    pdf_marca: str = ""
    lema: str = ""


@dataclass
class Config:
    empresa: Empresa

    # Reglas comerciales
    igv_tasa: Decimal = Decimal("0.18")
    precios_incluyen_igv: bool = False
    vigencia_horas: int = 48
    mostrar_soles: bool = True
    tc_fuente: str = "bcrp"            # bcrp | manual
    tc_manual: Decimal = Decimal("0")
    tc_spread: Decimal = Decimal("0")
    dias_alerta_precio: int = 7
    descuentos_tipo_cliente: dict[str, Decimal] = field(default_factory=dict)
    descuentos_volumen_kg: dict[str, Decimal] = field(default_factory=dict)
    descuento_maximo: Decimal = Decimal("0.08")

    # Flujo
    modo_envio: str = "aprobacion"     # aprobacion | automatico
    admin_numeros: list[str] = field(default_factory=list)
    debounce_segundos: int = 6
    humano_pausa_horas: int = 4
    historial_max_mensajes: int = 24
    catalogo_en_prompt_max: int = 400
    zona_horaria: str = "America/Lima"
    horario_atencion: str = "lunes a sábado de 8:00 a.m. a 6:00 p.m."

    # WhatsApp Cloud API
    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_app_secret: str = ""
    wa_verify_token: str = ""
    wa_graph_version: str = "v23.0"
    wa_plantilla_aprobacion: str = ""  # opcional, plantilla de utilidad aprobada por Meta
    wa_plantilla_idioma: str = "es"

    # IA
    llm_proveedor: str = "anthropic"   # anthropic | azure_openai | gemini | deepseek | openai_compatible
    llm_modelo: str = "claude-haiku-5-5"
    llm_modelo_respaldo: str = ""      # se usa si el principal está saturado (ej. Gemini gratis)
    llm_max_tokens: int = 1500
    anthropic_api_key: str = ""
    llm_api_key: str = ""              # proveedores compatibles con OpenAI (Gemini, DeepSeek...)
    llm_base_url: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str = ""
    transcripcion_proveedor: str = "none"  # none | azure_openai
    azure_openai_deployment_transcripcion: str = ""

    # Almacenamiento
    almacen: str = "memoria"           # memoria | azure
    storage_connection_string: str = ""
    storage_account_name: str = ""
    prefijo_tablas: str = ""
    cola_mensajes: str = "mensajes-entrantes"
    contenedor_pdfs: str = "cotizaciones"
    catalogo_csv_inicial: str = "data/catalogo_ejemplo.csv"

    @property
    def es_aprobacion(self) -> bool:
        return self.modo_envio.lower() != "automatico"

    def es_admin(self, wa_id: str) -> bool:
        return _solo_digitos(wa_id) in {_solo_digitos(n) for n in self.admin_numeros}


def _solo_digitos(numero: str) -> str:
    return "".join(c for c in numero if c.isdigit())


def cargar_config() -> Config:
    empresa = Empresa(
        nombre=_env("EMPRESA_NOMBRE", "Master Steel"),
        ruc=_env("EMPRESA_RUC", ""),
        direccion=_env("EMPRESA_DIRECCION", "Callao, Perú"),
        telefono=_env("EMPRESA_TELEFONO", ""),
        email=_env("EMPRESA_EMAIL", ""),
        web=_env("EMPRESA_WEB", ""),
        cuentas=[c.strip() for c in _env(
            "EMPRESA_CUENTAS", "BCP US$ 000-0000000-1-00 (ejemplo) | BCP S/ 000-0000000-0-00 (ejemplo)").split("|")
            if c.strip()],
        condiciones=[c.strip() for c in _env(
            "EMPRESA_CONDICIONES",
            "Precios en dólares americanos. | Stock sujeto a confirmación al momento de la orden. | "
            "Pago contado o según línea de crédito aprobada. | Pesos teóricos referenciales.",
        ).split("|") if c.strip()],
        flete=_env("EMPRESA_FLETE", "Despacho desde nuestro almacén en el Callao; flete a coordinar según zona de entrega."),
        logo_ruta=ruta_proyecto(_env("EMPRESA_LOGO", "assets/logo.png")),
        color_primario=_env("EMPRESA_COLOR_PRIMARIO", "#0168AA"),
        color_acento=_env("EMPRESA_COLOR_ACENTO", "#00A591"),
        pdf_marca=_env("PDF_MARCA", "Cotizador IA by DataPrix"),
        lema=_env("EMPRESA_LEMA", "Los Maestros del Acero"),
    )
    almacen = _env("ALMACEN", "")
    if not almacen:
        almacen = "azure" if (_env("STORAGE_CONNECTION_STRING") or _env("STORAGE_ACCOUNT_NAME")) else "memoria"

    proveedor = _env("LLM_PROVEEDOR", "anthropic").lower()
    from .llm import MODELOS_RESPALDO, PROVEEDORES_COMPATIBLES  # import tardío: llm.py importa Config

    base_url, modelo_defecto, var_clave = PROVEEDORES_COMPATIBLES.get(proveedor, ("", "", "LLM_API_KEY"))
    if proveedor == "anthropic":
        modelo_defecto = "claude-haiku-5-5"
    elif proveedor == "azure_openai":
        modelo_defecto = ""
    respaldo = _env("LLM_MODELO_RESPALDO", MODELOS_RESPALDO.get(proveedor, "")) or MODELOS_RESPALDO.get(proveedor, "")
    if respaldo.lower() == "ninguno":
        respaldo = ""

    return Config(
        empresa=empresa,
        igv_tasa=_env_dec("IGV_TASA", "0.18"),
        precios_incluyen_igv=_env_bool("PRECIOS_INCLUYEN_IGV", False),
        vigencia_horas=_env_int("VIGENCIA_HORAS", 48),
        mostrar_soles=_env_bool("MOSTRAR_SOLES", True),
        tc_fuente=_env("TC_FUENTE", "bcrp").lower(),
        tc_manual=_env_dec("TC_MANUAL", "0"),
        tc_spread=_env_dec("TC_SPREAD", "0"),
        dias_alerta_precio=_env_int("DIAS_ALERTA_PRECIO", 7),
        descuentos_tipo_cliente=_parse_pares(_env("DESCUENTOS_TIPO_CLIENTE", "publico:0,maestro:0.02,ferreteria:0.05,empresa:0.03")),
        descuentos_volumen_kg=_parse_pares(_env("DESCUENTOS_VOLUMEN_KG", "1000:0.02,5000:0.04")),
        descuento_maximo=_env_dec("DESCUENTO_MAXIMO", "0.08"),
        modo_envio=_env("MODO_ENVIO", "aprobacion").lower(),
        admin_numeros=_parse_lista(_env("ADMIN_NUMEROS", "")),
        debounce_segundos=_env_int("DEBOUNCE_SEGUNDOS", 6),
        humano_pausa_horas=_env_int("HUMANO_PAUSA_HORAS", 4),
        historial_max_mensajes=_env_int("HISTORIAL_MAX_MENSAJES", 24),
        catalogo_en_prompt_max=_env_int("CATALOGO_EN_PROMPT_MAX", 400),
        zona_horaria=_env("ZONA_HORARIA", "America/Lima"),
        horario_atencion=_env("HORARIO_ATENCION", "lunes a sábado de 8:00 a.m. a 6:00 p.m."),
        wa_token=_env("WA_TOKEN"),
        wa_phone_number_id=_env("WA_PHONE_NUMBER_ID"),
        wa_app_secret=_env("WA_APP_SECRET"),
        wa_verify_token=_env("WA_VERIFY_TOKEN"),
        wa_graph_version=_env("WA_GRAPH_VERSION", "v23.0"),
        wa_plantilla_aprobacion=_env("WA_PLANTILLA_APROBACION"),
        wa_plantilla_idioma=_env("WA_PLANTILLA_IDIOMA", "es"),
        llm_proveedor=proveedor,
        llm_modelo=_env("LLM_MODELO", modelo_defecto) or modelo_defecto,
        llm_modelo_respaldo=respaldo,
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 1500),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        llm_api_key=_env(var_clave) or _env("LLM_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL", base_url) or base_url,
        azure_openai_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
        azure_openai_api_key=_env("AZURE_OPENAI_API_KEY"),
        azure_openai_api_version=_env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_openai_deployment=_env("AZURE_OPENAI_DEPLOYMENT"),
        transcripcion_proveedor=_env("TRANSCRIPCION_PROVEEDOR", "none").lower(),
        azure_openai_deployment_transcripcion=_env("AZURE_OPENAI_DEPLOYMENT_TRANSCRIPCION"),
        almacen=almacen,
        storage_connection_string=_env("STORAGE_CONNECTION_STRING"),
        storage_account_name=_env("STORAGE_ACCOUNT_NAME"),
        prefijo_tablas="".join(c for c in _env("PREFIJO_TABLAS", "") if c.isalnum()),
        cola_mensajes=_env("COLA_MENSAJES", "mensajes-entrantes"),
        contenedor_pdfs=_env("CONTENEDOR_PDFS", "cotizaciones"),
        catalogo_csv_inicial=ruta_proyecto(_env("CATALOGO_CSV_INICIAL", "data/catalogo_ejemplo.csv")),
    )
