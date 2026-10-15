#!/usr/bin/env bash
# Despliega la infraestructura en Azure y (si tienes Azure Functions Core Tools) publica el código.
#
#   az login
#   cp infra/.env.example infra/.env   # y completa los valores
#   ./infra/deploy.sh rg-cotizador-acero eastus2
#
# Volver a correrlo es seguro: actualiza la configuración sin borrar datos.
set -euo pipefail

GRUPO="${1:-rg-cotizador-acero}"
REGION="${2:-eastus2}"
ENV_FILE="${3:-infra/.env}"
NOMBRE="${NOMBRE:-cotizador}"
SECRETOS="ANTHROPIC_API_KEY AZURE_OPENAI_API_KEY WA_TOKEN WA_APP_SECRET WA_VERIFY_TOKEN"

[[ -f "$ENV_FILE" ]] || { echo "No existe $ENV_FILE (copia infra/.env.example)"; exit 1; }
command -v az >/dev/null || { echo "Instala Azure CLI: https://aka.ms/azcli"; exit 1; }

PARAMS="$(mktemp -t cotizador-params-XXXX.json)"
trap 'rm -f "$PARAMS"' EXIT

python3 - "$ENV_FILE" "$PARAMS" "$NOMBRE" "$SECRETOS" <<'PY'
import json, sys
env_file, destino, nombre, secretos = sys.argv[1], sys.argv[2], sys.argv[3], set(sys.argv[4].split())
config, secretas = {}, {}
for linea in open(env_file, encoding="utf-8"):
    linea = linea.strip()
    if not linea or linea.startswith("#") or "=" not in linea:
        continue
    clave, valor = linea.split("=", 1)
    clave, valor = clave.strip(), valor.strip()
    if not valor:
        continue
    (secretas if clave in secretos else config)[clave] = valor
json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
           "contentVersion": "1.0.0.0",
           "parameters": {"nombre": {"value": nombre}, "configuracion": {"value": config},
                          "secretos": {"value": secretas}}}, open(destino, "w"))
print(f"Configuración: {len(config)} valores · Secretos: {', '.join(sorted(secretas)) or 'ninguno'}")
PY

echo "▶ Grupo de recursos $GRUPO en $REGION"
az group create --name "$GRUPO" --location "$REGION" --output none

echo "▶ Desplegando infraestructura (Bicep)…"
SALIDA="$(az deployment group create --resource-group "$GRUPO" --template-file infra/main.bicep \
  --parameters @"$PARAMS" --query properties.outputs --output json)"

APP="$(echo "$SALIDA" | python3 -c 'import json,sys; print(json.load(sys.stdin)["functionAppName"]["value"])')"
WEBHOOK="$(echo "$SALIDA" | python3 -c 'import json,sys; print(json.load(sys.stdin)["webhookUrl"]["value"])')"
SALUD="$(echo "$SALIDA" | python3 -c 'import json,sys; print(json.load(sys.stdin)["saludUrl"]["value"])')"

if command -v func >/dev/null; then
  echo "▶ Publicando código en $APP (build remoto)…"
  func azure functionapp publish "$APP" --python
else
  echo "⚠ Azure Functions Core Tools no está instalado: publica con GitHub Actions o instala 'func'."
fi

cat <<FIN

✅ Listo
   Function App : $APP
   Salud        : $SALUD
   Webhook Meta : $WEBHOOK
   Verify token : el valor de WA_VERIFY_TOKEN en $ENV_FILE

Siguiente paso: en developers.facebook.com > tu app > WhatsApp > Configuración,
pega el webhook y el verify token, y suscribe el campo "messages".
Clave para endpoints de administración:
   az functionapp keys list -g $GRUPO -n $APP --query functionKeys.default -o tsv
FIN
