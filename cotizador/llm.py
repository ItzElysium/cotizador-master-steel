"""Adaptadores de modelos de lenguaje.

El agente trabaja con un formato de mensajes neutro y estos adaptadores lo
traducen al proveedor elegido:

- Anthropic (Claude) vía API directa  -> LLM_PROVEEDOR=anthropic
- Azure OpenAI (GPT-4o / 4.1 mini...) -> LLM_PROVEEDOR=azure_openai

- Gemini, DeepSeek u otro compatible con OpenAI -> LLM_PROVEEDOR=gemini | deepseek | openai_compatible

Formato neutro:
  {"rol": "usuario", "contenido": [{"tipo": "texto", "texto": ...},
                                    {"tipo": "imagen", "media_type": "image/jpeg", "datos_b64": ...},
                                    {"tipo": "pdf", "datos_b64": ...}]}
  {"rol": "asistente", "texto": "...", "llamadas": [{"id": ..., "nombre": ..., "argumentos": {...}}]}
  {"rol": "herramienta", "resultados": [{"id": ..., "nombre": ..., "contenido": "..."}]}
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict[str, Any]
    # Metadatos del proveedor que hay que devolver tal cual en la siguiente llamada. Gemini 3 manda aquí
    # su "thought_signature" y responde 400 si no se le devuelve.
    extra: dict[str, Any] | None = None


@dataclass
class RespuestaLLM:
    texto: str
    llamadas: list[LlamadaHerramienta] = field(default_factory=list)
    uso: dict[str, int] = field(default_factory=dict)


# `sistema` puede ser un texto o [parte estable (se cachea), parte del turno (fecha, datos del cliente)].
class ProveedorLLM(Protocol):
    soporta_pdf: bool

    def completar(self, sistema: str | list[str], mensajes: list[dict[str, Any]],
                  herramientas: list[dict[str, Any]], forzar_texto: bool = False) -> RespuestaLLM: ...


def _partes(sistema: str | list[str]) -> list[str]:
    return [sistema] if isinstance(sistema, str) else [s for s in sistema if s]


# Proveedores con endpoint compatible con OpenAI: (base_url, modelo por defecto, variable de la clave)
PROVEEDORES_COMPATIBLES: dict[str, tuple[str, str, str]] = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.8-flash", "GEMINI_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "deepseek-flash", "DEEPSEEK_API_KEY"),
    "openai_compatible": ("", "", "LLM_API_KEY"),
}

# Modelo de respaldo por defecto: se usa si el principal está saturado (503/429...). LLM_MODELO_RESPALDO lo cambia.
MODELOS_RESPALDO: dict[str, str] = {"gemini": "gemini-3.5-flash-lite"}

# Tras una saturación, el modelo principal se deja descansar este tiempo y se va directo al respaldo.
SEGUNDOS_PAUSA_PRINCIPAL = 600
_ESTADOS_SATURACION = {408, 429, 500, 502, 503, 504, 529}
_pausados_hasta: dict[str, float] = {}


# Firma comodín que Gemini acepta cuando la firma real viene de otro modelo (p. ej. al pasar al respaldo a
# mitad de un turno): las firmas solo son válidas para el modelo que las generó.
FIRMA_COMODIN = "skip_thought_signature_validator"


def _con_firmas_comodin(mensajes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pone la firma comodín en la primera llamada de cada paso (donde Gemini la exige)."""
    salida = []
    for m in mensajes:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            primera, *resto = m["tool_calls"]
            extra = primera.get("extra_content") or {}
            primera = {**primera, "extra_content": {**extra, "google": {**(extra.get("google") or {}),
                                                                          "thought_signature": FIRMA_COMODIN}}}
            m = {**m, "tool_calls": [primera, *resto]}
        salida.append(m)
    return salida


