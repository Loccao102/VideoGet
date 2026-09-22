param(
    [int]$Port = 9222,
    [string]$BindAddress = "127.0.0.1",
    [string]$ProfileDir = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProfileDir)) {
    $ProfileDir = Join-Path (Split-Path $PSScriptRoot -Parent) "downloads\douyin-chrome-profile"
}

$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $chromeCandidates -or $chromeCandidates.Count -eq 0) {
    throw "Chrome/Edge was not found. Install Chrome/Edge or edit the candidate paths."
}

$browser = $chromeCandidates[0]
New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

if ($BindAddress -ne "127.0.0.1" -and $BindAddress -ne "localhost") {
    Write-Warning "DevTools remote debugging gives full control of this dedicated browser. Keep port $Port blocked from untrusted networks."
}

$args = @(
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=$BindAddress",
    "--remote-allow-origins=*",
    "--user-data-dir=$ProfileDir",
    "--no-first-run",
    "--no-default-browser-check",
    "https://www.douyin.com/"
)

Write-Host "Starting dedicated Douyin browser:"
Write-Host "  Browser : $browser"
Write-Host "  Profile : $ProfileDir"
Write-Host "  CDP     : http://$BindAddress`:$Port"
Write-Host ""
Write-Host "Log in to Douyin in this window and keep it running while VideoGet searches."
Write-Host ""
Write-Host "Direct Windows VideoGet: DOUYIN_CDP_URL=http://127.0.0.1:$Port"
Write-Host "Docker Desktop: try DOUYIN_CDP_URL=http://host.docker.internal:$Port"
Write-Host "If Docker cannot reach a loopback-bound port, rerun with a Docker-reachable bind address."
Write-Host ""

Start-Process -FilePath $browser -ArgumentList $args
