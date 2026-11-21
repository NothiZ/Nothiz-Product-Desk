# Save as UTF-8 with BOM for Windows PowerShell 5.1.
param([switch]$SkipZip, [string]$OutputRoot = 'dist')
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskDistRoot = [System.IO.Path]::GetFullPath((Join-Path $taskRoot $OutputRoot))
if (!$taskDistRoot.StartsWith($taskRoot.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output must remain inside the project copy.'
}
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot 'venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $taskPython)) { throw 'Run python -m venv venv, then install requirements-build.txt first.' }
$env:PYINSTALLER_CONFIG_DIR = Join-Path $taskRoot 'build\pyinstaller-cache'
$env:PLAYWRIGHT_SKIP_BROWSER_GC = '1'
$env:PYTHONIOENCODING = 'utf-8'
# Windows PowerShell 5.1 otherwise replaces piped Chinese source with question marks.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskAppName = & $taskPython -c 'from desktop_runtime import APP_TITLE; print(APP_TITLE)'
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($taskAppName)) { throw 'Cannot read the application name.' }
$taskBrowserRoot = Join-Path $taskRoot 'build-resources\browsers'
New-Item -ItemType Directory -Path $taskBrowserRoot -Force | Out-Null
$taskBrowserInfo = & $taskPython -c "import json,pathlib,patchright; p=pathlib.Path(patchright.__file__).parent/'driver/package/browsers.json'; print(json.dumps(json.loads(p.read_text())['browsers']))"
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate Patchright browser metadata.' }
$taskBrowserList = $taskBrowserInfo | ConvertFrom-Json
$taskChromium = $taskBrowserList | Where-Object { $_.name -eq 'chromium' } | Select-Object -First 1
$taskChromiumFolder = 'chromium-' + $taskChromium.revision
$taskChromiumTarget = Join-Path $taskBrowserRoot $taskChromiumFolder
if (!(Test-Path -LiteralPath $taskChromiumTarget)) {
    $taskCachedChromium = Join-Path $env:LOCALAPPDATA ('ms-playwright\' + $taskChromiumFolder)
    if (Test-Path -LiteralPath $taskCachedChromium) {
        Copy-Item -LiteralPath $taskCachedChromium -Destination $taskChromiumTarget -Recurse
    } else {
        $env:PLAYWRIGHT_BROWSERS_PATH = $taskBrowserRoot
        & $taskPython -m patchright install chromium --no-shell --no-remove
        if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed.' }
    }
}
foreach ($taskName in @('ffmpeg', 'winldd')) {
    $taskInfo = $taskBrowserList | Where-Object { $_.name -eq $taskName } | Select-Object -First 1
    if ($taskInfo) {
        $taskFolderName = $taskName + '-' + $taskInfo.revision
        $taskCache = Join-Path $env:LOCALAPPDATA ('ms-playwright\' + $taskFolderName)
        $taskTarget = Join-Path $taskBrowserRoot $taskFolderName
        if ((Test-Path -LiteralPath $taskCache) -and !(Test-Path -LiteralPath $taskTarget)) {
            Copy-Item -LiteralPath $taskCache -Destination $taskTarget -Recurse
        }
    }
}
& $taskPython -m PyInstaller --noconfirm --clean --distpath $taskDistRoot --workpath (Join-Path $taskRoot 'build\pyinstaller') (Join-Path $taskRoot 'desktop.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
$taskRelease = Join-Path $taskDistRoot $taskAppName
Copy-Item -LiteralPath (Join-Path $taskRoot 'DESKTOP_USAGE.txt') -Destination (Join-Path $taskRelease '使用说明.txt') -Force
$env:PD_RELEASE_DIR = $taskRelease
@'
import os
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
p = Path(os.environ['PD_RELEASE_DIR']) / '输入模板.xlsx'
w = Workbook()
s = w.active
s.title = '商品链接'
s.append(['链接'])
s['A1'].font = Font(bold=True, color='FFFFFF')
s['A1'].fill = PatternFill('solid', fgColor='2365D1')
s.column_dimensions['A'].width = 100
s.freeze_panes = 'A2'
w.save(p)
'@ | & $taskPython -
if ($LASTEXITCODE -ne 0) { throw 'Input template creation failed.' }
if (!$SkipZip) {
    Compress-Archive -LiteralPath $taskRelease -DestinationPath (Join-Path $taskDistRoot ($taskAppName + '-win-x64.zip')) -CompressionLevel Optimal -Force
}
Write-Output ('Release: ' + $taskRelease)
