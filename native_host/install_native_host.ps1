param(
    [string]$ExtensionId = "cgbbacpgahelimaelidkgmphgcbiclfd"
)

$ErrorActionPreference = "Stop"

$HostDir = $PSScriptRoot
$AppDir = Split-Path -Parent $HostDir
$TemplatePath = Join-Path $HostDir "com.local_translator.bridge.json.template"
$ManifestPath = Join-Path $HostDir "com.local_translator.bridge.json"
$LauncherBatPath = Join-Path $HostDir "launcher.bat"
$PythonwPath = Join-Path $AppDir "venv\Scripts\pythonw.exe"
$PythonPath = Join-Path $AppDir "venv\Scripts\python.exe"
$BridgeServerPath = Join-Path $AppDir "bridge_server.py"

$ExtensionId = $ExtensionId.Trim()
if ($ExtensionId -notmatch "^[a-p]{32}$") {
    Write-Warning "ExtensionId '$ExtensionId' is not a standard 32-char [a-p] Chrome Extension ID, proceeding anyway..."
}

$EscapedLauncherPath = $LauncherBatPath.Replace("\", "\\")
$TemplateContent = Get-Content -Path $TemplatePath -Raw -Encoding UTF8
$ManifestContent = $TemplateContent.Replace("__LAUNCHER_PATH__", $EscapedLauncherPath).Replace("__EXTENSION_ID__", $ExtensionId)
[System.IO.File]::WriteAllText($ManifestPath, $ManifestContent, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[1/3] Da tao file manifest Native Host: $ManifestPath" -ForegroundColor Green

$RegTargets = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.local_translator.bridge",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.local_translator.bridge",
    "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.local_translator.bridge"
)

foreach ($RegKey in $RegTargets) {
    $ParentKey = Split-Path -Parent $RegKey
    if (-not (Test-Path $ParentKey)) {
        New-Item -Path $ParentKey -Force | Out-Null
    }
    if (-not (Test-Path $RegKey)) {
        New-Item -Path $RegKey -Force | Out-Null
    }
    Set-ItemProperty -Path $RegKey -Name "(Default)" -Value $ManifestPath
}

Write-Host "[2/3] Da dang ky Registry HKCU NativeMessagingHosts cho Chrome/Edge/Brave (ExtensionId = $ExtensionId)." -ForegroundColor Green

$ExeToUse = if (Test-Path $PythonwPath) { $PythonwPath } else { $PythonPath }
$TaskName = "LocalTranslatorBridge"

try {
    $Action = New-ScheduledTaskAction -Execute $ExeToUse -Argument "`"$BridgeServerPath`" --idle-exit 900" -WorkingDirectory $AppDir
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Settings $Settings -Force | Out-Null
    Write-Host "[3/3] Da dang ky Scheduled Task du phong '$TaskName' (ngoai Job Object cua Chrome)." -ForegroundColor Green
} catch {
    Write-Warning "[3/3] Bo qua tao Scheduled Task ($_) - Launcher se dung che do Breakaway/Detached truc tiep."
}

Write-Host ""
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "  HOAN TAT CAI DAT NATIVE MESSAGING HOST!" -ForegroundColor Cyan
Write-Host "  Extension ID co dinh : $ExtensionId"
Write-Host "  Manifest             : $ManifestPath"
Write-Host "  Gio day ban chi can mo video tren Chrome va nhan Alt+Shift+T!"
Write-Host "============================================================================" -ForegroundColor Cyan
