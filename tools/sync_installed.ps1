# -*- coding: utf-8 -*-
# Keeps the installed addon copies in sync with the repository.
# Usage: powershell -ExecutionPolicy Bypass -File tools\sync_installed.ps1
# Hooked to post-commit so every commit updates the live-test installs.

$ErrorActionPreference = "Stop"

$REPO = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TARGETS = @(
    "$env:APPDATA\Blender Foundation\Blender\5.1\scripts\addons\ADNRNAGNTOOLKIT",
    "$env:APPDATA\Blender Foundation\Blender\5.2\scripts\addons\ADNRNAGNTOOLKIT"
)

$allOk = $true
foreach ($target in $TARGETS) {
    if (-not (Test-Path -LiteralPath $target)) {
        Write-Host "SKIP (not installed): $target"
        continue
    }
    Write-Host "=== Sync -> $target"
    foreach ($file in Get-ChildItem -LiteralPath $REPO -Filter "*.py") {
        $dest = Join-Path $target $file.Name
        if (Test-Path -LiteralPath $dest) {
            $rh = (Get-FileHash $file.FullName).Hash
            $ih = (Get-FileHash $dest).Hash
            if ($rh -eq $ih) {
                Write-Host "  $($file.Name): SAME"
                continue
            }
        }
        Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
        Write-Host "  $($file.Name): COPIED"
    }
    $mismatch = @()
    foreach ($file in Get-ChildItem -LiteralPath $target -Filter "*.py") {
        if (-not (Test-Path -LiteralPath (Join-Path $REPO $file.Name))) {
            $mismatch += $file.Name
        }
    }
    if ($mismatch.Count) {
        Write-Host "  WARNING: files present in install but not in repo: $($mismatch -join ', ')"
    }
}

Write-Host "Done. If Blender is running, reload the addon (Preferences > Add-ons > disable/enable GNToolkit) or restart it."
if ($allOk) { exit 0 } else { exit 1 }
