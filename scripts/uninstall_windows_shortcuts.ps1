[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'arXiv Updater.lnk'
$startupLink = Join-Path $shell.SpecialFolders('Startup') 'arXiv Updater Background.lnk'

foreach ($link in @($desktopLink, $startupLink)) {
    if (Test-Path -LiteralPath $link -PathType Leaf) {
        Remove-Item -LiteralPath $link -Force
        Write-Output "Removed shortcut: $link"
    }
}
