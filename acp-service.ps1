[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",

    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.yaml"),
    [string]$BridgePath = (Join-Path $PSScriptRoot "bridge\target\release\deerflow-acp.exe"),
    [string]$PythonPath,
    [string]$RuntimeDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# Keep native stderr/exit codes capturable even when the caller enabled the
# PowerShell 7 native-command error preference feature.
$PSNativeCommandUseErrorActionPreference = $false

function Resolve-ProjectPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $Path))
}

$ConfigPath = Resolve-ProjectPath $ConfigPath
$BridgePath = Resolve-ProjectPath $BridgePath
if ($PythonPath) {
    $PythonPath = Resolve-ProjectPath $PythonPath
}
if ($RuntimeDir) {
    $RuntimeDir = Resolve-ProjectPath $RuntimeDir
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "DeerFlow configuration was not found: $ConfigPath"
}
if (-not (Test-Path -LiteralPath $BridgePath -PathType Leaf)) {
    throw "Native ACP bridge was not found: $BridgePath. Run: cargo build --release --manifest-path `"$PSScriptRoot\bridge\Cargo.toml`""
}
if ($PythonPath -and -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python interpreter was not found: $PythonPath"
}

function Invoke-AcpBridge {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Mode
    )

    $commandArguments = [System.Collections.Generic.List[string]]::new()
    $commandArguments.Add($Mode)
    $commandArguments.Add("--config")
    $commandArguments.Add($ConfigPath)
    if ($PythonPath) {
        $commandArguments.Add("--python")
        $commandArguments.Add($PythonPath)
    }
    if ($RuntimeDir) {
        $commandArguments.Add("--runtime-dir")
        $commandArguments.Add($RuntimeDir)
    }

    Push-Location $PSScriptRoot
    try {
        $output = @(& $BridgePath @commandArguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $outputText = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    return [PSCustomObject]@{
        ExitCode = $exitCode
        Output = $outputText.Trim()
    }
}

function Assert-AcpCommandSucceeded {
    param(
        [Parameter(Mandatory = $true)]
        [PSCustomObject]$Result,
        [Parameter(Mandatory = $true)]
        [string]$Operation
    )

    if ($Result.ExitCode -ne 0) {
        $details = if ($Result.Output) { "`n$($Result.Output)" } else { "" }
        throw "Failed to $Operation DeerFlow ACP service (exit code $($Result.ExitCode)).$details"
    }
    if ($Result.Output) {
        Write-Output $Result.Output
    }
}

function Start-AcpService {
    Write-Host "Starting DeerFlow ACP service..."
    $result = Invoke-AcpBridge "--start-daemon"
    Assert-AcpCommandSucceeded $result "start"
}

function Stop-AcpService {
    Write-Host "Stopping DeerFlow ACP service..."
    $result = Invoke-AcpBridge "--stop-daemon"
    if ($result.ExitCode -ne 0 -and $result.Output -match "not running") {
        Write-Output "DeerFlow ACP service is already stopped."
        $global:LASTEXITCODE = 0
        return
    }

    Assert-AcpCommandSucceeded $result "stop"
}

switch ($Action) {
    "start" {
        Start-AcpService
    }
    "stop" {
        Stop-AcpService
    }
    "restart" {
        Stop-AcpService
        Start-AcpService
    }
    "status" {
        $result = Invoke-AcpBridge "--status"
        Assert-AcpCommandSucceeded $result "query"
    }
}
