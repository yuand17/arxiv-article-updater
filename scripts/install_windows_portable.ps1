param(
    [switch]$NoStartup
)

$ErrorActionPreference = 'Stop'
$candidateRoots = @($PSScriptRoot, (Split-Path -Parent $PSScriptRoot))
$installRoot = $candidateRoots |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ 'arXiv Updater.exe') -PathType Leaf } |
    Select-Object -First 1

if (-not $installRoot) {
    throw 'arXiv Updater.exe was not found next to this installer.'
}

$executable = Join-Path $installRoot 'arXiv Updater.exe'
$shell = New-Object -ComObject WScript.Shell
$desktopLink = Join-Path $shell.SpecialFolders('Desktop') 'arXiv Updater.lnk'
$startupLink = Join-Path $shell.SpecialFolders('Startup') 'arXiv Updater Background.lnk'

function Set-ArxivUpdaterShortcut {
    param(
        [string]$Path,
        [string]$Arguments
    )

    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $executable
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.IconLocation = "$executable,0"
    $shortcut.Description = 'arXiv Updater local research-paper reader'
    $shortcut.Save()
}

Set-ArxivUpdaterShortcut -Path $desktopLink -Arguments '--open'

if ($NoStartup) {
    if (Test-Path -LiteralPath $startupLink -PathType Leaf) {
        Remove-Item -LiteralPath $startupLink -Force
    }
    Write-Output "Login startup shortcut disabled: $startupLink"
}
else {
    Set-ArxivUpdaterShortcut -Path $startupLink -Arguments '--background'
    Write-Output "Created login startup shortcut: $startupLink"
}

Write-Output "Created desktop shortcut: $desktopLink"
