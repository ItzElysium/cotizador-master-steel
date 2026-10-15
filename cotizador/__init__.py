"""Cotizador IA para distribuidores de acero (MVP DataPrix)."""
from __future__ import annotations

from .config import Config, cargar_config

__version__ = "0.1.0"


def crear_servicio(config: Config | None = None):
    """Arma el servicio con los componentes reales según la configuración."""
    from .almacenamiento import (ArchivosBlob, ArchivosLocales, ColaAzure, ColaLista, RepositorioMemoria,
                                 RepositorioTablas)
    from .llm import crear_llm, crear_transcriptor
    from .servicio import Servicio
    from .whatsapp import ClienteWhatsApp

    config = config or cargar_config()
    if config.almacen == "azure":
        repo, archivos, cola = RepositorioTablas(config), ArchivosBlob(config), ColaAzure(config)
    else:
        repo, archivos, cola = RepositorioMemoria(), ArchivosLocales(), ColaLista()
    return Servicio(config=config, repo=repo, archivos=archivos, cola=cola, whatsapp=ClienteWhatsApp(config),
                    llm=crear_llm(config), transcriptor=crear_transcriptor(config))
