"""Genera un PDF de cotización de ejemplo (no requiere claves). Útil para mostrar al cliente.

    python scripts/generar_pdf_demo.py  ->  salida/Cotizacion_COT-000123.pdf
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cotizador.catalogo import Catalogo, leer_archivo  # noqa: E402
from cotizador.config import cargar_config  # noqa: E402
from cotizador.modelos import TipoCambio  # noqa: E402
from cotizador.pdf import generar_pdf, nombre_archivo  # noqa: E402
from cotizador.precios import MotorPrecios  # noqa: E402

PEDIDO = [
    {"sku": "FC-0500", "cantidad": 1, "unidad_pedida": "tonelada"},   # "una tonelada de fierro de media"
    {"sku": "FC-0375", "cantidad": 100},                               # "un ciento de tres octavos"
    {"sku": "AL-16", "cantidad": 25, "unidad_pedida": "kg"},           # "25 kilos de alambre de amarre"
    {"sku": "PF-150", "cantidad": 10},                                 # "10 planchas negras de 1/16"
    {"sku": "TC-1000", "cantidad": 20},                                # "20 tubos cuadrados de 1x1"
    {"sku": "FC-1000", "cantidad": 5},                                 # stock por confirmar
]


def main() -> None:
    config = cargar_config()
    productos, _ = leer_archivo(config.catalogo_csv_inicial)
    motor = MotorPrecios(config, Catalogo(productos))
    resultado = motor.calcular(PEDIDO, "maestro", TipoCambio(Decimal("3.457"), "2026-10-09", "BCRP/SBS venta"))
    cot = motor.crear_cotizacion(resultado, "COT-000123", "51987654321", "Constructora Los Andes S.A.C.",
                                 "20601234567", "Entrega en obra: Los Olivos, Lima")
    destino = Path("salida") / nombre_archivo(cot)
    destino.parent.mkdir(exist_ok=True)
    destino.write_bytes(generar_pdf(cot, config))
    print(f"PDF generado: {destino}  ·  Total US$ {cot.total_usd}")


if __name__ == "__main__":
    main()
