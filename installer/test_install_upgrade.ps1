param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [string]$WorkRoot = (Join-Path $PSScriptRoot '..\.build-tests')
)

# Only use an installer compiled with /DInstallerTestMode=1. This script never
# runs the normal UI, creates no shortcuts and registers no uninstaller.
$ErrorActionPreference = 'Stop'
$setupFile = (Resolve-Path -LiteralPath $SetupPath).Path
if ([IO.Path]::GetFileName($setupFile) -notlike '*-test.exe') {
    throw 'An InstallerTestMode build (*-test.exe) is required.'
}
$workPath = [IO.Path]::GetFullPath($WorkRoot)
$runPath = Join-Path $workPath ('installer-' + [guid]::NewGuid().ToString('N'))
$legacyPath = Join-Path $runPath 'legacy'
$freshProfilePath = Join-Path $runPath 'fresh-profile'
$freshAppPath = Join-Path $runPath 'fresh-install'
$profilePath = Join-Path $runPath 'profile'
$appPath = Join-Path $runPath 'installed'
New-Item -ItemType Directory -Path (Join-Path $legacyPath 'data') -Force | Out-Null

$utf8 = New-Object Text.UTF8Encoding($false)
$legacyConfig = Join-Path $legacyPath 'config.json'
$legacyProbe = Join-Path $legacyPath 'data\upgrade-proof.txt'
[IO.File]::WriteAllText($legacyConfig,
    '{"island":{"theme":"light"},"translate":{"api_key":"test-placeholder-only"}}', $utf8)
[IO.File]::WriteAllText($legacyProbe, 'legacy-data-snapshot', $utf8)
[IO.File]::WriteAllText((Join-Path $legacyPath 'data\clipboard.json'), '{"items":[]}', $utf8)
$sourceHash = (Get-FileHash -LiteralPath $legacyConfig -Algorithm SHA256).Hash
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{91E9797E-876F-44D0-A993-629B3E52A9AF}_is1'
$uninstallExisted = Test-Path -LiteralPath $uninstallKey -ErrorAction SilentlyContinue
$previousHome = [Environment]::GetEnvironmentVariable('AISLAND_USER_HOME', 'Process')
$previousCache = [Environment]::GetEnvironmentVariable('AISLAND_SETUP_CACHE', 'Process')

function Invoke-TestInstall(
    [string]$Pass,
    [string]$Installer = $setupFile,
    [string]$Target = $appPath,
    [string]$MigrationSource
) {
    $logPath = Join-Path $runPath ('install-' + $Pass + '.log')
    $arguments = @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NORUN=1',
        ('/DIR="' + $Target + '"'),
        ('/LOG="' + $logPath + '"')
    )
    if ($MigrationSource) {
        $arguments += ('/MIGRATEFROM="' + $MigrationSource + '"')
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Installer
    $startInfo.Arguments = $arguments -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw ('Installation pass {0} failed with exit code {1}. Log: {2}' -f $Pass, $process.ExitCode, $logPath)
    }
}

