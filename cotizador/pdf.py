"""Generación del PDF de cotización (reportlab, sin dependencias del sistema)."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import Config
from .modelos import Cotizacion, de_iso
from .precios import fmt, fmt_cantidad, fmt_precio

TIPOS_CLIENTE = {"publico": "Público", "maestro": "Maestro de obra", "ferreteria": "Ferretería",
                 "empresa": "Empresa / constructora"}


def _esc(texto: str) -> str:
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fecha_local(iso: str, zona: str, con_hora: bool = True) -> str:
    dt = de_iso(iso)
    if not dt:
        return "-"
    local = dt.astimezone(ZoneInfo(zona))
    return local.strftime("%d/%m/%Y %H:%M" if con_hora else "%d/%m/%Y")


def generar_pdf(cot: Cotizacion, config: Config) -> bytes:
    emp = config.empresa
    primario = colors.HexColor(emp.color_primario)
    acento = colors.HexColor(emp.color_acento)
    gris = colors.HexColor("#6B7280")
    gris_claro = colors.HexColor("#F3F4F6")
    borde = colors.HexColor("#D1D5DB")

    base = ParagraphStyle("base", fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.HexColor("#111827"))
    peq = ParagraphStyle("peq", parent=base, fontSize=7.2, leading=9, textColor=gris)
    negrita = ParagraphStyle("neg", parent=base, fontName="Helvetica-Bold")
    titulo_emp = ParagraphStyle("temp", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=17, textColor=primario)
    seccion = ParagraphStyle("sec", parent=base, fontName="Helvetica-Bold", fontSize=8, textColor=acento, spaceAfter=2)
    derecha = ParagraphStyle("der", parent=base, alignment=TA_RIGHT)
    cab = ParagraphStyle("cab", parent=base, fontName="Helvetica-Bold", fontSize=7.8, textColor=colors.white)
    cab_der = ParagraphStyle("cabd", parent=cab, alignment=TA_RIGHT)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Cotización {cot.id}", author=emp.nombre)
    ancho = A4[0] - 30 * mm
    historia = []

    # ---------------- encabezado
    datos_emp = [Paragraph(_esc(emp.nombre), titulo_emp)]
    if emp.lema:
        datos_emp.append(Paragraph(_esc(emp.lema), ParagraphStyle("lema", parent=peq, textColor=acento)))
    if emp.ruc:
        datos_emp.append(Paragraph(f"RUC {_esc(emp.ruc)}", base))
    datos_emp.append(Paragraph(_esc(emp.direccion), peq))
    contacto = " · ".join(_esc(x) for x in [emp.telefono, emp.email, emp.web] if x)
    if contacto:
        datos_emp.append(Paragraph(contacto, peq))
    logo = Path(emp.logo_ruta)
    if logo.is_file():
        bloque_emp = Table([[Image(str(logo), width=24 * mm, height=24 * mm, kind="proportional"), datos_emp]],
                           colWidths=[28 * mm, None])
        bloque_emp.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    else:
        bloque_emp = datos_emp

    caja = Table([
        [Paragraph("COTIZACIÓN", ParagraphStyle("c1", parent=cab, fontSize=11, leading=13))],
        [Paragraph(f"N° {_esc(cot.id)}", ParagraphStyle("c2", parent=cab, fontSize=10))],
        [Paragraph(f"Fecha: {_fecha_local(cot.fecha, config.zona_horaria)}", ParagraphStyle("c3", parent=cab, fontName="Helvetica"))],
        [Paragraph(f"Válida hasta: {_fecha_local(cot.vigencia_hasta, config.zona_horaria)}",
                   ParagraphStyle("c4", parent=cab, fontName="Helvetica"))],
    ], colWidths=[62 * mm])
    caja.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), primario),
        ("LINEBELOW", (0, 1), (-1, 1), 1.2, acento),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    encabezado = Table([[bloque_emp, caja]], colWidths=[ancho - 64 * mm, 64 * mm])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    historia += [encabezado, Spacer(1, 6 * mm)]

    # ---------------- cliente
    cliente = [
        [Paragraph("CLIENTE", seccion), ""],
        [Paragraph(f"<b>Señor(es):</b> {_esc(cot.cliente_nombre)}", base),
         Paragraph(f"<b>RUC/DNI:</b> {_esc(cot.cliente_ruc) or '-'}", base)],
        [Paragraph(f"<b>Tipo de cliente:</b> {TIPOS_CLIENTE.get(cot.tipo_cliente, cot.tipo_cliente)}", base),
         Paragraph(f"<b>WhatsApp:</b> +{_esc(cot.wa_id)}", base)],
    ]
    t_cliente = Table(cliente, colWidths=[ancho * 0.62, ancho * 0.38])
    t_cliente.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, borde), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFAFA")),
        ("SPAN", (0, 0), (-1, 0)), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    historia += [t_cliente, Spacer(1, 5 * mm)]

    # ---------------- detalle
    columnas = [7 * mm, 19 * mm, None, 15 * mm, 18 * mm, 21 * mm, 24 * mm]
    fijo = sum(c for c in columnas if c)
    columnas[2] = ancho - fijo
    filas = [[Paragraph("#", cab), Paragraph("Código", cab), Paragraph("Descripción", cab),
              Paragraph("Cant.", cab_der), Paragraph("Peso kg", cab_der), Paragraph("P. unit. US$", cab_der),
              Paragraph("Subtotal US$", cab_der)]]
    for i, ln in enumerate(cot.lineas, start=1):
        detalle = f"{_esc(ln.descripcion)}<br/><font size=7 color='#6B7280'>{_esc(ln.unidad)}"
        if ln.unidad.lower() != "kg":
            detalle += f" · {fmt(ln.peso_unitario_kg)} kg c/u"
        if ln.stock == "consultar":
            detalle += " · <font color='#B45309'>stock por confirmar</font>"
        if ln.nota:
            detalle += f"<br/>{_esc(ln.nota)}"
        detalle += "</font>"
        filas.append([
            Paragraph(str(i), base), Paragraph(_esc(ln.sku), peq), Paragraph(detalle, base),
            Paragraph(fmt_cantidad(ln.cantidad), derecha), Paragraph(fmt(ln.peso_total_kg), derecha),
            Paragraph(fmt_precio(ln.precio_unitario_usd), derecha), Paragraph(fmt(ln.subtotal_usd), derecha),
        ])
    t_detalle = Table(filas, colWidths=columnas, repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), primario),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, borde),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for fila in range(2, len(filas), 2):
        estilo.append(("BACKGROUND", (0, fila), (-1, fila), gris_claro))
    t_detalle.setStyle(TableStyle(estilo))
    historia += [t_detalle, Spacer(1, 4 * mm)]

    # ---------------- totales + resumen
    tot = [["Subtotal", f"US$ {fmt(cot.subtotal_bruto_usd)}"]]
    if cot.descuento_usd:
        tot.append([f"Descuento ({fmt(cot.descuento_pct * 100, 1)}%)", f"- US$ {fmt(cot.descuento_usd)}"])
    tot += [["Valor de venta", f"US$ {fmt(cot.base_imponible_usd)}"],
            [f"IGV ({fmt(config.igv_tasa * 100, 0)}%)", f"US$ {fmt(cot.igv_usd)}"],
            ["TOTAL", f"US$ {fmt(cot.total_usd)}"]]
    filas_tot = [[Paragraph(a, derecha), Paragraph(b, derecha)] for a, b in tot]
    filas_tot[-1] = [Paragraph("<b>TOTAL</b>", ParagraphStyle("tt", parent=derecha, textColor=colors.white, fontSize=10)),
                     Paragraph(f"<b>US$ {fmt(cot.total_usd)}</b>",
                               ParagraphStyle("tt2", parent=derecha, textColor=colors.white, fontSize=10))]
    if cot.total_pen is not None and cot.tipo_cambio:
        filas_tot.append([Paragraph("Equivalente referencial", peq),
                          Paragraph(f"S/ {fmt(cot.total_pen)}", ParagraphStyle("pen", parent=derecha, fontName="Helvetica-Bold"))])
    t_tot = Table(filas_tot, colWidths=[38 * mm, 34 * mm])
    idx_total = len(tot) - 1
    t_tot.setStyle(TableStyle([
        ("BACKGROUND", (0, idx_total), (-1, idx_total), acento),
        ("LINEABOVE", (0, idx_total), (-1, idx_total), 0.8, acento),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
    ]))

    resumen = [Paragraph("RESUMEN", seccion),
               Paragraph(f"<b>Peso total teórico:</b> {fmt(cot.peso_total_kg)} kg "
                         f"({fmt(cot.peso_total_kg / 1000, 2)} t)", base)]
    if cot.tipo_cambio:
        resumen.append(Paragraph(
            f"<b>Tipo de cambio:</b> S/ {cot.tipo_cambio.valor} por US$ ({_esc(cot.tipo_cambio.fuente)}, "
            f"{_esc(cot.tipo_cambio.fecha)}). El monto en soles es referencial; se factura en dólares "
            f"o al tipo de cambio del día de pago.", peq))
    resumen.append(Paragraph(f"<b>Entrega:</b> {_esc(emp.flete)}", base))
    for aviso in cot.avisos:
        resumen.append(Paragraph(f"<font color='#B45309'>• {_esc(aviso)}</font>", peq))
    if cot.notas:
        resumen.append(Paragraph(f"<b>Notas:</b> {_esc(cot.notas)}", base))
    t_res = Table([[resumen, t_tot]], colWidths=[ancho - 76 * mm, 76 * mm])
    t_res.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    historia += [KeepTogether([t_res]), Spacer(1, 6 * mm)]

    # ---------------- condiciones y cuentas
    condiciones = [Paragraph("CONDICIONES COMERCIALES", seccion)] + \
                  [Paragraph(f"• {_esc(c)}", peq) for c in emp.condiciones] + \
                  [Paragraph(f"• Vigencia de {config.vigencia_horas} horas por la volatilidad del precio del acero.", peq)]
    cuentas = [Paragraph("CUENTAS PARA DEPÓSITO", seccion)] + [Paragraph(_esc(c), peq) for c in emp.cuentas]
    t_cond = Table([[condiciones, cuentas]], colWidths=[ancho * 0.6, ancho * 0.4])
    t_cond.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, borde),
                                ("TOPPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    historia.append(KeepTogether([t_cond]))

    pie_texto = "Cotización generada automáticamente. No es comprobante de pago."
    if emp.pdf_marca:
        pie_texto += f"  ·  {emp.pdf_marca}"

    def pie(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 6.8)
        canvas.setFillColor(gris)
        canvas.drawString(15 * mm, 9 * mm, pie_texto)
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"{cot.id} · pág. {documento.page}")
        canvas.setStrokeColor(acento)
        canvas.setLineWidth(2)
        canvas.line(0, A4[1] - 3, A4[0], A4[1] - 3)
        canvas.restoreState()

    doc.build(historia, onFirstPage=pie, onLaterPages=pie)
    return buffer.getvalue()


def nombre_archivo(cot: Cotizacion) -> str:
    return f"Cotizacion_{cot.id}.pdf"


def ahora_local(config: Config) -> datetime:
    return datetime.now(ZoneInfo(config.zona_horaria))
