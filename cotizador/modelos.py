"""Modelos de dominio: productos, cotizaciones, conversaciones y mensajes."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def a_iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def de_iso(texto: str | None) -> datetime | None:
    if not texto:
        return None
    dt = datetime.fromisoformat(texto)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- Catálogo

@dataclass
class Producto:
    sku: str
    categoria: str
    descripcion: str
    sinonimos: list[str]
    unidad: str                 # cómo se vende: "varilla 9 m", "plancha 1.20 x 2.40 m", "kg"
    peso_kg: Decimal            # peso teórico de UNA unidad de venta (1 si se vende por kg)
    precio_usd: Decimal         # precio sin IGV, por unidad o por kg según base_precio
    base_precio: str = "unidad"  # unidad | kg
    stock: str = "disponible"   # disponible | consultar | agotado
    actualizado: str = ""       # AAAA-MM-DD de la última actualización de precio

    @property
    def precio_unitario_usd(self) -> Decimal:
        """Precio sin IGV de una unidad de venta."""
        if self.base_precio == "kg":
            return self.precio_usd * self.peso_kg
        return self.precio_usd

    @property
    def se_vende_por_kg(self) -> bool:
        return self.unidad.strip().lower() == "kg"

    def a_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "categoria": self.categoria,
            "descripcion": self.descripcion,
            "sinonimos": "|".join(self.sinonimos),
            "unidad": self.unidad,
            "peso_kg": str(self.peso_kg),
            "precio_usd": str(self.precio_usd),
            "base_precio": self.base_precio,
            "stock": self.stock,
            "actualizado": self.actualizado,
        }

    @classmethod
    def de_dict(cls, d: dict[str, Any]) -> "Producto":
        sinonimos = d.get("sinonimos") or ""
        if isinstance(sinonimos, str):
            sinonimos = [s.strip() for s in sinonimos.split("|") if s.strip()]
        return cls(
            sku=str(d["sku"]).strip(),
            categoria=str(d.get("categoria", "")).strip(),
            descripcion=str(d.get("descripcion", "")).strip(),
            sinonimos=list(sinonimos),
            unidad=str(d.get("unidad", "unidad")).strip(),
            peso_kg=Decimal(str(d.get("peso_kg") or "0")),
            precio_usd=Decimal(str(d.get("precio_usd") or "0")),
            base_precio=(str(d.get("base_precio") or "unidad").strip().lower()),
            stock=(str(d.get("stock") or "disponible").strip().lower()),
            actualizado=str(d.get("actualizado") or "").strip(),
        )


# ---------------------------------------------------------------- Cotización

@dataclass
class LineaCotizacion:
    sku: str
    descripcion: str
    unidad: str
    cantidad: Decimal
    peso_unitario_kg: Decimal
    peso_total_kg: Decimal
    precio_unitario_usd: Decimal
    subtotal_usd: Decimal
    stock: str
    nota: str = ""


@dataclass
class TipoCambio:
    valor: Decimal
    fecha: str
    fuente: str


@dataclass
class Cotizacion:
    id: str
    fecha: str
    wa_id: str
    cliente_nombre: str
    cliente_ruc: str
    tipo_cliente: str
    lineas: list[LineaCotizacion]
    subtotal_bruto_usd: Decimal
    descuento_pct: Decimal
    descuento_usd: Decimal
    base_imponible_usd: Decimal
    igv_usd: Decimal
    total_usd: Decimal
    peso_total_kg: Decimal
    tipo_cambio: TipoCambio | None
    total_pen: Decimal | None
    vigencia_hasta: str
    alertas: list[str] = field(default_factory=list)   # internas (solo para el vendedor)
    avisos: list[str] = field(default_factory=list)    # visibles para el cliente
    notas: str = ""
    estado: str = "borrador"   # borrador | pendiente_aprobacion | enviada | ganada | perdida | descartada
    motivo_cierre: str = ""
    pdf_ruta: str = ""

    def a_json(self) -> str:
        return json.dumps(asdict(self), default=str, ensure_ascii=False)

    @classmethod
    def de_json(cls, texto: str) -> "Cotizacion":
        d = json.loads(texto)
        lineas = [
            LineaCotizacion(**{k: (Decimal(v) if k in _DEC_LINEA else v) for k, v in ln.items()})
            for ln in d.pop("lineas")
        ]
        tc = d.pop("tipo_cambio")
        tipo_cambio = TipoCambio(Decimal(tc["valor"]), tc["fecha"], tc["fuente"]) if tc else None
        for k in _DEC_COT:
            if d.get(k) not in (None, "None"):
                d[k] = Decimal(d[k])
            else:
                d[k] = None
        return cls(lineas=lineas, tipo_cambio=tipo_cambio, **d)


_DEC_LINEA = {"cantidad", "peso_unitario_kg", "peso_total_kg", "precio_unitario_usd", "subtotal_usd"}
_DEC_COT = {"subtotal_bruto_usd", "descuento_pct", "descuento_usd", "base_imponible_usd", "igv_usd",
            "total_usd", "peso_total_kg", "total_pen"}


# ---------------------------------------------------------------- Mensajería

@dataclass
class MensajeEntrante:
    id: str
    wa_id: str
    nombre: str
    tipo: str               # text | image | audio | document | interactive | button | otro
    texto: str = ""
    media_id: str = ""
    mime: str = ""
    nombre_archivo: str = ""
    boton_id: str = ""
    timestamp: str = ""

    def a_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def de_dict(cls, d: dict[str, Any]) -> "MensajeEntrante":
        campos = {k: d.get(k, "") for k in cls.__dataclass_fields__}
        return cls(**{k: ("" if v is None else str(v)) for k, v in campos.items()})


@dataclass
class Conversacion:
    wa_id: str
    nombre: str = ""
    historial: list[dict[str, Any]] = field(default_factory=list)
    modo: str = "bot"            # bot | humano
    humano_hasta: str = ""
    cliente_nombre: str = ""
    cliente_ruc: str = ""
    tipo_cliente: str = "publico"
    actualizado: str = ""

    def en_modo_humano(self, ahora: datetime | None = None) -> bool:
        if self.modo != "humano":
            return False
        hasta = de_iso(self.humano_hasta)
        if hasta is None:
            return True  # pausa indefinida hasta que un admin reactive el bot
        return (ahora or ahora_utc()) < hasta