def _es_error_de_firma(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400 and "signature" in str(exc).lower()


def _es_saturacion(exc: Exception) -> bool:
    """Errores pasajeros del proveedor (saturación, límite de uso, caída), no errores de la petición."""
    if getattr(exc, "status_code", None) in _ESTADOS_SATURACION:
        return True
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}


# ============================================================ Anthropic

class LLMAnthropic:
    soporta_pdf = True

    def __init__(self, config: Config, cliente: Any = None):
        if cliente is None:
            import anthropic

            cliente = anthropic.Anthropic(api_key=config.anthropic_api_key, max_retries=2, timeout=60)
        self.cliente = cliente
        self.modelo = config.llm_modelo
        self.max_tokens = config.llm_max_tokens

    @staticmethod
    def convertir(mensajes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        salida: list[dict[str, Any]] = []
        for m in mensajes:
            if m["rol"] == "usuario":
                bloques = []
                for c in m["contenido"]:
                    if c["tipo"] == "texto" and c.get("texto"):
                        bloques.append({"type": "text", "text": c["texto"]})
                    elif c["tipo"] == "imagen":
                        bloques.append({"type": "image", "source": {"type": "base64", "media_type": c["media_type"],
                                                                     "data": c["datos_b64"]}})
                    elif c["tipo"] == "pdf":
                        bloques.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                                        "data": c["datos_b64"]}})
                rol, bloques = "user", bloques or [{"type": "text", "text": "(mensaje vacío)"}]
            elif m["rol"] == "asistente":
                bloques = [{"type": "text", "text": m["texto"]}] if m.get("texto") else []
                bloques += [{"type": "tool_use", "id": ll["id"], "name": ll["nombre"], "input": ll["argumentos"]}
                            for ll in m.get("llamadas", [])]
                rol, bloques = "assistant", bloques or [{"type": "text", "text": "..."}]
            else:  # herramienta
                bloques = [{"type": "tool_result", "tool_use_id": r["id"], "content": r["contenido"]}
                           for r in m["resultados"]]
                rol = "user"
            if salida and salida[-1]["role"] == rol:
                salida[-1]["content"].extend(bloques)  # la API no admite dos turnos seguidos del mismo rol
            else:
                salida.append({"role": rol, "content": bloques})
        return salida

    def completar(self, sistema: str | list[str], mensajes: list[dict[str, Any]],
                  herramientas: list[dict[str, Any]], forzar_texto: bool = False) -> RespuestaLLM:
        tools = [{"name": h["nombre"], "description": h["descripcion"], "input_schema": h["parametros"]}
                 for h in herramientas]
        # Caché: herramientas + instrucciones estables (catálogo) se reutilizan entre turnos y clientes.
        bloques = [{"type": "text", "text": parte} for parte in _partes(sistema)]
        bloques[0]["cache_control"] = {"type": "ephemeral"}
        extra: dict[str, Any] = {"tools": tools} if tools else {}
        if tools and forzar_texto:
            extra["tool_choice"] = {"type": "none"}
        max_tokens = self.max_tokens
        for _ in range(2):
            r = self.cliente.messages.create(model=self.modelo, max_tokens=max_tokens, system=bloques,
                                             messages=self.convertir(mensajes), **extra)
            if getattr(r, "stop_reason", "") != "max_tokens":
                break
            max_tokens *= 2  # respuesta cortada (ej. una lista larga): se reintenta con más espacio
        texto = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        llamadas = [LlamadaHerramienta(b.id, b.name, dict(b.input or {}))
                    for b in r.content if getattr(b, "type", "") == "tool_use"]
        uso = {}
        if getattr(r, "usage", None):
            uso = {"entrada": getattr(r.usage, "input_tokens", 0) or 0,
                   "salida": getattr(r.usage, "output_tokens", 0) or 0,
                   "cache_lectura": getattr(r.usage, "cache_read_input_tokens", 0) or 0}
        return RespuestaLLM(texto=texto, llamadas=llamadas, uso=uso)


# ============================================================ formato OpenAI (Azure, Gemini, DeepSeek...)

