"""Motor de precios determinístico.

Regla de oro del MVP: la IA interpreta, el código calcula. Ningún precio,
peso, descuento o tipo de cambio sale del modelo de lenguaje.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .catalogo import Catalogo
from .config import Config
from .modelos import Cotizacion, LineaCotizacion, TipoCambio, a_iso

CENT = Decimal("0.01")
MAX_LINEAS = 40
MAX_CANTIDAD = Decimal("1000000")
UNIDADES_PEDIDO = {"unidad", "kg", "tonelada"}
TIPOS_CLIENTE = {"publico", "maestro", "ferreteria", "empresa"}


def q2(valor: Decimal) -> Decimal:
    return valor.quantize(CENT, rounding=ROUND_HALF_UP)


def q4(valor: Decimal) -> Decimal:
    return valor.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def fmt_precio(valor: Decimal) -> str:
    """Precio unitario: 2 decimales salvo que tenga más precisión (ej. 1.2345 US$/kg)."""
    return fmt(valor, 2) if valor == q2(valor) else fmt(valor.normalize(), 4)


def fmt(valor: Decimal | None, decimales: int = 2) -> str:
    if valor is None:
        return "-"
    return f"{valor:,.{decimales}f}"


def fmt_cantidad(valor: Decimal) -> str:
    return f"{valor:,.0f}" if valor == valor.to_integral_value() else f"{valor:,.2f}"


@dataclass
class ResultadoCalculo:
    lineas: list[LineaCotizacion]
    errores: list[str]
    avisos_cliente: list[str]
    alertas_internas: list[str]
    tipo_cliente: str
    subtotal_bruto_usd: Decimal = Decimal("0")
    descuento_pct: Decimal = Decimal("0")
    descuento_usd: Decimal = Decimal("0")
    base_imponible_usd: Decimal = Decimal("0")
    igv_usd: Decimal = Decimal("0")
    total_usd: Decimal = Decimal("0")
    peso_total_kg: Decimal = Decimal("0")
    tipo_cambio: TipoCambio | None = None
    total_pen: Decimal | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.lineas) and not self.errores

    def para_agente(self) -> dict[str, Any]:
        """Resumen compacto que recibe el agente (ya formateado, sin cálculos pendientes)."""
        datos: dict[str, Any] = {
            "lineas": [
                {
                    "sku": ln.sku,
                    "descripcion": ln.descripcion,
                    "cantidad": fmt_cantidad(ln.cantidad),
                    "unidad": ln.unidad,
                    "precio_unitario_usd_sin_igv": fmt_precio(ln.precio_unitario_usd),
                    "subtotal_usd_sin_igv": fmt(ln.subtotal_usd),
                    "peso_kg": fmt(ln.peso_total_kg),
                    "stock": ln.stock,
                    **({"nota": ln.nota} if ln.nota else {}),
                }
                for ln in self.lineas
            ],
            "subtotal_usd_sin_igv": fmt(self.subtotal_bruto_usd),
            "descuento": f"{fmt(self.descuento_pct * 100, 1)}% (US$ {fmt(self.descuento_usd)})" if self.descuento_usd else "0",
            "igv_usd": fmt(self.igv_usd),
            "total_usd_con_igv": fmt(self.total_usd),
            "peso_total_kg": fmt(self.peso_total_kg),
        }
        if self.total_pen is not None and self.tipo_cambio:
            datos["total_soles_referencial"] = fmt(self.total_pen)
            datos["tipo_cambio"] = f"{self.tipo_cambio.valor} ({self.tipo_cambio.fuente}, {self.tipo_cambio.fecha})"
        if self.errores:
            datos["errores"] = self.errores
        if self.avisos_cliente:
            datos["avisos"] = self.avisos_cliente
        return datos


class MotorPrecios:
    def __init__(self, config: Config, catalogo: Catalogo):
        self.config = config
        self.catalogo = catalogo

    # ------------------------------------------------------------ cálculo
    def calcular(self, items: list[dict[str, Any]], tipo_cliente: str = "publico",
                 tipo_cambio: TipoCambio | None = None, hoy: date | None = None) -> ResultadoCalculo:
        hoy = hoy or date.today()
        tipo_cliente = (tipo_cliente or "publico").strip().lower()
        if tipo_cliente not in TIPOS_CLIENTE:
            tipo_cliente = "publico"
        res = ResultadoCalculo(lineas=[], errores=[], avisos_cliente=[], alertas_internas=[], tipo_cliente=tipo_cliente)

        if not items:
            res.errores.append("No hay productos en el pedido.")
            return res
        if len(items) > MAX_LINEAS:
            res.errores.append(f"Máximo {MAX_LINEAS} productos por cotización.")
            return res

        # Agrupa SKUs repetidos (mismo producto pedido dos veces)
        agrupados: dict[tuple[str, str], Decimal] = {}
        for item in items:
            sku = str(item.get("sku", "")).strip().upper()
            unidad_pedida = str(item.get("unidad_pedida") or "unidad").strip().lower()
            if unidad_pedida in {"toneladas", "ton", "tn", "t"}:
                unidad_pedida = "tonelada"
            if unidad_pedida in {"kilos", "kilo", "kgs"}:
                unidad_pedida = "kg"
            if unidad_pedida not in UNIDADES_PEDIDO:
                unidad_pedida = "unidad"
            try:
                cantidad = Decimal(str(item.get("cantidad", "0")).replace(",", "."))
            except InvalidOperation:
                res.errores.append(f"Cantidad inválida para {sku}.")
                continue
            if cantidad <= 0 or cantidad > MAX_CANTIDAD:
                res.errores.append(f"Cantidad fuera de rango para {sku}.")
                continue
            clave = (sku, unidad_pedida)
            agrupados[clave] = agrupados.get(clave, Decimal("0")) + cantidad

        bruto_con_igv = Decimal("0")
        for (sku, unidad_pedida), cantidad in agrupados.items():
            producto = self.catalogo.obtener(sku)
            if producto is None:
                res.errores.append(f"El código {sku} no existe en el catálogo.")
                continue
            if producto.stock == "agotado":
                res.errores.append(f"{producto.descripcion} ({sku}) está agotado; ofrece una alternativa.")
                continue

            nota = ""
            if producto.se_vende_por_kg:
                kilos = cantidad * 1000 if unidad_pedida == "tonelada" else cantidad
                cantidad_venta = q2(kilos)
                if unidad_pedida == "tonelada":
                    nota = f"{fmt_cantidad(cantidad)} t = {fmt_cantidad(cantidad_venta)} kg"
            elif unidad_pedida in {"kg", "tonelada"}:
                kilos = cantidad * 1000 if unidad_pedida == "tonelada" else cantidad
                unidades = Decimal(math.ceil(kilos / producto.peso_kg))
                cantidad_venta = unidades
                etiqueta = "t" if unidad_pedida == "tonelada" else "kg"
                nota = (f"Pedido {fmt_cantidad(cantidad)} {etiqueta}: {fmt_cantidad(unidades)} x {producto.unidad} "
                        f"= {fmt(q2(unidades * producto.peso_kg))} kg teóricos")
            else:
                cantidad_venta = Decimal(math.ceil(cantidad))
                if cantidad_venta != cantidad:
                    nota = f"Cantidad redondeada de {cantidad} a {fmt_cantidad(cantidad_venta)}"

            # El unitario se guarda con 4 decimales y solo se redondea el subtotal (evita errores en
            # pedidos grandes de productos con precio por kg).
            precio_lista = producto.precio_unitario_usd
            if self.config.precios_incluyen_igv:
                subtotal_con_igv = q2(precio_lista * cantidad_venta)
                bruto_con_igv += subtotal_con_igv
                subtotal = q2(subtotal_con_igv / (1 + self.config.igv_tasa))
            else:
                subtotal = q2(precio_lista * cantidad_venta)
            precio_unit = self.precio_sin_igv(producto)
            peso_unit = producto.peso_kg
            linea = LineaCotizacion(
                sku=producto.sku,
                descripcion=producto.descripcion,
                unidad=producto.unidad,
                cantidad=cantidad_venta,
                peso_unitario_kg=peso_unit,
                peso_total_kg=q2(peso_unit * cantidad_venta),
                precio_unitario_usd=precio_unit,
                subtotal_usd=subtotal,
                stock=producto.stock,
                nota=nota,
            )
            res.lineas.append(linea)

            if producto.stock == "consultar":
                res.avisos_cliente.append(f"Stock de {producto.descripcion} por confirmar.")
            if self._precio_desactualizado(producto.actualizado, hoy):
                res.alertas_internas.append(
                    f"Precio de {producto.sku} sin actualizar desde {producto.actualizado or 'fecha desconocida'}.")

        if not res.lineas:
            return res

        res.peso_total_kg = sum((ln.peso_total_kg for ln in res.lineas), Decimal("0"))
        res.descuento_pct = self._descuento(tipo_cliente, res.peso_total_kg)
        if self.config.precios_incluyen_igv:
            # Lista con IGV: el total respeta exactamente el precio de lista y se desagrega el IGV.
            factor = 1 + self.config.igv_tasa
            res.total_usd = bruto_con_igv - q2(bruto_con_igv * res.descuento_pct)
            res.base_imponible_usd = q2(res.total_usd / factor)
            res.igv_usd = res.total_usd - res.base_imponible_usd
            res.subtotal_bruto_usd = q2(bruto_con_igv / factor)
            res.descuento_usd = res.subtotal_bruto_usd - res.base_imponible_usd
        else:
            res.subtotal_bruto_usd = sum((ln.subtotal_usd for ln in res.lineas), Decimal("0"))
            res.descuento_usd = q2(res.subtotal_bruto_usd * res.descuento_pct)
            res.base_imponible_usd = res.subtotal_bruto_usd - res.descuento_usd
            res.igv_usd = q2(res.base_imponible_usd * self.config.igv_tasa)
            res.total_usd = res.base_imponible_usd + res.igv_usd
        if tipo_cambio and self.config.mostrar_soles and tipo_cambio.valor > 0:
            res.tipo_cambio = TipoCambio(tipo_cambio.valor + self.config.tc_spread, tipo_cambio.fecha, tipo_cambio.fuente)
            res.total_pen = q2(res.total_usd * res.tipo_cambio.valor)
        return res

    def precio_sin_igv(self, producto) -> Decimal:
        """Precio de una unidad de venta, sin IGV, en US$ (4 decimales)."""
        precio = producto.precio_unitario_usd
        if self.config.precios_incluyen_igv:
            precio = precio / (1 + self.config.igv_tasa)
        return q4(precio)

    def precio_con_igv(self, producto) -> Decimal:
        precio = producto.precio_unitario_usd
        if self.config.precios_incluyen_igv:
            return q2(precio)
        return q2(precio * (1 + self.config.igv_tasa))

    def _descuento(self, tipo_cliente: str, peso_kg: Decimal) -> Decimal:
        por_tipo = self.config.descuentos_tipo_cliente.get(tipo_cliente, Decimal("0"))
        por_volumen = Decimal("0")
        for umbral, pct in self.config.descuentos_volumen_kg.items():
            try:
                if peso_kg >= Decimal(umbral):
                    por_volumen = max(por_volumen, pct)
            except InvalidOperation:
                continue
        return min(por_tipo + por_volumen, self.config.descuento_maximo)

    def _precio_desactualizado(self, actualizado: str, hoy: date) -> bool:
        if not actualizado:
            return True
        try:
            fecha = date.fromisoformat(actualizado[:10])
        except ValueError:
            return True
        return (hoy - fecha).days > self.config.dias_alerta_precio

    # ------------------------------------------------------------ cotización
    def crear_cotizacion(self, res: ResultadoCalculo, cot_id: str, wa_id: str, cliente_nombre: str,
                         cliente_ruc: str = "", notas: str = "", ahora: datetime | None = None) -> Cotizacion:
        from .modelos import ahora_utc

        ahora = ahora or ahora_utc()
        return Cotizacion(
            id=cot_id,
            fecha=a_iso(ahora),
            wa_id=wa_id,
            cliente_nombre=cliente_nombre.strip() or "Cliente",
            cliente_ruc=cliente_ruc.strip(),
            tipo_cliente=res.tipo_cliente,
            lineas=res.lineas,
            subtotal_bruto_usd=res.subtotal_bruto_usd,
            descuento_pct=res.descuento_pct,
            descuento_usd=res.descuento_usd,
            base_imponible_usd=res.base_imponible_usd,
            igv_usd=res.igv_usd,
            total_usd=res.total_usd,
            peso_total_kg=res.peso_total_kg,
            tipo_cambio=res.tipo_cambio,
            total_pen=res.total_pen,
            vigencia_hasta=a_iso(ahora + timedelta(hours=self.config.vigencia_horas)),
            alertas=list(res.alertas_internas),
            avisos=list(res.avisos_cliente),
            notas=notas.strip(),
        )