try {
    $env:AISLAND_SETUP_CACHE = Join-Path $runPath 'setup-cache'

    # Put the installer next to a valid legacy source tree.
    New-Item -ItemType Directory -Path (Join-Path $legacyPath 'aisland') | Out-Null
    [IO.File]::WriteAllText((Join-Path $legacyPath 'run.pyw'), '', $utf8)
    [IO.File]::WriteAllText((Join-Path $legacyPath 'aisland\app.py'), '', $utf8)
    $nearbySetup = Join-Path $legacyPath ([IO.Path]::GetFileName($setupFile))
    Copy-Item -LiteralPath $setupFile -Destination $nearbySetup
    $env:AISLAND_USER_HOME = $freshProfilePath
    Invoke-TestInstall -Pass fresh -Installer $nearbySetup -Target $freshAppPath
    if (-not (Test-Path -LiteralPath (Join-Path $freshAppPath 'Aisland.exe'))) {
        throw 'The fresh installation executable is missing.'
    }
    if ((Test-Path -LiteralPath (Join-Path $freshProfilePath 'config.json')) -or
        (Test-Path -LiteralPath (Join-Path $freshProfilePath 'data\upgrade-proof.txt'))) {
        throw 'Fresh installation imported nearby legacy data without an explicit request.'
    }

    $env:AISLAND_USER_HOME = $profilePath
    $oldRuntime = Join-Path $appPath '_internal'
    New-Item -ItemType Directory -Path $oldRuntime -Force | Out-Null
    $oldExe = Join-Path $appPath 'Wisland.exe'
    $oldMarker = Join-Path $appPath 'wisland-install.marker'
    $oldRuntimeFile = Join-Path $oldRuntime 'legacy-runtime-probe.txt'
    [IO.File]::WriteAllText($oldExe, 'legacy-executable-placeholder', $utf8)
    [IO.File]::WriteAllText($oldMarker, '91E9797E-876F-44D0-A993-629B3E52A9AF', $utf8)
    [IO.File]::WriteAllText($oldRuntimeFile, 'legacy-runtime-placeholder', $utf8)
    Invoke-TestInstall -Pass import -MigrationSource $legacyPath
    if ((Test-Path -LiteralPath $oldExe) -or (Test-Path -LiteralPath $oldMarker) -or
        (Test-Path -LiteralPath $oldRuntimeFile)) {
        throw 'Upgrade did not replace the legacy branded executable and runtime.'
    }
    $targetConfig = Join-Path $profilePath 'config.json'
    $targetProbe = Join-Path $profilePath 'data\upgrade-proof.txt'
    if (-not (Test-Path -LiteralPath (Join-Path $appPath 'Aisland.exe'))) {
        throw 'The installed executable is missing.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $appPath 'aisland-install.marker'))) {
        throw 'The installation marker is missing.'
    }
    if ((Get-Content -Raw -LiteralPath $targetConfig | ConvertFrom-Json).island.theme -ne 'light') {
        throw 'First-install configuration import failed.'
    }
    if ([IO.File]::ReadAllText($targetProbe) -ne 'legacy-data-snapshot') {
        throw 'First-install data import failed.'
    }
    if ((Get-FileHash -LiteralPath $legacyConfig -Algorithm SHA256).Hash -ne $sourceHash) {
        throw 'Import modified the legacy configuration.'
    }

    # Simulate settings and data changed by the installed application.
    [IO.File]::WriteAllText($targetConfig,
        '{"island":{"theme":"sky"},"translate":{"api_key":"updated-test-placeholder"}}', $utf8)
    [IO.File]::WriteAllText($targetProbe, 'keep-installed-data-on-upgrade', $utf8)
    $configHash = (Get-FileHash -LiteralPath $targetConfig -Algorithm SHA256).Hash
    $dataHash = (Get-FileHash -LiteralPath $targetProbe -Algorithm SHA256).Hash
    $stalePath = Join-Path $appPath '_internal\stale-upgrade-test.txt'
    [IO.File]::WriteAllText($stalePath, 'old-runtime-file', $utf8)

    Invoke-TestInstall -Pass upgrade -MigrationSource $legacyPath
    if ((Get-FileHash -LiteralPath $targetConfig -Algorithm SHA256).Hash -ne $configHash) {
        throw 'Upgrade replaced the installed user configuration.'
    }
    if ((Get-FileHash -LiteralPath $targetProbe -Algorithm SHA256).Hash -ne $dataHash) {
        throw 'Upgrade replaced the installed user data.'
    }
    if (Test-Path -LiteralPath $stalePath) {
        throw 'Upgrade failed to remove a stale packaged runtime file.'
    }
    if ((Test-Path -LiteralPath $uninstallKey -ErrorAction SilentlyContinue) -ne $uninstallExisted) {
        throw 'The test installer changed production uninstall registration.'
    }
    $result = [ordered]@{
        status = 'passed'
        fresh_install_skips_nearby_legacy_data = $true
        first_install_import = $true
        source_unchanged = $true
        upgrade_preserves_config = $true
        upgrade_preserves_data = $true
        stale_runtime_removed = $true
        legacy_install_replaced = $true
        production_uninstall_key_unchanged = $true
        ui_started = $false
        test_directory = $runPath
    }
    $json = $result | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText((Join-Path $runPath 'result.json'), $json, $utf8)
    Write-Output $json
}
finally {
    [Environment]::SetEnvironmentVariable('AISLAND_SETUP_CACHE', $previousCache, 'Process')
    if ($null -eq $previousHome) {
        Remove-Item Env:AISLAND_USER_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:AISLAND_USER_HOME = $previousHome
    }
}
