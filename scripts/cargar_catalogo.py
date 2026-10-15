"""Valida y sube la lista de precios (Excel o CSV) al cotizador desplegado.

    # 1) Validar localmente
    python scripts/cargar_catalogo.py data/plantilla_lista_precios.xlsx --validar

    # 2) Subir (la clave de función se obtiene en el portal o con `az functionapp keys list`)
    python scripts/cargar_catalogo.py lista.xlsx --url https://<app>.azurewebsites.net --clave <clave>

    # Solo actualizar precios (CSV con columnas sku,precio_usd): se conserva el resto de datos
    python scripts/cargar_catalogo.py precios_hoy.csv --url ... --clave ...

    # Reemplazar todo el catálogo (borra los SKU que no estén en el archivo)
    python scripts/cargar_catalogo.py lista.xlsx --url ... --clave ... --reemplazar
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cotizador.catalogo import ErrorCatalogo, excel_a_csv, leer_csv  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archivo")
    parser.add_argument("--url", help="URL base de la Function App")
    parser.add_argument("--clave", help="Clave de función (x-functions-key)")
    parser.add_argument("--reemplazar", action="store_true")
    parser.add_argument("--validar", action="store_true", help="Solo validar, no subir")
    args = parser.parse_args()

    ruta = Path(args.archivo)
    texto = excel_a_csv(ruta) if ruta.suffix.lower() in {".xlsx", ".xlsm"} else ruta.read_text(encoding="utf-8-sig")
    encabezado = texto.splitlines()[0].lower() if texto.strip() else ""
    parcial = "descripcion" not in encabezado  # ej. solo sku,precio_usd: lo valida el servidor contra su catálogo
    if args.validar or not parcial:
        try:
            productos, errores = leer_csv(texto)
        except ErrorCatalogo as exc:
            sys.exit(f"❌ {exc}")
        print(f"✔ {len(productos)} filas válidas")
        for e in errores:
            print(f"  ⚠ {e}")
        if args.validar:
            return
    if not (args.url and args.clave):
        sys.exit("Indica --url y --clave para subir, o usa --validar.")
    r = requests.post(f"{args.url.rstrip('/')}/api/admin/catalogo", params={"reemplazar": int(args.reemplazar)},
                      headers={"x-functions-key": args.clave, "Content-Type": "text/csv; charset=utf-8"},
                      data=texto.encode("utf-8"), timeout=60)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
