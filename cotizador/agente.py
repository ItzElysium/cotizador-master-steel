"""Bucle del agente: el modelo decide, las herramientas ejecutan, el historial se guarda liviano."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .llm import ProveedorLLM, RespuestaLLM

log = logging.getLogger(__name__)

MAX_ITERACIONES = 6
MAX_RESULTADO_EN_HISTORIAL = 2500


class Agente:
    def __init__(self, llm: ProveedorLLM, herramientas: list[dict[str, Any]], max_iteraciones: int = MAX_ITERACIONES):
        self.llm = llm
        self.herramientas = herramientas
        self.max_iteraciones = max_iteraciones

    def responder(self, sistema: str | list[str], historial: list[dict[str, Any]], contenido_usuario: list[dict[str, Any]],
                  ejecutar: Callable[[str, dict[str, Any]], str]) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
        """Devuelve (texto para el cliente, historial nuevo, uso de tokens)."""
        mensajes = list(historial) + [{"rol": "usuario", "contenido": contenido_usuario}]
        uso_total: dict[str, int] = {}
        respuesta = RespuestaLLM(texto="")
        for _ in range(self.max_iteraciones):
            respuesta = self.llm.completar(sistema, mensajes, self.herramientas)
            for k, v in respuesta.uso.items():
                uso_total[k] = uso_total.get(k, 0) + v
            mensajes.append({
                "rol": "asistente", "texto": respuesta.texto,
                "llamadas": [{"id": ll.id, "nombre": ll.nombre, "argumentos": ll.argumentos} for ll in respuesta.llamadas],
            })
            if not respuesta.llamadas:
                break
            resultados = []
            for ll in respuesta.llamadas:
                try:
                    contenido = ejecutar(ll.nombre, ll.argumentos)
                except Exception as exc:  # una herramienta rota no debe tumbar la conversación
                    log.exception("Error en herramienta %s", ll.nombre)
                    contenido = json.dumps({"ok": False, "error": f"Error interno: {exc}"}, ensure_ascii=False)
                resultados.append({"id": ll.id, "nombre": ll.nombre, "contenido": contenido})
            mensajes.append({"rol": "herramienta", "resultados": resultados})
        else:
            # Se agotaron las iteraciones: se pide un cierre en texto. Se envían las mismas herramientas
            # (la API las exige si el historial tiene llamadas) pero prohibiendo usarlas.
            respuesta = self.llm.completar(sistema, mensajes, self.herramientas, forzar_texto=True)
            mensajes.append({"rol": "asistente", "texto": respuesta.texto, "llamadas": []})

        texto = respuesta.texto or ""
        return texto, mensajes, uso_total


# ------------------------------------------------------------------ historial

def aligerar(mensajes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quita imágenes/PDF (pesados) y recorta resultados largos antes de guardar."""
    salida = []
    for m in mensajes:
        if m["rol"] == "usuario":
            contenido = []
            for c in m["contenido"]:
                if c["tipo"] == "imagen":
                    contenido.append({"tipo": "texto", "texto": "[El cliente envió una imagen]"})
                elif c["tipo"] == "pdf":
                    contenido.append({"tipo": "texto", "texto": "[El cliente envió un PDF]"})
                else:
                    contenido.append(c)
            salida.append({"rol": "usuario", "contenido": contenido})
        elif m["rol"] == "herramienta":
            salida.append({"rol": "herramienta", "resultados": [
                {**r, "contenido": r["contenido"][:MAX_RESULTADO_EN_HISTORIAL]} for r in m["resultados"]]})
        else:
            salida.append(m)
    return salida


def recortar(mensajes: list[dict[str, Any]], maximo: int) -> list[dict[str, Any]]:
    """Mantiene los últimos `maximo` mensajes empezando siempre en un turno del usuario
    (nunca deja una respuesta de herramienta sin su llamada)."""
    if len(mensajes) <= maximo:
        return mensajes
    recorte = mensajes[-maximo:]
    for i, m in enumerate(recorte):
        if m["rol"] == "usuario":
            return recorte[i:]
    return []


def nota_de_sistema(texto: str) -> list[dict[str, Any]]:
    """Par usuario/asistente para dejar constancia en el historial de algo que pasó fuera del chat."""
    return [{"rol": "usuario", "contenido": [{"tipo": "texto", "texto": f"[Nota interna del sistema: {texto}]"}]},
            {"rol": "asistente", "texto": "Entendido.", "llamadas": []}]