class LLMFormatoOpenAI:
    """Base para cualquier API con el formato de chat completions de OpenAI."""
    soporta_pdf = False

    def __init__(self, cliente: Any, modelo: str, max_tokens: int, modelo_respaldo: str = ""):
        self.cliente = cliente
        self.modelo = modelo
        self.max_tokens = max_tokens
        self.modelo_respaldo = modelo_respaldo if modelo_respaldo != modelo else ""

    def _crear(self, **kwargs: Any) -> Any:
        """Llama al modelo principal; si está saturado y hay respaldo, responde con el respaldo."""
        if self.modelo_respaldo and time.monotonic() < _pausados_hasta.get(self.modelo, 0):
            return self._llamar(self.modelo_respaldo, kwargs)
        try:
            return self._llamar(self.modelo, kwargs)
        except Exception as exc:
            if not self.modelo_respaldo or not _es_saturacion(exc):
                raise
            log.warning("Modelo %s no disponible (%s); se usa %s por %s min", self.modelo, exc,
                        self.modelo_respaldo, SEGUNDOS_PAUSA_PRINCIPAL // 60)
            _pausados_hasta[self.modelo] = time.monotonic() + SEGUNDOS_PAUSA_PRINCIPAL
            # Las firmas de razonamiento solo valen para el modelo que las generó.
            return self._llamar(self.modelo_respaldo, {**kwargs, "messages": _con_firmas_comodin(kwargs["messages"])})

    def _llamar(self, modelo: str, kwargs: dict[str, Any]) -> Any:
        try:
            return self.cliente.chat.completions.create(model=modelo, **kwargs)
        except Exception as exc:
            if not _es_error_de_firma(exc):
                raise
            # Gemini 3 rechazó una firma de razonamiento (faltante o de otro modelo): un reintento con la comodín.
            log.warning("%s rechazó la firma de razonamiento (%s); reintento con firma comodín", modelo, exc)
            return self.cliente.chat.completions.create(
                model=modelo, **{**kwargs, "messages": _con_firmas_comodin(kwargs["messages"])})

    @staticmethod
    def convertir(sistema: str | list[str], mensajes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        salida: list[dict[str, Any]] = [{"role": "system", "content": "\n\n".join(_partes(sistema))}]
        for m in mensajes:
            if m["rol"] == "usuario":
                partes = []
                for c in m["contenido"]:
                    if c["tipo"] == "texto" and c.get("texto"):
                        partes.append({"type": "text", "text": c["texto"]})
                    elif c["tipo"] == "imagen":
                        partes.append({"type": "image_url",
                                       "image_url": {"url": f"data:{c['media_type']};base64,{c['datos_b64']}"}})
                    elif c["tipo"] == "pdf":
                        partes.append({"type": "text", "text": "[El cliente envió un PDF que este modelo no puede leer; "
                                                               "pídele una foto o que escriba la lista.]"})
                salida.append({"role": "user", "content": partes or [{"type": "text", "text": "(mensaje vacío)"}]})
            elif m["rol"] == "asistente":
                msg: dict[str, Any] = {"role": "assistant",
                                       "content": m.get("texto") or (None if m.get("llamadas") else "...")}
                if m.get("llamadas"):
                    msg["tool_calls"] = []
                    for ll in m["llamadas"]:
                        tc: dict[str, Any] = {"id": ll["id"], "type": "function",
                                              "function": {"name": ll["nombre"],
                                                           "arguments": json.dumps(ll["argumentos"], ensure_ascii=False)}}
                        if ll.get("extra"):
                            tc["extra_content"] = ll["extra"]  # firma de Gemini 3, se devuelve tal cual
                        msg["tool_calls"].append(tc)
                salida.append(msg)
            else:
                for r in m["resultados"]:
                    salida.append({"role": "tool", "tool_call_id": r["id"], "content": r["contenido"]})
        return salida

    def completar(self, sistema: str | list[str], mensajes: list[dict[str, Any]],
                  herramientas: list[dict[str, Any]], forzar_texto: bool = False) -> RespuestaLLM:
        tools = [{"type": "function", "function": {"name": h["nombre"], "description": h["descripcion"],
                                                   "parameters": h["parametros"]}} for h in herramientas]
        extra: dict[str, Any] = {"tools": tools} if tools else {}
        if tools and forzar_texto:
            extra["tool_choice"] = "none"
        max_tokens = self.max_tokens
        for _ in range(2):
            r = self._crear(messages=self.convertir(sistema, mensajes), max_tokens=max_tokens, temperature=0.2,
                            **extra)
            if r.choices[0].finish_reason != "length":
                break
            max_tokens *= 2
        msg = r.choices[0].message
        llamadas = []
        for tc in msg.tool_calls or []:
            try:
                argumentos = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                argumentos = {}
            extra = getattr(tc, "extra_content", None)
            llamadas.append(LlamadaHerramienta(tc.id, tc.function.name, argumentos,
                                               extra if isinstance(extra, dict) and extra else None))
        uso = {}
        if getattr(r, "usage", None):
            uso = {"entrada": r.usage.prompt_tokens or 0, "salida": r.usage.completion_tokens or 0}
        return RespuestaLLM(texto=(msg.content or "").strip(), llamadas=llamadas, uso=uso)


class LLMAzureOpenAI(LLMFormatoOpenAI):
    def __init__(self, config: Config, cliente: Any = None):
        if cliente is None:
            from openai import AzureOpenAI

            cliente = AzureOpenAI(api_key=config.azure_openai_api_key, api_version=config.azure_openai_api_version,
                                  azure_endpoint=config.azure_openai_endpoint, max_retries=2, timeout=60)
        super().__init__(cliente, config.azure_openai_deployment, config.llm_max_tokens)


class LLMCompatibleOpenAI(LLMFormatoOpenAI):
    """Gemini (tiene capa gratuita), DeepSeek, Groq, OpenRouter, etc. vía su endpoint compatible con OpenAI."""

    def __init__(self, config: Config, cliente: Any = None):
        if cliente is None:
            from openai import OpenAI

            # Con respaldo, un solo reintento: es más rápido pasar al otro modelo que insistir con uno saturado.
            reintentos = 1 if config.llm_modelo_respaldo else 2
            cliente = OpenAI(api_key=config.llm_api_key, base_url=config.llm_base_url, max_retries=reintentos,
                             timeout=60)
        super().__init__(cliente, config.llm_modelo, config.llm_max_tokens, config.llm_modelo_respaldo)


# ============================================================ audio

class TranscriptorAzureOpenAI:
    def __init__(self, config: Config, cliente: Any = None):
        if cliente is None:
            from openai import AzureOpenAI

            cliente = AzureOpenAI(api_key=config.azure_openai_api_key, api_version=config.azure_openai_api_version,
                                  azure_endpoint=config.azure_openai_endpoint)
        self.cliente = cliente
        self.deployment = config.azure_openai_deployment_transcripcion

    def transcribir(self, audio: bytes, mime: str) -> str:
        extension = "ogg" if "ogg" in mime else (mime.split("/")[-1] or "ogg")
        r = self.cliente.audio.transcriptions.create(model=self.deployment, file=(f"audio.{extension}", audio, mime),
                                                     language="es")
        return (getattr(r, "text", "") or "").strip()


def crear_llm(config: Config) -> ProveedorLLM:
    if config.llm_proveedor == "azure_openai":
        return LLMAzureOpenAI(config)
    if config.llm_proveedor in PROVEEDORES_COMPATIBLES:
        return LLMCompatibleOpenAI(config)
    return LLMAnthropic(config)


def crear_transcriptor(config: Config):
    if config.transcripcion_proveedor == "azure_openai" and config.azure_openai_deployment_transcripcion:
        return TranscriptorAzureOpenAI(config)
    return None
