param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$configPath = Join-Path $PSScriptRoot "adapter.toml"

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    Write-Error "adapter.toml was not found at: $configPath"
    exit 1
}

$configText = Get-Content -LiteralPath $configPath -Raw
$wakeSection = [regex]::Match(
    $configText,
    '(?ms)^\s*\[wake\]\s*(.*?)(?=^\s*\[|\z)'
)

if (-not $wakeSection.Success) {
    Write-Error "The [wake] section was not found in adapter.toml."
    exit 1
}

$portSetting = [regex]::Match(
    $wakeSection.Groups[1].Value,
    '(?m)^\s*port\s*=\s*(\d+)\s*(?:#.*)?$'
)

if (-not $portSetting.Success) {
    Write-Error "wake.port was not found in adapter.toml."
    exit 1
}

$wakePort = [int]$portSetting.Groups[1].Value
if ($wakePort -lt 1 -or $wakePort -gt 65535) {
    Write-Error "wake.port must be between 1 and 65535 for stop.bat."
    exit 1
}

$listeners = @(
    Get-NetTCPConnection `
        -LocalAddress "127.0.0.1" `
        -LocalPort $wakePort `
        -State Listen `
        -ErrorAction SilentlyContinue
)

# Get-NetTCPConnection can return no rows in restricted/non-elevated shells
# even though the listener exists. Fall back to netstat so stop.bat does not
# incorrectly claim that the adapter is offline and leave a stale bridge alive.
if ($listeners.Count -eq 0) {
    $endpointPattern = "^\s*TCP\s+127\.0\.0\.1:$wakePort\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $fallbackOwnerIds = @(
        & netstat.exe -ano -p tcp |
            ForEach-Object {
                if ($_ -match $endpointPattern) {
                    [int]$Matches[1]
                }
            } |
            Select-Object -Unique
    )
    $listeners = @(
        $fallbackOwnerIds | ForEach-Object {
            [pscustomobject]@{ OwningProcess = $_ }
        }
    )
}

if ($listeners.Count -eq 0) {
    Write-Host "Raft-DeerFlow adapter is not running on 127.0.0.1:$wakePort."
    exit 0
}

$ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
if ($ownerIds.Count -ne 1) {
    Write-Error "Expected one listener owner on 127.0.0.1:$wakePort, found $($ownerIds.Count)."
    exit 1
}

$ownerId = [int]$ownerIds[0]
$adapterRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$pidPath = Join-Path $PSScriptRoot "data\adapter.pid"
$pidMatches = $false
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $pidMatches = $pidText -match '^\d+$' -and [int]$pidText -eq $ownerId
}

$belongsToAdapter = $pidMatches
if (-not $belongsToAdapter) {
    # Compatibility path for adapters started before PID-file support. CIM may
    # require elevation, so future starts rely on the verified PID file above.
    $process = $null
    try {
        $process = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $ownerId" `
            -ErrorAction Stop
    }
    catch {
        Write-Error "Cannot verify PID $ownerId. This appears to be a pre-PID-file adapter; restart it once from an elevated shell, then stop.bat will work without CIM access."
        exit 1
    }
    if ($null -eq $process) {
        Write-Error "Listener process $ownerId disappeared before it could be verified."
        exit 1
    }
    $commandLine = [string]$process.CommandLine
    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    $belongsToAdapter =
        $commandLine.IndexOf($adapterRoot, $comparison) -ge 0 -and
        $commandLine.IndexOf("raft-deerflow-adapter", $comparison) -ge 0
}

if (-not $belongsToAdapter) {
    Write-Error "Refusing to stop PID $ownerId because its command line is not this adapter."
    exit 1
}

if ($DryRun) {
    Write-Host "Dry run: would stop Raft-DeerFlow adapter PID $ownerId and its child processes."
    exit 0
}

Write-Host "Stopping Raft-DeerFlow adapter PID $ownerId..."
& taskkill.exe /PID $ownerId /T /F
if ($LASTEXITCODE -ne 0) {
    Write-Error "taskkill failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    Remove-Item -LiteralPath $pidPath -Force
}

Write-Host "Raft-DeerFlow adapter stopped."
