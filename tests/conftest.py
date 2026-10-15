from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cotizador.almacenamiento import ArchivosLocales, ColaLista, RepositorioMemoria  # noqa: E402
from cotizador.config import cargar_config  # noqa: E402
from cotizador.llm import LlamadaHerramienta, RespuestaLLM  # noqa: E402
from cotizador.servicio import Servicio  # noqa: E402
from cotizador.tipo_cambio import ProveedorTipoCambio  # noqa: E402
from cotizador.whatsapp import ClienteConsola  # noqa: E402

ADMIN = "51911111111"
CLIENTE = "51987654321"
AHORA = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)

RESPUESTA_BCRP = {
    "config": {"title": "Tipo de cambio", "series": [{"name": "TC Sistema bancario SBS (S/ por US$) - Venta"}]},
    "periods": [{"name": "08.Oct.26", "values": ["3.453"]}, {"name": "09.Oct.26", "values": ["3.457"]},
                {"name": "10.Oct.26", "values": ["n.d."]}],
}


class FakeHttp:
    def __init__(self, datos=RESPUESTA_BCRP, falla: bool = False):
        self.datos, self.falla, self.llamadas = datos, falla, 0

    def __call__(self, url, timeout=None, headers=None):
        self.llamadas += 1
        if self.falla:
            raise ConnectionError("sin red")
        datos = self.datos

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return datos
        return R()


class FakeLLM:
    """Modelo falso: cada paso es una RespuestaLLM o una función que la construye."""
    soporta_pdf = True

    def __init__(self, pasos: list[RespuestaLLM | Callable[..., RespuestaLLM]]):
        self.pasos = list(pasos)
        self.llamadas: list[dict[str, Any]] = []

    def completar(self, sistema, mensajes, herramientas, forzar_texto=False):
        sistema = "\n\n".join(sistema) if isinstance(sistema, list) else sistema
        self.llamadas.append({"sistema": sistema, "mensajes": [dict(m) for m in mensajes],
                              "herramientas": [h["nombre"] for h in herramientas], "forzar_texto": forzar_texto})
        if not self.pasos:
            return RespuestaLLM(texto="(sin guion)")
        paso = self.pasos.pop(0)
        if isinstance(paso, Exception):
            raise paso
        return paso(sistema, mensajes, herramientas) if callable(paso) else paso


def llamada(nombre: str, **argumentos) -> RespuestaLLM:
    return RespuestaLLM(texto="", llamadas=[LlamadaHerramienta(id=f"t_{nombre}", nombre=nombre, argumentos=argumentos)])


def texto(t: str) -> RespuestaLLM:
    return RespuestaLLM(texto=t)


@pytest.fixture
def entorno(monkeypatch):
    monkeypatch.setenv("ADMIN_NUMEROS", ADMIN)
    monkeypatch.setenv("MODO_ENVIO", "aprobacion")
    monkeypatch.setenv("WA_APP_SECRET", "secreto")
    monkeypatch.delenv("STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.setenv("ALMACEN", "memoria")
    return monkeypatch


@pytest.fixture
def config(entorno):
    return cargar_config()


class Reloj:
    """Reloj controlable para probar vencimientos (ventana de 24 h, pausas)."""

    def __init__(self, ahora: datetime):
        self.ahora = ahora

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, **kwargs) -> None:
        from datetime import timedelta
        self.ahora += timedelta(**kwargs)


@pytest.fixture
def armar(config, tmp_path):
    """Fábrica: armar(pasos_llm, **cambios_config) -> Servicio listo para probar."""
    def _armar(pasos=None, **cambios):
        for k, v in cambios.items():
            setattr(config, k, v)
        repo = RepositorioMemoria()
        tc = ProveedorTipoCambio(config, repo, http_get=FakeHttp())
        return Servicio(config=config, repo=repo, archivos=ArchivosLocales(tmp_path / "archivos"), cola=ColaLista(),
                        whatsapp=ClienteConsola(tmp_path / "enviados", imprimir=False), llm=FakeLLM(pasos or []),
                        tc=tc, reloj=Reloj(AHORA))
    return _armar


def webhook_texto(wa_id: str, cuerpo: str, msg_id: str, ts: int = 1760454000, nombre: str = "Juan Pérez") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"},
        "contacts": [{"wa_id": wa_id, "profile": {"name": nombre}}],
        "messages": [{"from": wa_id, "id": msg_id, "timestamp": str(ts), "type": "text", "text": {"body": cuerpo}}],
    }}]}]}


def webhook_boton(wa_id: str, boton_id: str, msg_id: str, ts: int = 1760454100) -> dict:
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": "Admin"}}],
        "messages": [{"from": wa_id, "id": msg_id, "timestamp": str(ts), "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": boton_id, "title": "Enviar"}}}],
    }}]}]}


def procesar_cola(servicio: Servicio) -> list[str]:
    resultados = []
    while servicio.cola.mensajes:
        mensaje, _ = servicio.cola.mensajes.pop(0)
        resultados.append(servicio.procesar(mensaje["wa_id"], mensaje["msg_id"]))
    return resultados
