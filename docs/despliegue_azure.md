# Despliegue en Azure (serverless)

## Recursos que se crean

| Recurso | Uso | Modelo de cobro |
|---|---|---|
| Function App · plan **Flex Consumption** (Linux, Python 3.12) | Webhook, procesamiento, tareas programadas | Por ejecución y memoria; escala a cero |
| Storage Account (Standard LRS) | **Table** = datos · **Blob** = PDFs · **Queue** = mensajes | Centavos al mes a este volumen |
| Identidad administrada + roles | Acceso al Storage sin claves | Gratis |
| Application Insights + Log Analytics | Logs, errores, trazas | Por GB ingerido |

Table Storage es compatible con **Azure Cosmos DB for Table**. Si el producto escala (muchos clientes o mucho volumen), se migra cambiando solo la conexión.

## Requisitos

- Suscripción de Azure con permiso para crear recursos y asignar roles (Owner, o Contributor + User Access Administrator).
- [Azure CLI](https://aka.ms/azcli) (`az login`) y Python 3.11 o superior.
- Opcional: [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local) (`func`) para publicar desde tu PC. Si no, publica con GitHub Actions.

## Paso a paso en Windows (PowerShell, solo requiere Azure CLI)

```powershell
az login
az functionapp list-flexconsumption-locations -o table        # regiones con Flex Consumption
Copy-Item infra\.env.example infra\.env
notepad infra\.env                                             # completar empresa, admins, claves
powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -Grupo rg-cotizador-acero -Region eastus
```

Para actualizar solo el código después de un cambio: `... infra\deploy.ps1 -Grupo rg-cotizador-acero -SoloCodigo`.

> **Suscripciones Azure for Students:** solo permiten desplegar en algunas regiones (política *Allowed resource deployment regions*). Para verlas:
> `az policy assignment list --disable-scope-strict-match --query "[?parameters.listOfAllowedLocations].parameters.listOfAllowedLocations.value" -o json`
> Elige una que también tenga Flex Consumption (por ejemplo `eastus`). Úsala solo para pruebas: sus condiciones no permiten dar servicio a terceros y se deshabilita cuando se acaba el crédito.

## Paso a paso en macOS / Linux (bash)

```bash
az login
az account set --subscription "<id o nombre>"

# 1. Región con soporte de Flex Consumption (elige la más cercana disponible)
az functionapp list-flexconsumption-locations -o table

# 2. Configuración
cp infra/.env.example infra/.env      # completar empresa, admins, claves

# 3. Infraestructura + código
./infra/deploy.sh rg-cotizador-acero eastus
```

El script imprime la URL del webhook (para Meta), la URL de salud y el comando para obtener la clave de administración.

```bash
# 4. Lista de precios del cliente
CLAVE=$(az functionapp keys list -g rg-cotizador-acero -n <app> --query functionKeys.default -o tsv)
python scripts/cargar_catalogo.py lista_cliente.xlsx --url https://<app>.azurewebsites.net --clave $CLAVE

# 5. Verificar
curl https://<app>.azurewebsites.net/api/salud
```

Si el catálogo está vacío, el sistema carga `data/catalogo_ejemplo.csv` para que la demo funcione desde el minuto uno. **Reemplázalo** por la lista real con `--reemplazar` antes de abrir el canal a clientes.

## CI/CD con GitHub Actions (OIDC, sin contraseñas)

```bash
RG=rg-cotizador-acero
APP=<nombre de la function app>
REPO=<owner>/<repo>
SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)

APP_ID=$(az ad app create --display-name "gh-$APP" --query appId -o tsv)
az ad sp create --id $APP_ID
az role assignment create --assignee $APP_ID --role Contributor \
  --scope /subscriptions/$SUB/resourceGroups/$RG
az ad app federated-credential create --id $APP_ID --parameters "{
  \"name\": \"github-main\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:$REPO:environment:produccion\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

gh secret set AZURE_CLIENT_ID --body $APP_ID
gh secret set AZURE_TENANT_ID --body $TENANT
gh secret set AZURE_SUBSCRIPTION_ID --body $SUB
gh variable set AZURE_FUNCTIONAPP_NAME --body $APP
```

Con eso, cada push a `main`:
1. corre las pruebas (incluida la integración con Azurite),
2. publica el código con build remoto en Flex Consumption,
3. verifica `/api/salud`.

Los cambios de configuración o secretos se aplican volviendo a correr `infra/deploy.sh`. El workflow solo publica código.

## Desarrollo local con el runtime de Functions

```bash
npx azurite --silent --skipApiVersionCheck &     # emulador de Storage
cp local.settings.example.json local.settings.json
func start
```

## Monitoreo

En Application Insights → *Logs*:

```kusto
traces
| where message has "Agente respondió" or message has "Falló el agente"
| project timestamp, message
| order by timestamp desc
```

```kusto
exceptions | summarize count() by type, outerMessage | order by count_ desc
```

Alerta recomendada: más de 3 excepciones en 15 minutos → correo al equipo de DataPrix.

## Endurecimiento para producción (siguiente iteración)

- Secretos en **Key Vault** con referencias `@Microsoft.KeyVault(...)` en los app settings.
- `alwaysReady` = 1 instancia para el procesamiento, si el arranque en frío molesta (tiene costo fijo).
- Retención y respaldo de las tablas de cotizaciones (exportación programada a Blob).
- Restricción de red del Storage (private endpoints) si el cliente lo exige.
