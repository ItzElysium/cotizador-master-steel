"""Normalización de texto y jerga peruana del rubro acero.

El objetivo es que "fierro de media", "fierro 1/2", "varilla de ½ pulgada"
y "FIERRO DE MEDIA PULGADA" terminen en los mismos tokens comparables.
"""
from __future__ import annotations

import re
import unicodedata

# Frases habladas -> notación numérica. El orden importa (frases largas primero).
_JERGA: list[tuple[str, str]] = [
    (r"\bmedia docena\b", " 6 "),
    (r"\buna? y media pulgadas?\b", " 1-1/2 "),
    (r"\buno y medio\b", " 1-1/2 "),
    (r"\bmedia pulgada\b", " 1/2 "),
    (r"\bde media\b", " 1/2 "),
    (r"\bmedia\b", " 1/2 "),
    (r"\btres octavos?\b", " 3/8 "),
    (r"\bcinco octavos?\b", " 5/8 "),
    (r"\bun octavo\b", " 1/8 "),
    (r"\btres cuartos?\b", " 3/4 "),
    (r"\bun cuarto\b", " 1/4 "),
    (r"\btres dieciseisavos?\b", " 3/16 "),
    (r"\bun dieciseisavo\b", " 1/16 "),
    (r"\buna pulgada\b", " 1 pulg "),
    (r"\bde una\b", " 1 pulg "),
    (r"\bdos pulgadas\b", " 2 pulg "),
]

_CARACTERES = {
    "½": " 1/2 ", "¼": " 1/4 ", "¾": " 3/4 ", "⅛": " 1/8 ", "⅜": " 3/8 ", "⅝": " 5/8 ",
    "”": '"', "“": '"', "″": '"', "''": '"',
}

STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "en", "x", "por", "para", "un", "una", "unos", "unas",
    "y", "con", "a", "al", "me", "quiero", "necesito", "cotizar", "cotizacion", "precio", "precios",
    "cuanto", "esta", "estan", "cuesta", "favor", "porfa", "hola", "buenas", "buenos", "dias",
    "tardes", "noches", "que", "tiene", "tienen", "hay", "sr", "senor", "joven", "amigo", "pulgada",
    "pulgadas", "pulg", "metros", "metro", "m", "mm", "milimetros", "unidades", "unidad", "piezas",
    "pieza", "und", "unid",
}

_TOKEN = re.compile(r"\d+-\d+/\d+|\d+(?:[./]\d+)?|[a-zñ]+")


def quitar_tildes(texto: str) -> str:
    """Quita tildes pero conserva la ñ."""
    salida = []
    for c in texto:
        if c in "ñÑ":
            salida.append(c)
            continue
        salida.append("".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x)))
    return "".join(salida)


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, jerga convertida a fracciones, números unificados."""
    t = texto or ""
    for origen, destino in _CARACTERES.items():
        t = t.replace(origen, destino)
    t = t.lower()
    t = quitar_tildes(t)
    t = t.replace(",", ".") if re.search(r"\d,\d", t) else t
    for patron, reemplazo in _JERGA:
        t = re.sub(patron, reemplazo, t)
    # "1 1/2" -> "1-1/2" (una y media)
    t = re.sub(r"\b(\d+)\s+(\d+/\d+)\b", r"\1-\2", t)
    # "1/2\"" o "1/2 pulg" -> "1/2"
    t = t.replace('"', " ")
    # "1.20x2.40" -> "1.20 x 2.40"
    t = re.sub(r"(\d)\s*x\s*(\d)", r"\1 x \2", t)
    # "12mm" -> "12 mm"
    t = re.sub(r"(\d)(mm|m|kg)\b", r"\1 \2", t)
    # "1.50" -> "1.5", "3.0" -> "3"
    t = re.sub(r"\b(\d+)\.(\d*?)0+\b", lambda m: m.group(1) + ("." + m.group(2) if m.group(2) else ""), t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(texto: str, quitar_stopwords: bool = True) -> list[str]:
    toks = _TOKEN.findall(normalizar(texto))
    if quitar_stopwords:
        toks = [t for t in toks if t not in STOPWORDS]
    return toks


def es_numerico(token: str) -> bool:
    return bool(token) and token[0].isdigit()
