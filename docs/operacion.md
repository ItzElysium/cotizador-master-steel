# Operación diaria (para el cliente)

## Rutina del vendedor

| Momento | Acción |
|---|---|
| Al iniciar el día | Escribir `/pendientes` al número del bot. Abre la ventana de 24 h para recibir avisos y muestra lo que quedó por aprobar. |
| Si el precio del acero cambió | `/precio FC-0500 8.90`, o subir el Excel actualizado |
| Si un producto se agotó | `/stock PG-090 agotado` |
| Cuando llega una cotización | Revisar el PDF → **✅ Enviar** o **🙋 La atiendo yo** |
| Cuando se cierra la venta | `/ganada COT-000123` |
| Cuando se pierde | `/perdida COT-000123 precio` (u otro motivo: stock, plazo, crédito) |
| Fin de semana | `/resumen 7` |

Registrar ganadas y perdidas es lo que convierte el bot en un **sistema de inteligencia comercial**: muestra la tasa de cierre real, en qué productos se pierde por precio y qué se cotiza más.

## Comandos

| Comando | Efecto |
|---|---|
| `/ayuda` | Lista de comandos |
| `/pendientes` | Cotizaciones por aprobar, con botones |
| `/cot COT-000123` | Ver el detalle |
| `/ganada COT-000123` · `/perdida COT-000123 motivo` | Cerrar el ciclo comercial |
| `/tc` · `/tc 3.75` | Ver el TC o fijarlo para el día (si no, se usa el TC SBS venta del BCRP) |
| `/precio SKU valor` | Nuevo precio en US$ sin IGV (misma base de la lista: por unidad o por kg) |
| `/stock SKU disponible\|consultar\|agotado` | Disponibilidad |
| `/buscar texto` | Consultar el catálogo y los precios vigentes |
| `/humano 519…` · `/bot 519…` | Pausar o reactivar el bot con un cliente |
| `/resumen [hoy\|7\|30]` | Indicadores del periodo |

Los mensajes del vendedor que **no** empiezan con `/` se tratan como si fuera un cliente. Sirve para probar el bot desde su propio celular.

## Actualizar la lista de precios

- **Cambio puntual:** `/precio` o `/stock` por WhatsApp.
- **Cambio masivo:** editar `plantilla_lista_precios.xlsx` y subirla:
  ```bash
  python scripts/cargar_catalogo.py lista.xlsx --url https://<app>.azurewebsites.net --clave <clave>
  ```
- **Solo precios:** un CSV con `sku,precio_usd`. Los demás datos de cada producto se conservan.
- La columna `sinonimos` define cómo entiende el bot la jerga. Si un cliente pide algo de una forma que el bot no reconoce, agrégala ahí.

## Reportes (Excel / Power BI)

- Cabeceras: `GET https://<app>.azurewebsites.net/api/admin/cotizaciones?code=<clave>`
- Detalle por producto: `…/api/admin/cotizaciones?detalle=1&code=<clave>`

En Power BI: *Obtener datos → Web* con esa URL. Métricas sugeridas: cotizaciones por día, monto cotizado vs. ganado, tasa de cierre por tipo de cliente, kg por familia de producto, motivos de pérdida y tiempo hasta el cierre.
