param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Args
)

# Ensure working dir is the script directory
Set-Location -LiteralPath $PSScriptRoot

# Common uv installation paths
$uvPaths = @(
    "$env:USERPROFILE\.cargo\bin\uv.exe",
    "$env:USERPROFILE\.local\bin\uv.exe",
    "$env:APPDATA\Python\Scripts\uv.exe",
    "$env:LOCALAPPDATA\Programs\uv\uv.exe",
    "C:\Program Files\uv\uv.exe",
    "C:\tools\uv\uv.exe"
)

$uvExe = $uvPaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $uvExe) {
    # Try to find uv via 'where' command (checks PATH)
    $whereResult = where.exe uv 2>$null
    if ($whereResult) {
        $uvExe = $whereResult[0]
    }
}

if (-not $uvExe) {
    Write-Host "Error: uv not found. Please ensure uv is installed and in PATH." -ForegroundColor Red
    Write-Host "Install from: https://docs.astral.sh/uv/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Press any key to exit..." -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

Write-Host "Running: $uvExe run python -m wingman.main $($Args -join ' ')" -ForegroundColor Cyan
Write-Host ""

# Forward args to uv
& $uvExe run python -m wingman.main @Args

Write-Host ""
Write-Host "Press any key to exit..." -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")