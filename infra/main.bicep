/*
  Cotizador IA para distribuidores de acero — infraestructura serverless en Azure.

  Crea:
  - Function App en plan Flex Consumption (Linux, Python) — pago por uso
  - Storage Account: Table (datos), Blob (PDFs), Queue (mensajes) — sin claves compartidas
  - Identidad administrada con permisos mínimos sobre el Storage
  - Application Insights + Log Analytics para monitoreo

  Basado en la plantilla oficial "function-app-flex-managed-identities" de Microsoft.
*/

@description('Región. Para Perú suele convenir eastus2 o brazilsouth (verifica soporte de Flex Consumption).')
param location string = resourceGroup().location

@description('Prefijo corto para nombrar recursos (solo minúsculas y números).')
@minLength(3)
@maxLength(12)
param nombre string = 'cotizador'

@description('Versión de Python del runtime.')
@allowed(['3.11', '3.12'])
param pythonVersion string = '3.12'

@description('Máximo de instancias en paralelo (Flex Consumption exige mínimo 40).')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@description('Memoria por instancia.')
@allowed([2048, 4096])
param instanceMemoryMB int = 2048

@description('Prefijo de tablas (permite varios clientes en una misma cuenta de Storage).')
param prefijoTablas string = ''

@description('App settings no secretos: EMPRESA_*, MODO_ENVIO, ADMIN_NUMEROS, LLM_PROVEEDOR, etc.')
param configuracion object = {}

@description('App settings secretos: ANTHROPIC_API_KEY, WA_TOKEN, WA_APP_SECRET, WA_VERIFY_TOKEN, AZURE_OPENAI_API_KEY.')
@secure()
param secretos object = {}

param tags object = {
  proyecto: 'cotizador-acero'
  proveedor: 'DataPrix'
}

var token = toLower(uniqueString(subscription().id, resourceGroup().id, nombre))
var appName = 'func-${nombre}-${take(token, 6)}'
var storageName = take('st${nombre}${token}', 24)
var deploymentContainer = 'app-package-${take(token, 8)}'
var tablas = ['procesados', 'pendientes', 'control', 'conversaciones', 'productos', 'cotizaciones', 'config', 'contadores']

var roles = {
  blobOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  blobContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  queueContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  tableContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  metricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

// ------------------------------------------------------------------ monitoreo
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${nombre}-${take(token, 6)}'
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${nombre}-${take(token, 6)}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    DisableLocalAuth: true
  }
}

// ------------------------------------------------------------------ almacenamiento
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }

  resource blobServices 'blobServices' = {
    name: 'default'
    properties: {
      deleteRetentionPolicy: {
        enabled: true
        days: 7
      }
    }
    resource paquetes 'containers' = {
      name: deploymentContainer
      properties: {
        publicAccess: 'None'
      }
    }
    resource pdfs 'containers' = {
      name: 'cotizaciones'
      properties: {
        publicAccess: 'None'
      }
    }
  }

  resource queueServices 'queueServices' = {
    name: 'default'
    resource cola 'queues' = {
      name: 'mensajes-entrantes'
    }
  }

  resource tableServices 'tableServices' = {
    name: 'default'
  }
}

resource tablasStorage 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = [for t in tablas: {
  parent: storage::tableServices
  name: '${prefijoTablas}${t}'
}]

// ------------------------------------------------------------------ identidad y permisos
resource identidad 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${nombre}-${take(token, 6)}'
  location: location
  tags: tags
}

var asignacionesStorage = [
  roles.blobOwner
  roles.blobContributor
  roles.queueContributor
  roles.tableContributor
]

resource permisosStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for rol in asignacionesStorage: {
  name: guid(storage.id, identidad.id, rol)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', rol)
    principalId: identidad.properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

resource permisoMetricas 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, identidad.id, roles.metricsPublisher)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.metricsPublisher)
    principalId: identidad.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ------------------------------------------------------------------ Function App (Flex Consumption)
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${nombre}-${take(token, 6)}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

var settingsBase = {
  AzureWebJobsStorage__accountName: storage.name
  AzureWebJobsStorage__credential: 'managedidentity'
  AzureWebJobsStorage__clientId: identidad.properties.clientId
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'ClientId=${identidad.properties.clientId};Authorization=AAD'
  AZURE_CLIENT_ID: identidad.properties.clientId
  ALMACEN: 'azure'
  STORAGE_ACCOUNT_NAME: storage.name
  PREFIJO_TABLAS: prefijoTablas
  ZONA_HORARIA: 'America/Lima'
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: appName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identidad.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identidad.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
  }

  resource appSettings 'config' = {
    name: 'appsettings'
    properties: union(settingsBase, configuracion, secretos)
  }

  dependsOn: [
    permisosStorage
  ]
}

output functionAppName string = functionApp.name
output storageAccountName string = storage.name
output webhookUrl string = 'https://${functionApp.properties.defaultHostName}/api/whatsapp/webhook'
output saludUrl string = 'https://${functionApp.properties.defaultHostName}/api/salud'
