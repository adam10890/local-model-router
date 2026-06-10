[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:9000",
    [string]$ApiKey = $env:A0_LMM_ROUTER_API_KEY
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")

$Headers = @{}
if ($ApiKey) {
    $Headers["Authorization"] = "Bearer $ApiKey"
}

function Invoke-SmokeRequest {
    param(
        [string]$Name,
        [string]$Method,
        [string]$Path,
        [object]$Body = $null,
        [int[]]$AllowedStatus = @(200)
    )

    $uri = "$BaseUrl$Path"
    $params = @{
        Method = $Method
        Uri = $uri
        Headers = $Headers
        UseBasicParsing = $true
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $params["Body"] = ($Body | ConvertTo-Json -Depth 10)
        $params["ContentType"] = "application/json"
    }

    try {
        $response = Invoke-WebRequest @params
        $status = [int]$response.StatusCode
        $content = $response.Content
    }
    catch {
        if ($null -eq $_.Exception.Response) {
            throw "$Name failed before receiving an HTTP response: $($_.Exception.Message)"
        }
        $status = [int]$_.Exception.Response.StatusCode
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $content = $reader.ReadToEnd()
    }

    if ($AllowedStatus -notcontains $status) {
        throw "$Name failed with HTTP $status. Body: $content"
    }

    Write-Host "[OK] $Name -> HTTP $status"
}

Invoke-SmokeRequest -Name "health" -Method GET -Path "/health"

Invoke-SmokeRequest -Name "fleet status" -Method GET -Path "/fleet/status"

Invoke-SmokeRequest `
    -Name "routing request" `
    -Method POST `
    -Path "/routing/request" `
    -AllowedStatus @(200) `
    -Body @{
        agent_id = "phase9-smoke"
        agent_type = "smoke"
        role = "chat"
        task_type = "smoke"
        privacy_mode = "local"
        local_only = $true
        cloud_allowed = $false
    }

Invoke-SmokeRequest `
    -Name "chat completions" `
    -Method POST `
    -Path "/v1/chat/completions" `
    -AllowedStatus @(200, 503) `
    -Body @{
        model = "local-chat"
        stream = $false
        messages = @(@{ role = "user"; content = "phase9 smoke test" })
    }

Write-Host "Smoke complete."
