"""Catálogo, jerga y motor de precios."""
from datetime import date
from decimal import Decimal

import pytest

from conftest import RAIZ
from cotizador.catalogo import Catalogo, ErrorCatalogo, leer_archivo, leer_csv
from cotizador.modelos import TipoCambio
from cotizador.precios import MotorPrecios
from cotizador.texto import normalizar, tokens

TC = TipoCambio(Decimal("3.457"), "2026-10-09", "BCRP/SBS venta")


@pytest.fixture(scope="module")
def catalogo():
    productos, errores = leer_archivo(RAIZ / "data" / "catalogo_ejemplo.csv")
    assert not errores
    return Catalogo(productos)


@pytest.mark.parametrize("entrada,esperado", [
    ("Fierro de MEDIA", "fierro 1/2"),
    ("fierro de tres octavos", "fierro de 3/8"),
    ("varilla de ½ pulgada", "varilla de 1/2 pulgada"),
    ("plancha de 1.50mm", "plancha de 1.5 mm"),
    ("tubo 2x1", "tubo 2 x 1"),
    ("ángulo de 1 1/2", "angulo de 1-1/2"),
    ("media docena de tubos", "6 de tubos"),
])
def test_normalizar_jerga(entrada, esperado):
    assert normalizar(entrada) == esperado


def test_tokens_quitan_relleno():
    assert tokens("hola, quiero cotizar 100 fierros de media") == ["100", "fierros", "1/2"]


@pytest.mark.parametrize("consulta,sku", [
    ("fierro de media", "FC-0500"),
    ("fierro de 12", "FC-M12"),           # 12 mm no es 1/2"
    ("fierro de tres octavos", "FC-0375"),
    ("plancha negra de 1/16", "PF-150"),  # 1/16 es LAF
    ("plancha negra 1/8", "PC-030"),      # 1/8 es LAC
    ("alambre de amarre", "AL-16"),
    ("tubo rectangular 2x1", "TT-2010"),
    ("tubo cuadrado de 1 x 1", "TC-1000"),
    ("angulo 2 x 3/16", "AN-2316"),
    ("platina de 2 x 1/4", "PL-2014"),
    ("fc-0500", "FC-0500"),
])
def test_busqueda_con_jerga(catalogo, consulta, sku):
    assert catalogo.buscar(consulta)[0].producto.sku == sku


def test_csv_valida_errores():
    texto = ("sku,descripcion,peso_kg,precio_usd\n"
             "A-1,Prod A,1,2\nA-1,Duplicado,1,2\nB/2,Con barra,1,2\nC-3,Sin precio,1,0\nD-4,Coma decimal,\"1,5\",\"2,5\"\n")
    productos, errores = leer_csv(texto)
    assert [p.sku for p in productos] == ["A-1", "D-4"]
    assert productos[1].peso_kg == Decimal("1.5")
    assert len(errores) == 3


def test_csv_sin_columnas():
    with pytest.raises(ErrorCatalogo):
        leer_csv("codigo,nombre\nA,B\n")


def test_tonelada_se_convierte_a_varillas(config, catalogo):
    res = MotorPrecios(config, catalogo).calcular([{"sku": "FC-0500", "cantidad": 1, "unidad_pedida": "tonelada"}],
                                                  hoy=date(2026, 10, 14))
    linea = res.lineas[0]
    assert linea.cantidad == 112                    # 1000 / 8.95 = 111.7 -> 112
    assert linea.peso_total_kg == Decimal("1002.40")
    assert "112" in linea.nota


def test_alambre_por_kg(config, catalogo):
    res = MotorPrecios(config, catalogo).calcular([{"sku": "AL-16", "cantidad": 25, "unidad_pedida": "kg"}],
                                                  hoy=date(2026, 10, 14))
    assert res.lineas[0].cantidad == 25 and res.lineas[0].subtotal_usd == Decimal("31.25")


def test_totales_igv_y_soles(config, catalogo):
    res = MotorPrecios(config, catalogo).calcular([{"sku": "FC-0500", "cantidad": 10}], "publico", TC,
                                                  hoy=date(2026, 10, 14))
    assert res.subtotal_bruto_usd == Decimal("85.00")
    assert res.descuento_usd == 0
    assert res.igv_usd == Decimal("15.30")
    assert res.total_usd == Decimal("100.30")
    assert res.total_pen == Decimal("346.74")       # 100.30 x 3.457


def test_descuento_tipo_mas_volumen_con_tope(config, catalogo):
    config.descuento_maximo = Decimal("0.05")
    res = MotorPrecios(config, catalogo).calcular([{"sku": "FC-0500", "cantidad": 2, "unidad_pedida": "tonelada"}],
                                                  "ferreteria", hoy=date(2026, 10, 14))
    assert res.descuento_pct == Decimal("0.05")     # 5% ferretería + 2% volumen, tope 5%


def test_agotado_e_inexistente_son_errores(config, catalogo):
    res = MotorPrecios(config, catalogo).calcular([{"sku": "PG-090", "cantidad": 1}, {"sku": "NO-EXISTE", "cantidad": 1},
                                                   {"sku": "FC-0500", "cantidad": 1}], hoy=date(2026, 10, 14))
    assert not res.ok and len(res.errores) == 2 and len(res.lineas) == 1


def test_stock_consultar_y_precio_desactualizado(config, catalogo):
    res = MotorPrecios(config, catalogo).calcular([{"sku": "VH-W0615", "cantidad": 1}], hoy=date(2026, 10, 14))
    assert res.avisos_cliente and res.alertas_internas


def test_sku_repetido_se_agrupa_y_decimales_se_redondean(config, catalogo):
    res = MotorPrecios(config, catalogo).calcular([{"sku": "FC-0500", "cantidad": 10}, {"sku": "fc-0500", "cantidad": 2.4}],
                                                  hoy=date(2026, 10, 14))
    assert len(res.lineas) == 1 and res.lineas[0].cantidad == 13


def test_precios_que_incluyen_igv(config, catalogo):
    config.precios_incluyen_igv = True
    motor = MotorPrecios(config, catalogo)
    producto = catalogo.obtener("FC-0500")
    assert motor.precio_sin_igv(producto) == Decimal("7.2034")  # 8.50 / 1.18
    res = motor.calcular([{"sku": "FC-0500", "cantidad": 10}], hoy=date(2026, 10, 14))
    assert res.total_usd == Decimal("85.00")                    # el total respeta el precio de lista
    assert res.base_imponible_usd + res.igv_usd == res.total_usd and res.igv_usd == Decimal("12.97")


def test_precio_por_kg_no_pierde_centavos(config, catalogo):
    catalogo.obtener("AL-16").precio_usd = Decimal("1.234")
    try:
        res = MotorPrecios(config, catalogo).calcular([{"sku": "AL-16", "cantidad": 5, "unidad_pedida": "tonelada"}],
                                                      hoy=date(2026, 10, 14))
    finally:
        catalogo.obtener("AL-16").precio_usd = Decimal("1.25")
    assert res.lineas[0].subtotal_usd == Decimal("6170.00")    # no 6,150.00 por redondear el unitario
