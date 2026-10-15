# Configurar WhatsApp Cloud API (conexión directa con Meta)

Conexión **directa** con Meta, sin BSP intermediario: no hay suscripción de terceros y solo se pagan las tarifas de Meta.

## 0. Antes de empezar

| Necesitas | Detalle |
|---|---|
| Portafolio comercial de Meta | business.facebook.com. Conviene verificar la empresa (RUC y documentos): sube los límites de envío y habilita el nombre visible. |
| Un número de teléfono | Ver la decisión del punto 2. |
| URL del webhook | Sale de `infra/deploy.sh`: `https://func-…azurewebsites.net/api/whatsapp/webhook` |
| Política de privacidad (URL) | Meta la exige para pasar la app a modo *Live*. Puede ser una página simple en la web del cliente. |
| Método de pago en WhatsApp Manager | Desde el 1 de octubre de 2026 se cobran las respuestas que pasan de 1,000 mensajes al mes por número. |

## 1. Crear la app

1. Ve a developers.facebook.com → **Mis apps** → **Crear app** → tipo **Business**.
2. Agrega el producto **WhatsApp** y vincúlalo al portafolio comercial del cliente.

## 2. El número: ¿nuevo o el actual?

| Opción | Pros | Contras |
|---|---|---|
| **A. Número nuevo dedicado** (recomendado para el MVP) | Configuración simple y estable; 100% API | El cliente debe comunicar el número nuevo |
| **B. Coexistencia con el número actual** | Mantiene el número conocido y los chats en la app WhatsApp Business | Se activa con *Embedded Signup* (QR desde la app), normalmente a través de un proveedor tecnológico; algunos proveedores la reportan como menos estable |

Con la opción B, suscribe también el campo `smb_message_echoes`. Así, cuando el vendedor responde desde la app, el bot se pausa solo con ese cliente.

En **WhatsApp → Configuración de la API**, agrega y verifica el número, define el nombre visible y anota:
- **Phone number ID** → `WA_PHONE_NUMBER_ID`
- **WhatsApp Business Account ID** (para referencia)

## 3. Token permanente (no uses el token temporal de 24 h)

1. business.facebook.com → **Configuración** → **Usuarios del sistema** → **Agregar** (rol Administrador).
2. **Asignar activos**: la app (control total) y la cuenta de WhatsApp (control total).
3. **Generar token**: elige la app, la caducidad **Nunca** y los permisos `whatsapp_business_messaging` y `whatsapp_business_management`.
4. Guárdalo como `WA_TOKEN`.

## 4. App secret y verify token

- **App secret**: en la app → Configuración → Básica → *Clave secreta de la app* → `WA_APP_SECRET`. Se usa para validar la firma de cada webhook.
- **Verify token**: una cadena larga inventada por ti → `WA_VERIFY_TOKEN`.

Vuelve a correr `./infra/deploy.sh` para cargar estos valores en Azure.

## 5. Webhook

1. En la app → **WhatsApp** → **Configuración** → **Webhook** → **Editar**.
2. **URL de devolución de llamada**: la URL del webhook. **Token de verificación**: `WA_VERIFY_TOKEN`.
3. **Verificar y guardar**. Si falla, revisa `https://…/api/salud` y que el token coincida.
4. En *Campos del webhook*, suscribe `messages` (y `smb_message_echoes` si usas coexistencia). El campo `messages` también trae los estados de entrega: si un mensaje al cliente falla (por ejemplo, por la ventana de 24 h), el vendedor recibe un aviso.
5. Pasa la app a modo **Live** (requiere la URL de política de privacidad).

## 6. Prueba

Desde tu celular, escribe al número: *"hola, cuánto está el fierro de media?"*. Deberías ver la respuesta del agente. En Application Insights aparecen las trazas de `whatsapp_webhook` y `procesar_mensaje`.

## 7. Aviso al vendedor fuera de la ventana de 24 h (opcional, recomendado)

WhatsApp solo permite mensajes libres a quien te escribió en las últimas 24 h. Si el vendedor no le ha escrito al bot ese día, el aviso de "cotización por aprobar" no le llega. Hay dos soluciones:

- **Rutina simple:** el vendedor escribe `/pendientes` al bot al iniciar su jornada. Eso abre la ventana del día.
- **Plantilla de utilidad** (WhatsApp Manager → Plantillas → Crear):
  - Nombre: `cotizacion_pendiente` · Categoría: **Utilidad** · Idioma: español.
  - Cuerpo: `Tienes la cotización {{1}} de {{2}} por {{3}} pendiente de aprobación.`
  - Botón de respuesta rápida: `Ver pendientes`.
  - Cuando Meta la apruebe: `WA_PLANTILLA_APROBACION=cotizacion_pendiente`.

## 8. Costos de Meta (Perú)

Las respuestas dentro de la ventana de atención tienen 1,000 mensajes gratis al mes por número. Después se cobran a la tarifa de utilidad (alrededor de S/ 0.10 por mensaje). Las plantillas de marketing cuestan más. Revisa el tarifario vigente en la documentación de precios de WhatsApp Business Platform.

El diseño del bot ya optimiza el costo: agrupa los mensajes seguidos y responde en un solo mensaje.

## Errores frecuentes

| Síntoma | Causa probable |
|---|---|
| Meta no verifica el webhook | `WA_VERIFY_TOKEN` distinto, o la app aún no está desplegada |
| `401 firma inválida` en los logs | `WA_APP_SECRET` incorrecto |
| Error 131047 al avisar al vendedor | Pasaron más de 24 h desde su último mensaje: usa la plantilla del punto 7 |
| Error 190 (token) | Token temporal vencido: usa el token del usuario del sistema |
| El bot no responde a un cliente | Está en pausa (asesor): `/bot 519…` lo reactiva |
