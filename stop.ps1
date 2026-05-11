param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Get-ConfigPort {
    if ($Port -gt 0) {
        return $Port
    }

    if ($env:PORT) {
        $parsed = 0
        if ([int]::TryParse($env:PORT, [ref]$parsed) -and $parsed -gt 0) {
            return $parsed
        }
    }

    if (Test-Path -LiteralPath ".\config.yaml") {
        try {
            $output = & uv run python .\scripts\read_api_config.py port 8000 2>$null
            $parsed = 0
            if ([int]::TryParse(($output | Select-Object -First 1), [ref]$parsed) -and $parsed -gt 0) {
                return $parsed
            }
        } catch {
            return 8000
        }
    }

    return 8000
}

$appPort = Get-ConfigPort
$connections = Get-NetTCPConnection -LocalPort $appPort -State Listen -ErrorAction SilentlyContinue

if (-not $connections) {
    Write-Host "No DeerFlow API process listening on port $appPort."
    exit 0
}

$processIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
Write-Host "Stopping DeerFlow API process(es) on port ${appPort}: $($processIds -join ', ')"

foreach ($processId in $processIds) {
    try {
        Stop-Process -Id $processId -ErrorAction Stop
    } catch {
        Write-Warning "Failed to stop process ${processId}: $($_.Exception.Message)"
    }
}

Start-Sleep -Seconds 1
$remaining = Get-NetTCPConnection -LocalPort $appPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique

if ($remaining) {
    Write-Warning "Process(es) still listening on port ${appPort}: $($remaining -join ', ')"
    exit 1
}

Write-Host "DeerFlow API stopped."
