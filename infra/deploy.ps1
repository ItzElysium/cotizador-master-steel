<#
.SYNOPSIS
  Despliega el cotizador en Azure desde Windows (PowerShell). No necesita Python, Git Bash
  ni Azure Functions Core Tools: solo Azure CLI.

.EXAMPLE
  # Desde la carpeta del repositorio:
  az login
  Copy-Item infra\.env.example infra\.env
  notepad infra\.env
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -Grupo rg-cotizador-acero -Region eastus

  # Solo actualizar el código (la infraestructura ya existe):
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -Grupo rg-cotizador-acero -SoloCodigo
#>
param(
    [string]$Grupo = "rg-cotizador-acero",
    [string]$Region = "eastus",
    [string]$EnvFile = "infra\.env",
    [string]$Nombre = "cotizador",
    [switch]$SoloCodigo
)

$ErrorActionPreference = "Stop"
$Secretos = @("ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "LLM_API_KEY",
    "WA_TOKEN", "WA_APP_SECRET", "WA_VERIFY_TOKEN")
$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Raiz

function Invoke-Az {
    # Ejecuta Azure CLI y se detiene si falla.
    $salida = & az @args
    if ($LASTEXITCODE -ne 0) { throw "Falló: az $($args -join ' ')" }
    return $salida
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Instala Azure CLI: https://aka.ms/azcli" }

if (-not $SoloCodigo) {
    if (-not (Test-Path $EnvFile)) { throw "No existe $EnvFile (copia infra\.env.example y complétalo)" }

    # ---------------------------------------------------------------- 1. Leer configuración
    $config = [ordered]@{}
    $secretas = [ordered]@{}
    foreach ($linea in Get-Content -Path $EnvFile -Encoding UTF8) {
        $l = $linea.Trim()
        if (-not $l -or $l.StartsWith("#") -or -not $l.Contains("=")) { continue }
        $i = $l.IndexOf("=")
        $clave = $l.Substring(0, $i).Trim()
        $valor = $l.Substring($i + 1).Trim()
        if (-not $valor) { continue }
        if ($Secretos -contains $clave) { $secretas[$clave] = $valor } else { $config[$clave] = $valor }
    }
    $nombresSecretos = if ($secretas.Count) { ($secretas.Keys | Sort-Object) -join ", " } else { "ninguno" }
    Write-Host "Configuración: $($config.Count) valores · Secretos: $nombresSecretos"

    $parametros = [ordered]@{
        '$schema'      = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
        contentVersion = "1.0.0.0"
        parameters     = [ordered]@{
            nombre        = @{ value = $Nombre }
            configuracion = @{ value = $config }
            secretos      = @{ value = $secretas }
        }
    }
    $archivoParametros = Join-Path ([System.IO.Path]::GetTempPath()) "cotizador-params-$PID.json"
    $json = $parametros | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($archivoParametros, $json, (New-Object System.Text.UTF8Encoding($false)))

    try {
        # ------------------------------------------------------------ 2. Infraestructura
        Write-Host ">> Grupo de recursos $Grupo en $Region"
        Invoke-Az group create --name $Grupo --location $Region --output none | Out-Null

        Write-Host ">> Desplegando infraestructura (Bicep)… puede tardar unos minutos"
        $salida = Invoke-Az deployment group create --resource-group $Grupo --template-file "infra\main.bicep" `
            --parameters "@$archivoParametros" --query properties.outputs --output json
        $resultado = ($salida -join "`n") | ConvertFrom-Json
        $App = $resultado.functionAppName.value
        $Webhook = $resultado.webhookUrl.value
        $Salud = $resultado.saludUrl.value
    }
    finally {
        Remove-Item $archivoParametros -Force -ErrorAction SilentlyContinue
    }
}
else {
    $App = Invoke-Az functionapp list --resource-group $Grupo --query "[0].name" --output tsv | Select-Object -First 1
    if (-not $App) { throw "No hay una Function App en el grupo $Grupo" }
    $App = $App.Trim()
    $Webhook = "https://$App.azurewebsites.net/api/whatsapp/webhook"
    $Salud = "https://$App.azurewebsites.net/api/salud"
}

# -------------------------------------------------------------------- 3. Paquete de código
Write-Host ">> Empaquetando el código"
$excluirCarpetas = @(".git", ".github", ".vscode", ".venv", "venv", "__pycache__", ".pytest_cache",
    "tests", "docs", "infra", "scripts", "salida")
$excluirArchivos = @("local.settings.json", "local.settings.example.json", "requirements-dev.txt",
    "plantilla_lista_precios.xlsx")
$excluirExtensiones = @(".md", ".pyc", ".bundle", ".zip")

$zip = Join-Path ([System.IO.Path]::GetTempPath()) "cotizador-paquete-$PID.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archivoZip = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
$incluidos = 0
try {
    foreach ($f in Get-ChildItem -Path $Raiz -Recurse -File -Force) {
        $relativa = $f.FullName.Substring($Raiz.Length).TrimStart('\', '/')
        $partes = $relativa -split '[\\/]'
        $carpetas = $partes[0..([Math]::Max(0, $partes.Length - 2))]
        if ($partes.Length -gt 1 -and ($carpetas | Where-Object { $excluirCarpetas -contains $_ })) { continue }
        if ($excluirArchivos -contains $f.Name) { continue }
        if ($excluirExtensiones -contains $f.Extension.ToLower()) { continue }
        # Rutas con "/" para que Linux (Azure) las entienda
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archivoZip, $f.FullName,
            ($partes -join "/")) | Out-Null
        $incluidos++
    }
}
finally {
    $archivoZip.Dispose()
}
Write-Host "  $incluidos archivos"

# -------------------------------------------------------------------- 4. Publicar (build remoto)
Write-Host ">> Publicando en $App (instala dependencias en Azure)…"
try {
    Invoke-Az functionapp deployment source config-zip --resource-group $Grupo --name $App --src $zip `
        --build-remote true --output none | Out-Null
}
finally {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "LISTO"
Write-Host "   Function App : $App"
Write-Host "   Salud        : $Salud"
Write-Host "   Webhook Meta : $Webhook"
Write-Host "   Verify token : el valor de WA_VERIFY_TOKEN en $EnvFile"
Write-Host ""
Write-Host "Clave para los endpoints de administración:"
Write-Host "   az functionapp keys list -g $Grupo -n $App --query functionKeys.default -o tsv"
