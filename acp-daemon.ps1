<#
.SYNOPSIS
Starts and manages the detached DeerFlow ACP daemon.

.DESCRIPTION
This is a convenience entry point over acp-service.ps1. The default action is
`start`. The native Bridge launches the Python daemon without a window, with
null stdio and in a separate process group, so this script returns after the
daemon is ready and the daemon keeps running after PowerShell closes.

No Windows service or scheduled task is created.

.EXAMPLE
.\acp-daemon.ps1
.\acp-daemon.ps1 status
.\acp-daemon.ps1 restart
.\acp-daemon.ps1 stop
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "start",

    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.yaml"),
    [string]$BridgePath = (Join-Path $PSScriptRoot "bridge\target\release\deerflow-acp.exe"),
    [string]$PythonPath,
    [string]$RuntimeDir,
    [string]$ServiceScriptPath = (Join-Path $PSScriptRoot "acp-service.ps1")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

$ServiceScriptPath = Resolve-ProjectPath $ServiceScriptPath
if (-not (Test-Path -LiteralPath $ServiceScriptPath -PathType Leaf)) {
    throw "ACP service control script was not found: $ServiceScriptPath"
}

$parameters = @{
    Action = $Action
    ConfigPath = $ConfigPath
    BridgePath = $BridgePath
}
if ($PythonPath) {
    $parameters.PythonPath = $PythonPath
}
if ($RuntimeDir) {
    $parameters.RuntimeDir = $RuntimeDir
}

& $ServiceScriptPath @parameters
