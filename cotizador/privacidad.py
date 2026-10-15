"""Política de privacidad pública del cotizador (Meta la exige para publicar la app de WhatsApp).

Se sirve en GET /api/privacidad y se arma con los datos de la empresa (EMPRESA_*), así cada cliente
tiene la suya sin tocar código. Es una base razonable para la Ley N.° 29733 (Perú): conviene que el
área legal del cliente la revise antes de producción.
"""
from __future__ import annotations

from html import escape

from .config import Config

ACTUALIZACION = "15 de octubre de 2026"

_PROVEEDORES_IA = {
    "anthropic": "Anthropic (modelo Claude)",
    "gemini": "Google (modelo Gemini)",
    "deepseek": "DeepSeek",
    "azure_openai": "Microsoft (Azure OpenAI)",
}


def _contacto(config: Config) -> str:
    e = config.empresa
    medios = []
    if e.email:
        medios.append(f'el correo <a href="mailto:{escape(e.email)}">{escape(e.email)}</a>')
    if e.telefono:
        medios.append(f"el teléfono {escape(e.telefono)}")
    medios.append("este mismo número de WhatsApp")
    return ", ".join(medios[:-1]) + (" o " if len(medios) > 1 else "") + medios[-1]


def pagina_privacidad(config: Config) -> str:
    e = config.empresa
    nombre = escape(e.nombre)
    responsable = nombre + (f", con RUC {escape(e.ruc)}" if e.ruc else "") + (
        f", con domicilio en {escape(e.direccion)}" if e.direccion else "")
    ia = escape(_PROVEEDORES_IA.get(config.llm_proveedor, "un proveedor de inteligencia artificial"))
    contacto = _contacto(config)
    color = escape(e.color_primario or "#0168AA")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de privacidad · {nombre}</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; background: #f6f8fa;
         color: #1f2328; line-height: 1.6; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 56px; background: #fff; }}
  h1 {{ color: {color}; font-size: 1.6rem; margin-bottom: 4px; }}
  h2 {{ color: {color}; font-size: 1.1rem; margin-top: 28px; }}
  .meta {{ color: #59636e; font-size: .9rem; }}
  li {{ margin: 4px 0; }}
</style></head>
<body><main>
<h1>Política de privacidad</h1>
<p class="meta">{nombre} · Cotizaciones por WhatsApp · Última actualización: {ACTUALIZACION}</p>

<h2>1. Responsable</h2>
<p>El responsable del tratamiento de tus datos personales es {responsable}. Esta política explica qué datos
usamos cuando nos escribes por WhatsApp para pedir precios o cotizaciones, y cómo los protegemos.</p>

<h2>2. Datos que tratamos</h2>
<ul>
  <li>Tu número de WhatsApp y el nombre de tu perfil.</li>
  <li>Los mensajes, fotos, audios o documentos que nos envías (por ejemplo, tu lista de materiales).</li>
  <li>Los datos que nos des para la cotización: nombre o razón social, RUC, dirección de entrega.</li>
  <li>Las cotizaciones generadas y su estado (enviada, aceptada o no concretada).</li>
</ul>
<p>No pedimos ni guardamos datos de tarjetas, contraseñas ni datos sensibles.</p>

<h2>3. Para qué los usamos</h2>
<ul>
  <li>Responder tus consultas y preparar cotizaciones de nuestros productos.</li>
  <li>Hacer seguimiento comercial de tus cotizaciones y atender tus pedidos.</li>
  <li>Mejorar nuestro servicio mediante estadísticas internas de ventas.</li>
</ul>
<p>No vendemos ni alquilamos tus datos personales.</p>

<h2>4. Asistente automático</h2>
<p>Un asistente de inteligencia artificial nos ayuda a entender tu pedido y a preparar la cotización. Los
precios, pesos, descuentos y tipo de cambio los calcula nuestro sistema a partir de nuestra lista de precios,
y un asesor puede revisar la cotización antes de enviártela. Puedes pedir hablar con un asesor en cualquier
momento.</p>

<h2>5. Con quién los compartimos</h2>
<p>Solo con proveedores que nos prestan servicios necesarios para atenderte, bajo sus propias condiciones de
seguridad y confidencialidad:</p>
<ul>
  <li>Meta Platforms (WhatsApp Business Platform), para enviar y recibir mensajes.</li>
  <li>Microsoft (Azure), donde se aloja y almacena el sistema de cotizaciones.</li>
  <li>{ia}, que procesa el texto de tus mensajes para entender tu pedido.</li>
  <li>DataPrix, proveedor tecnológico que desarrolla y mantiene el sistema.</li>
</ul>
<p>Estos proveedores pueden tener servidores fuera del Perú (por ejemplo, en Estados Unidos), por lo que tus
datos pueden ser objeto de transferencia internacional con el único fin de prestar este servicio.</p>

<h2>6. Cuánto tiempo los conservamos</h2>
<p>Mientras sea necesario para atender tus consultas y cotizaciones, y durante los plazos que exijan las normas
tributarias y comerciales. Después los eliminamos o anonimizamos.</p>

<h2 id="eliminacion">7. Tus derechos y cómo eliminar tus datos</h2>
<p>Conforme a la Ley N.° 29733, Ley de Protección de Datos Personales, y su reglamento, puedes solicitar el
acceso, rectificación, cancelación (eliminación) u oposición al tratamiento de tus datos. Escríbenos por
{contacto}, indicando tu nombre y tu solicitud, y la atenderemos en los plazos que establece la ley. Si no
quedas conforme con la respuesta, puedes acudir a la Autoridad Nacional de Protección de Datos Personales.</p>

<h2>8. Seguridad</h2>
<p>Usamos conexiones cifradas, acceso restringido por identidades administradas y almacenamiento en la nube con
controles de seguridad. Solo el personal autorizado puede ver tus conversaciones y cotizaciones.</p>

<h2>9. Cambios</h2>
<p>Si actualizamos esta política, publicaremos la nueva versión en esta misma dirección con su fecha.</p>
</main></body></html>"""
