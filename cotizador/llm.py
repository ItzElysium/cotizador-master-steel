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
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict[str, Any]


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

    def __init__(self, cliente: Any, modelo: str, max_tokens: int):
        self.cliente = cliente
        self.modelo = modelo
        self.max_tokens = max_tokens

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
                    msg["tool_calls"] = [{"id": ll["id"], "type": "function",
                                          "function": {"name": ll["nombre"],
                                                       "arguments": json.dumps(ll["argumentos"], ensure_ascii=False)}}
                                         for ll in m["llamadas"]]
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
            r = self.cliente.chat.completions.create(model=self.modelo, messages=self.convertir(sistema, mensajes),
                                                     max_tokens=max_tokens, temperature=0.2, **extra)
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
            llamadas.append(LlamadaHerramienta(tc.id, tc.function.name, argumentos))
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

            cliente = OpenAI(api_key=config.llm_api_key, base_url=config.llm_base_url, max_retries=2, timeout=60)
        super().__init__(cliente, config.llm_modelo, config.llm_max_tokens)


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
