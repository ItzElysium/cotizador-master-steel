"""Chat de prueba en la terminal: el agente real, sin WhatsApp ni Azure.

Uso:
    export ANTHROPIC_API_KEY=sk-ant-...        # o LLM_PROVEEDOR=azure_openai + AZURE_OPENAI_*
    python scripts/simular_chat.py

Comandos dentro del chat:
    /foto ruta/a/lista.jpg [texto]   enviar una foto (ej. lista de materiales escrita a mano)
    /admin <texto o comando>         hablar como el vendedor (ej. /admin /pendientes)
    /aprobar COT-000001              simular el botón "Enviar" del vendedor
    /salir

Los PDFs quedan en salida/enviados/.
"""
from __future__ import annotations

import mimetypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cotizador.almacenamiento import ArchivosLocales, ColaLista, RepositorioMemoria  # noqa: E402
from cotizador.config import cargar_config  # noqa: E402
from cotizador.llm import crear_llm  # noqa: E402
from cotizador.servicio import Servicio  # noqa: E402
from cotizador.whatsapp import ClienteConsola  # noqa: E402

CLIENTE = "51900000001"
ADMIN = "51900000099"


def _payload(wa_id: str, mensaje: dict, nombre: str) -> dict:
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": nombre}}], "messages": [mensaje]}}]}]}


def main() -> None:
    os.environ.setdefault("ADMIN_NUMEROS", ADMIN)
    os.environ["ALMACEN"] = "memoria"
    config = cargar_config()
    if config.llm_proveedor == "anthropic" and not config.anthropic_api_key:
        sys.exit("Falta ANTHROPIC_API_KEY (o usa LLM_PROVEEDOR=azure_openai con AZURE_OPENAI_*).")
    whatsapp = ClienteConsola("salida/enviados")
    servicio = Servicio(config=config, repo=RepositorioMemoria(), archivos=ArchivosLocales("salida"),
                        cola=ColaLista(), whatsapp=whatsapp, llm=crear_llm(config))
    print(f"🧪 Simulador · {config.empresa.nombre} · modo {config.modo_envio} · {len(servicio.catalogo())} productos")
    print("Escribe como si fueras un cliente por WhatsApp. /salir para terminar.\n")

    contador = 0
    while True:
        try:
            entrada = input("👤 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not entrada:
            continue
        if entrada == "/salir":
            break
        contador += 1
        msg_id, ts = f"sim{contador}", str(int(time.time()))
        wa_id, nombre = CLIENTE, "Cliente de prueba"

        if entrada.startswith("/aprobar "):
            wa_id, nombre = ADMIN, "Vendedor"
            mensaje = {"from": ADMIN, "id": msg_id, "timestamp": ts, "type": "interactive",
                       "interactive": {"button_reply": {"id": f"aprobar:{entrada.split()[1]}", "title": "Enviar"}}}
        elif entrada.startswith("/admin "):
            wa_id, nombre = ADMIN, "Vendedor"
            mensaje = {"from": ADMIN, "id": msg_id, "timestamp": ts, "type": "text", "text": {"body": entrada[7:]}}
        elif entrada.startswith("/foto "):
            partes = entrada.split(maxsplit=2)
            ruta = Path(partes[1])
            if not ruta.is_file():
                print(f"No existe {ruta}")
                continue
            media_id = f"media{contador}"
            mime = mimetypes.guess_type(ruta.name)[0] or "image/jpeg"
            whatsapp.medias[media_id] = (ruta.read_bytes(), mime)
            mensaje = {"from": CLIENTE, "id": msg_id, "timestamp": ts, "type": "image",
                       "image": {"id": media_id, "mime_type": mime, "caption": partes[2] if len(partes) > 2 else ""}}
        else:
            mensaje = {"from": CLIENTE, "id": msg_id, "timestamp": ts, "type": "text", "text": {"body": entrada}}

        servicio.recibir_webhook(_payload(wa_id, mensaje, nombre))
        while servicio.cola.mensajes:
            item, _ = servicio.cola.mensajes.pop(0)
            servicio.procesar(item["wa_id"], item["msg_id"])


if __name__ == "__main__":
    main()
