[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $projectRoot 'scripts\launch_arxiv_updater.pyw'
$icon = Join-Path $projectRoot 'src\arxiv_updater\static\icons\arxiv-updater.ico'

foreach ($path in @($pythonw, $launcher, $icon)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file is missing: $path"
    }
}

$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$startup = $shell.SpecialFolders('Startup')

function Set-ArxivUpdaterShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Arguments
    )

    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = "$icon,0"
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Open the local arXiv Updater paper library'
    $shortcut.Save()
}

Set-ArxivUpdaterShortcut -Path (Join-Path $desktop 'arXiv Updater.lnk') -Arguments "`"$launcher`""
Set-ArxivUpdaterShortcut -Path (Join-Path $startup 'arXiv Updater Background.lnk') -Arguments "`"$launcher`" --background"

Write-Output "Created desktop shortcut: $(Join-Path $desktop 'arXiv Updater.lnk')"
Write-Output "Created login startup shortcut: $(Join-Path $startup 'arXiv Updater Background.lnk')"
