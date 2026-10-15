"""Tipo de cambio US$ -> S/.

Fuente por defecto: API pública del BCRP, serie PD04640PD
("TC Sistema bancario SBS (S/ por US$) - Venta"), la misma referencia que usa
SUNAT. El administrador puede fijar un TC manual por WhatsApp (/tc 3.75) y
ese valor manda durante el día.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from .config import Config
from .modelos import TipoCambio

log = logging.getLogger(__name__)

SERIE_SBS_VENTA = "PD04640PD"
URL_BCRP = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api/{serie}/json/{desde}/{hasta}"
_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
          "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


def _fecha_bcrp(nombre: str) -> str:
    """'09.Oct.26' -> '2026-10-09'. Si no se puede interpretar, devuelve el texto tal cual."""
    try:
        dia, mes, anio = nombre.split(".")
        return date(2000 + int(anio), _MESES[mes.lower()[:3]], int(dia)).isoformat()
    except (ValueError, KeyError):
        return nombre


def parsear_bcrp(datos: dict[str, Any]) -> TipoCambio | None:
    """Toma el último valor publicado (ignora los 'n.d.' de días sin dato)."""
    for periodo in reversed(datos.get("periods", [])):
        valores = periodo.get("values") or []
        if not valores:
            continue
        try:
            valor = Decimal(str(valores[0]))
        except InvalidOperation:
            continue
        if valor > 0:
            return TipoCambio(valor=valor, fecha=_fecha_bcrp(periodo.get("name", "")), fuente="BCRP/SBS venta")
    return None


class ProveedorTipoCambio:
    def __init__(self, config: Config, repo, http_get: Callable[..., Any] = requests.get):
        self.config = config
        self.repo = repo
        self.http_get = http_get

    def _hoy(self) -> str:
        return datetime.now(ZoneInfo(self.config.zona_horaria)).date().isoformat()

    def _leer_guardado(self) -> tuple[TipoCambio | None, str]:
        dato = self.repo.obtener_config("tipo_cambio")
        if not dato:
            return None, ""
        try:
            tc = TipoCambio(Decimal(dato["valor"]), dato.get("fecha", ""), dato.get("fuente", ""))
        except (InvalidOperation, KeyError):
            return None, ""
        return tc, dato.get("consultado", "")

    def _guardar(self, tc: TipoCambio) -> None:
        self.repo.guardar_config("tipo_cambio", {
            "valor": str(tc.valor), "fecha": tc.fecha, "fuente": tc.fuente, "consultado": self._hoy(),
        })

    def obtener(self) -> TipoCambio | None:
        hoy = self._hoy()
        guardado, consultado = self._leer_guardado()
        if guardado and guardado.fuente == "manual" and consultado == hoy:
            return guardado
        if self.config.tc_fuente == "manual":
            return TipoCambio(self.config.tc_manual, hoy, "manual") if self.config.tc_manual > 0 else guardado
        if guardado and consultado == hoy:
            return guardado
        nuevo = self.actualizar_desde_bcrp()
        if nuevo:
            return nuevo
        if guardado:
            return guardado
        if self.config.tc_manual > 0:
            return TipoCambio(self.config.tc_manual, hoy, "manual (respaldo)")
        return None

    def actualizar_desde_bcrp(self) -> TipoCambio | None:
        hasta = date.fromisoformat(self._hoy())
        desde = hasta - timedelta(days=15)
        url = URL_BCRP.format(serie=SERIE_SBS_VENTA, desde=desde.isoformat(), hasta=hasta.isoformat())
        try:
            respuesta = self.http_get(url, timeout=8, headers={"User-Agent": "cotizador-acero/1.0"})
            respuesta.raise_for_status()
            tc = parsear_bcrp(respuesta.json())
        except Exception as exc:  # red caída, formato inesperado, etc.
            log.warning("No se pudo obtener el TC del BCRP: %s", exc)
            return None
        if tc:
            self._guardar(tc)
        return tc

    def fijar_manual(self, valor: Decimal) -> TipoCambio:
        tc = TipoCambio(valor=valor, fecha=self._hoy(), fuente="manual")
        self._guardar(tc)
        return tc
