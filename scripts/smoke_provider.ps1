[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:9000",
    [string]$ApiKey = $env:A0_LMM_ROUTER_API_KEY,
    [string]$JsonOutput = "",
    [switch]$RequireLive,
    [int]$TimeoutSec = 60
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")
$Headers = @{}
if ($ApiKey) { $Headers["Authorization"] = "Bearer $ApiKey" }
$Checks = [Collections.Generic.List[object]]::new()
$Failed = $false
$Live = $true

function Add-SmokeCheck([string]$Name, [string]$Status, [int]$HttpStatus = 0, [string]$ErrorCode = "") {
    $row = [ordered]@{ name = $Name; status = $Status }
    if ($HttpStatus) { $row.http_status = $HttpStatus }
    if ($ErrorCode) { $row.error_code = $ErrorCode }
    $Checks.Add([pscustomobject]$row)
}

function Invoke-SmokeRequest {
    param(
        [string]$Name,
        [string]$Method,
        [string]$Path,
        [object]$Body = $null,
        [int[]]$AllowedStatus = @(200),
        [switch]$Json,
        [switch]$RequireNonEmpty
    )
    $params = @{
        Method = $Method
        Uri = "$BaseUrl$Path"
        Headers = $Headers
        UseBasicParsing = $true
        TimeoutSec = $TimeoutSec
    }
    if ($null -ne $Body) {
        $params.Body = $Body | ConvertTo-Json -Depth 10
        $params.ContentType = "application/json"
    }
    try {
        $response = Invoke-WebRequest @params
        $status = [int]$response.StatusCode
    } catch {
        $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        if ($AllowedStatus -notcontains $status) {
            Add-SmokeCheck $Name "fail" $status $(if ($status) { "http_$status" } else { "request_failed" })
            throw "provider_smoke_failed"
        }
        $response = $_.Exception.Response
    }
    if ($AllowedStatus -notcontains $status) {
        Add-SmokeCheck $Name "fail" $status "http_$status"
        throw "provider_smoke_failed"
    }
    if ($status -eq 503) {
        $script:Live = $false
        Add-SmokeCheck $Name "unavailable" $status "model_unavailable"
        Write-Host "[UNAVAILABLE] $Name -> HTTP 503"
        return $null
    }
    $content = [string]$response.Content
    if ($RequireNonEmpty -and -not $content) {
        Add-SmokeCheck $Name "fail" $status "empty_response"
        throw "provider_smoke_failed"
    }
    $payload = $null
    if ($Json) {
        try { $payload = $content | ConvertFrom-Json } catch {
            Add-SmokeCheck $Name "fail" $status "invalid_json"
            throw "provider_smoke_failed"
        }
    }
    Add-SmokeCheck $Name "pass" $status
    Write-Host "[OK] $Name -> HTTP $status"
    return $payload
}

try {
    $health = Invoke-SmokeRequest "health" GET "/health" -Json
    if ($health.service -ne "lmm-router-observer") { throw "invalid_health_identity" }
    Invoke-SmokeRequest "models" GET "/v1/models" -Json | Out-Null
    Invoke-SmokeRequest "fleet" GET "/fleet/status" -Json | Out-Null
    Invoke-SmokeRequest "route" POST "/routing/request" -Json -Body @{
        agent_id = "provider-smoke"
        agent_type = "smoke"
        role = "chat"
        task_type = "smoke"
        local_only = $true
    } | Out-Null
    $liveStatus = if ($RequireLive) { @(200) } else { @(200, 503) }
    $chat = Invoke-SmokeRequest "chat" POST "/v1/chat/completions" -Json -AllowedStatus $liveStatus -Body @{
        model = "local-chat"
        stream = $false
        messages = @(@{ role = "user"; content = "Reply with exactly: OK" })
        max_tokens = 16
    }
    if ($chat -and -not @($chat.choices).Count) { throw "invalid_chat_response" }
    Invoke-SmokeRequest "stream" POST "/v1/chat/completions" -RequireNonEmpty -AllowedStatus $liveStatus -Body @{
        model = "local-chat"
        stream = $true
        messages = @(@{ role = "user"; content = "Reply with exactly: OK" })
        max_tokens = 16
    } | Out-Null
    $tools = Invoke-SmokeRequest "tools" POST "/v1/chat/completions" -Json -AllowedStatus $liveStatus -Body @{
        model = "local-chat"
        stream = $false
        messages = @(@{ role = "user"; content = "Call imperium_ping now." })
        tools = @(@{
            type = "function"
            function = @{
                name = "imperium_ping"
                description = "Return a smoke verification ping"
                parameters = @{ type = "object"; properties = @{} }
            }
        })
        tool_choice = "required"
        chat_template_kwargs = @{ enable_thinking = $false }
        temperature = 0
        max_tokens = 64
    }
    if ($tools) {
        $calls = @($tools.choices[0].message.tool_calls)
        if (-not @($calls | Where-Object { $_.function.name -eq "imperium_ping" }).Count) {
            throw "tool_call_not_returned"
        }
    }
} catch {
    $Failed = $true
    if (-not @($Checks | Where-Object { $_.status -eq "fail" }).Count) {
        Add-SmokeCheck "validation" "fail" 0 ([string]$_).Trim()
    }
}

$Report = [ordered]@{
    schema_version = 1
    kind = "provider_smoke"
    ok = -not $Failed
    require_live = [bool]$RequireLive
    live = $Live -and -not $Failed
    checks = $Checks
}
$Json = $Report | ConvertTo-Json -Depth 8
if ($JsonOutput) {
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($JsonOutput))
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $Json | Set-Content -LiteralPath $JsonOutput -Encoding UTF8
}
$Json
if ($Failed) { throw "provider_smoke_failed" }
