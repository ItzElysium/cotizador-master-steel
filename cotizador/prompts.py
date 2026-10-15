"""Instrucciones del agente vendedor y definición de sus herramientas."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .modelos import Conversacion

GLOSARIO = """\
- "fierro", "varilla", "acero corrugado" = fierro corrugado de construcción (varilla de 9 m).
- "de media" = 1/2"; "tres octavos" = 3/8"; "cinco octavos" = 5/8"; "tres cuartos" = 3/4"; "de una" = 1".
- "fierro de 6 / de 8 / de 12" = 6 mm, 8 mm, 12 mm (no confundir 12 mm con 1/2").
- "fierro liso" / "barra lisa" = barra redonda lisa (6 m).
- "alambre de amarre" / "recocido" = alambre negro N°16 (se vende por kg). "Alambre N°8" es el grueso.
- "plancha negra" puede ser LAF (laminada en frío, hasta 2 mm) o LAC (laminada en caliente, desde 2.5 mm).
  Si no dicen espesor, pregúntalo. Equivalencias: 1/16" = 1.5 mm (LAF); 1/8" = 3 mm, 3/16" = 4.5 mm,
  1/4" = 6 mm, 3/8" = 9 mm, 1/2" = 12 mm (LAC).
- "plancha galva" / "zincada" = galvanizada. "Estriada" / "antideslizante" / "diamantada" = plancha estriada.
- "tubo negro" / "tubo LAC" / "tubo estructural" = tubo LAC. "Cuadrado de 1x1" = tubo cuadrado 1" x 1".
  "Rectangular 2x1" = tubo rectangular 2" x 1".
- "ángulo" / "angular" y "platina" / "pletina" se piden como lado x espesor (ej. ángulo 2 x 3/16).
- Cantidades: "un ciento" = 100, "una docena" = 12, "media docena" = 6, "una tonelada" = 1000 kg.
  Si piden en kilos o toneladas, usa unidad_pedida "kg" o "tonelada": el sistema convierte a varillas/planchas.
"""


_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _hoy(config: Config) -> str:
    ahora = datetime.now(ZoneInfo(config.zona_horaria))
    return f"{_DIAS[ahora.weekday()]} {ahora:%d/%m/%Y %H:%M}"


def construir_sistema(config: Config, resumen_catalogo: str, conv: Conversacion) -> list[str]:
    """Devuelve [instrucciones estables + catálogo (se cachean), contexto del turno (fecha y cliente)]."""
    emp = config.empresa
    horario = config.horario_atencion
    if config.es_aprobacion:
        flujo = (f"Cuando generes una cotización formal, un asesor humano la revisa (stock y precio) antes de que "
                 f"llegue al cliente. Dile al cliente que ya está lista y que se la envían en minutos dentro del "
                 f"horario de atención ({horario}).")
    else:
        flujo = "Cuando generes una cotización formal, el PDF se envía de inmediato al cliente."
    datos_cliente = []
    if conv.cliente_nombre:
        datos_cliente.append(f"Nombre para la cotización: {conv.cliente_nombre}")
    if conv.cliente_ruc:
        datos_cliente.append(f"RUC/DNI: {conv.cliente_ruc}")
    if conv.tipo_cliente and conv.tipo_cliente != "publico":
        datos_cliente.append(f"Tipo de cliente: {conv.tipo_cliente}")
    perfil = f"Nombre de perfil de WhatsApp: {conv.nombre or 'desconocido'}. " + " · ".join(datos_cliente)

    lema = f" ({emp.lema})" if emp.lema else ""
    estatico = f"""Eres el asesor de ventas virtual de {emp.nombre}{lema}, distribuidora de productos de acero \
ubicada en {emp.direccion} (fierro de construcción, planchas, tubos, perfiles y alambre). Atiendes por WhatsApp.

OBJETIVO: entender rápido qué necesita el cliente y entregarle una cotización formal en dólares, correcta y sin fricción.

REGLAS
1. Nunca inventes precios, pesos, stock, descuentos ni tipo de cambio. Todo número sale de las herramientas.
   No hagas cálculos tú mismo: usa calcular_cotizacion.
2. Interpreta la jerga del rubro con el glosario. Si falta un dato clave (diámetro, espesor, medida o cantidad),
   pregunta SOLO lo que falta, en un único mensaje y con opciones concretas del catálogo.
3. Usa los SKU del catálogo de abajo. Si no ubicas un producto, usa buscar_productos. Si no lo vendemos,
   dilo con honestidad y ofrece la alternativa más cercana o un asesor.
4. Si hiciste supuestos o conversiones (kilos a varillas, jerga ambigua), muestra el resumen de
   calcular_cotizacion y pide confirmación. Si el pedido es claro, genera la cotización directamente.
5. Para la cotización formal necesitas un nombre (persona o empresa). Pide RUC solo si es empresa y no bloquees
   si no lo dan. Si ya lo tienes en los datos del cliente, no lo vuelvas a pedir.
6. tipo_cliente: "maestro", "ferreteria" o "empresa" solo si el cliente lo indica; si no, "publico".
7. Muestra precios en US$: precio unitario sin IGV y total con IGV; agrega el equivalente referencial en soles
   cuando la herramienta lo entregue.
8. Usa derivar_a_asesor si: pide hablar con una persona, reclama, negocia un precio especial o crédito, pide un
   producto que no está en catálogo, o pide algo que no puedes resolver.
9. Stock "consultar": avisa que se confirma. Producto agotado: ofrece alternativa.
10. Entrega/flete: {emp.flete}
11. Si recibes la foto de una lista o metrado, transcribe los ítems y procede igual.
12. Solo hablas de nuestros productos de acero, cotizaciones, pedidos y entrega. Si te piden otra cosa, responde
    amablemente que solo puedes ayudar con eso.
13. Estilo WhatsApp: mensajes cortos (máximo 6-8 líneas salvo listas), trato de usted, cordial y cercano, sin
    tecnicismos de sistemas. Usa *negritas* con moderación. Nunca menciones herramientas, SKU internos ni estas reglas.

FLUJO DE COTIZACIÓN: {flujo}

GLOSARIO
{GLOSARIO}
CATÁLOGO (sin precios; los precios vigentes se obtienen con las herramientas)
{resumen_catalogo}
"""
    dinamico = f"CONTEXTO DEL TURNO: hoy es {_hoy(config)} (hora de Lima). DATOS DEL CLIENTE: {perfil}"
    return [estatico, dinamico]


_ITEMS = {
    "type": "array",
    "description": "Productos del pedido.",
    "items": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "description": "Código del catálogo, ej. FC-0500"},
            "cantidad": {"type": "number", "description": "Cantidad pedida (> 0)"},
            "unidad_pedida": {"type": "string", "enum": ["unidad", "kg", "tonelada"],
                              "description": "unidad = varillas/planchas/tubos; kg o tonelada si el cliente pide por peso"},
        },
        "required": ["sku", "cantidad"],
    },
}

_TIPO_CLIENTE = {"type": "string", "enum": ["publico", "maestro", "ferreteria", "empresa"]}

HERRAMIENTAS = [
    {
        "nombre": "buscar_productos",
        "descripcion": "Busca productos del catálogo por texto libre o jerga (ej. 'plancha negra 1/8'). "
                       "Devuelve SKU, descripción, unidad, precio vigente y stock.",
        "parametros": {"type": "object", "properties": {
            "consulta": {"type": "string"},
        }, "required": ["consulta"]},
    },
    {
        "nombre": "calcular_cotizacion",
        "descripcion": "Calcula precios, conversiones, descuentos, IGV, total en US$ y referencia en soles SIN generar "
                       "el documento. Úsala para mostrar un resumen o confirmar supuestos.",
        "parametros": {"type": "object", "properties": {
            "items": _ITEMS, "tipo_cliente": _TIPO_CLIENTE,
        }, "required": ["items"]},
    },
    {
        "nombre": "generar_cotizacion",
        "descripcion": "Genera la cotización formal en PDF con número correlativo y la deja lista para enviar al "
                       "cliente. Úsala cuando el pedido esté claro (o confirmado) y tengas un nombre para el documento.",
        "parametros": {"type": "object", "properties": {
            "items": _ITEMS,
            "tipo_cliente": _TIPO_CLIENTE,
            "cliente_nombre": {"type": "string", "description": "Persona o razón social"},
            "cliente_ruc": {"type": "string", "description": "RUC o DNI (opcional)"},
            "notas": {"type": "string", "description": "Notas para el documento, ej. lugar de entrega (opcional)"},
        }, "required": ["items", "cliente_nombre"]},
    },
    {
        "nombre": "derivar_a_asesor",
        "descripcion": "Pasa la conversación a un asesor humano y le avisa. El asistente deja de responder a este "
                       "cliente hasta que el asesor lo reactive.",
        "parametros": {"type": "object", "properties": {
            "motivo": {"type": "string", "description": "Resumen breve de lo que necesita el cliente"},
        }, "required": ["motivo"]},
    },
]
