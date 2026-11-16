param(
    [string]$Compiler = (Join-Path $PSScriptRoot '.build-tools\InnoSetup\ISCC.exe'),
    [switch]$SkipFreeze,
    [switch]$TestInstaller,
    # Retained for command-line compatibility; public builds always omit keys.
    [switch]$WithoutPersonalDefaults,
    [string]$UpdateFrom,
    [switch]$FullOnly
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
# This source distribution only creates public, credential-free packages.
# Even -WithoutPersonalDefaults:$false cannot enable private packaging.
$WithoutPersonalDefaults = $true
function Set-ReleaseIntegrity([string]$Executable) {
    # Output folders used by isolated build tools can carry an inherited Low
    # integrity label. Release EXEs must run at normal user (Medium) integrity.
    # This changes only each generated executable, never directory ACLs.
    & (Join-Path $env:SystemRoot 'System32\icacls.exe') $Executable /setintegritylevel M
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot publish a normal-user executable. Run the build from a standard Windows user process.'
    }
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run setup.bat first to prepare the build environment.' }
# Use the installed interpreter image, rather than a launcher inheriting the
# workspace's Low label. Modules still come from this project's venv.
$pythonPath = (& $venvPython -I -S -c 'import sys; print(sys._base_executable)').Trim()
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Base Python interpreter missing.' }
$env:PYTHONPATH = Join-Path $PSScriptRoot '.venv\Lib\site-packages'
$env:PYTHONNOUSERSITE = '1'
if (-not (Test-Path -LiteralPath $Compiler)) { throw 'Inno Setup compiler missing. Pass -Compiler with the full ISCC.exe path.' }
$versionText = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'aisland\version.py')
$version = [regex]::Match($versionText, '__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"').Groups[1].Value
if (-not $version) { throw 'Cannot read application version.' }
$bundle = Join-Path $PSScriptRoot 'dist\Aisland'
$manifestDir = Join-Path $PSScriptRoot 'dist\manifests'
$manifestTool = Join-Path $PSScriptRoot 'scripts\prepare_update.py'
$targetManifest = Join-Path $manifestDir "Aisland-$version-x64.json"
$baselineManifest = ''
if ($WithoutPersonalDefaults -and !$TestInstaller -and !$SkipFreeze -and (Test-Path -LiteralPath $targetManifest)) {
    throw 'This version is already published. Increase the version, or use -SkipFreeze to reuse its verified bundle.'
}
if ($UpdateFrom -and $FullOnly) { throw 'Use either -UpdateFrom or -FullOnly.' }
if ($WithoutPersonalDefaults -and -not $FullOnly) {
    if ($UpdateFrom) {
        $baselineManifest = (Resolve-Path -LiteralPath $UpdateFrom).ProviderPath
    } else {
        # Hash the published bundle BEFORE freezing overwrites it. Keep only the
        # small manifest, rather than a second copy of the models and runtime.
        $previousExe = Join-Path $bundle 'Aisland.exe'
        if ((Test-Path -LiteralPath $previousExe) -and
            -not (Test-Path -LiteralPath (Join-Path $bundle '_internal\private_defaults.json'))) {
            $previousVersion = (Get-Item -LiteralPath $previousExe).VersionInfo.ProductVersion
            if ($previousVersion -and ([version]$previousVersion -lt [version]$version)) {
                $baselineManifest = Join-Path $manifestDir "Aisland-$previousVersion-x64.json"
                & $pythonPath -S $manifestTool --bundle $bundle --version $previousVersion --output $baselineManifest
                if ($LASTEXITCODE -ne 0) { throw 'Previous release manifest failed.' }
            }
        }
        if (-not $baselineManifest -and (Test-Path -LiteralPath $manifestDir)) {
            $latestVersion = [version]'0.0.0'
            foreach ($manifest in Get-ChildItem -LiteralPath $manifestDir -Filter 'Aisland-*-x64.json' -File) {
                $record = Get-Content -Raw -LiteralPath $manifest.FullName | ConvertFrom-Json
                $recordVersion = [version]$record.version
                if ($recordVersion -lt [version]$version -and $recordVersion -gt $latestVersion) {
                    $latestVersion = $recordVersion
                    $baselineManifest = $manifest.FullName
                }
            }
        }
    }
}
$prepareArgs = @('-I', '-S', (Join-Path $PSScriptRoot 'scripts\prepare_personal_defaults.py'), '--without-personal-defaults')
& $pythonPath @prepareArgs
if ($LASTEXITCODE -ne 0) { throw 'Public build credential cleanup failed.' }
$env:AISLAND_BUNDLE_PERSONAL_DEFAULTS = '0'
& $pythonPath -S -m unittest discover -s (Join-Path $PSScriptRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Storage and migration checks failed.' }
if (-not $SkipFreeze) {
    & $pythonPath -S -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot 'Aisland.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Application freezing failed.' }
}
Set-ReleaseIntegrity (Join-Path $bundle 'Aisland.exe')
foreach ($relative in @('config.json', 'data', '_internal\config.json', '_internal\data', '_internal\models\dict')) {
    if (Test-Path -LiteralPath (Join-Path $bundle $relative)) { throw "Private state must not be bundled: $relative" }
}
$verifyArgs = @('-S', (Join-Path $PSScriptRoot 'scripts\verify_distribution.py'), '--without-personal-defaults')
& $pythonPath @verifyArgs
if ($LASTEXITCODE -ne 0) { throw 'Frozen application verification failed.' }
if ($WithoutPersonalDefaults -and !$TestInstaller -and (Test-Path -LiteralPath $targetManifest)) {
    # Reject a changed bundle before overwriting any installer for this version.
    & $pythonPath -S $manifestTool --bundle $bundle --version $version --output $targetManifest
    if ($LASTEXITCODE -ne 0) { throw 'Published release files differ; use a new version.' }
}
$nativeStage = Join-Path $PSScriptRoot ('build\native-installers\' + [guid]::NewGuid().ToString('N'))
$stageRoot = Join-Path $PSScriptRoot 'build\native-installers'
New-Item -ItemType Directory -Path $nativeStage -Force | Out-Null
try {
    # A workspace compiler can inherit Low integrity. Always create its native
    # output in a fresh staging folder; it must not overwrite a Medium release EXE.
    $cleanupSource = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'installer\clear_translation_key.ps1')
    $cleanupCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cleanupSource))
    $compilerArgs = @('/Qp', "/DAppVersion=$version", "/DTranslationCleanupCommand=$cleanupCommand", "/O$nativeStage")
    if ($TestInstaller) { $compilerArgs += '/DInstallerTestMode=1' }
    & $Compiler @compilerArgs (Join-Path $PSScriptRoot 'installer\Aisland.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
    $suffix = if ($TestInstaller) { '-test' } else { '' }
    $setupName = "Aisland-Setup-$version-x64$suffix.exe"
    $distDir = Join-Path $PSScriptRoot 'dist'
    $setupPath = Join-Path $distDir $setupName
    $nativePayload = Join-Path $nativeStage $setupName
    & $pythonPath -S (Join-Path $PSScriptRoot 'scripts\build_setup_bootstrap.py') --payload $nativePayload --output $setupPath
    if ($LASTEXITCODE -ne 0) { throw 'Installer bootstrap compilation failed.' }
    Set-ReleaseIntegrity $setupPath
    $releasePaths = @($setupPath)
    if ($baselineManifest) {
        $updateStage = Join-Path $nativeStage 'update'
        & $pythonPath -S $manifestTool --bundle $bundle --version $version --base $baselineManifest --output $updateStage
        if ($LASTEXITCODE -ne 0) { throw 'Incremental file plan failed.' }
        $plan = Get-Content -Raw -LiteralPath (Join-Path $updateStage 'plan.json') | ConvertFrom-Json
        $updateArgs = $compilerArgs + @('/DIncrementalUpdate=1', "/DUpdateBaseVersion=$($plan.base_version)",
            ('/DUpdateFiles=' + (Join-Path $updateStage 'files.iss')),
            ('/DUpdateDeletes=' + (Join-Path $updateStage 'delete.iss')),
            ('/DUpdateChecks=' + (Join-Path $updateStage 'update-checks.txt')))
        & $Compiler @updateArgs (Join-Path $PSScriptRoot 'installer\Aisland.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Incremental installer compilation failed.' }
        $updateName = $plan.name -replace '\.exe$', "$suffix.exe"
        $updatePath = Join-Path $distDir $updateName
        & $pythonPath -S (Join-Path $PSScriptRoot 'scripts\build_setup_bootstrap.py') --payload (Join-Path $nativeStage $updateName) --output $updatePath
        if ($LASTEXITCODE -ne 0) { throw 'Incremental bootstrap compilation failed.' }
        Set-ReleaseIntegrity $updatePath
        $releasePaths += $updatePath
    }
    if ($WithoutPersonalDefaults -and !$TestInstaller) {
        & $pythonPath -S $manifestTool --bundle $bundle --version $version --output $targetManifest
        if ($LASTEXITCODE -ne 0) { throw 'New release manifest failed.' }
    }
} finally {
    # Remove temporary installer payloads after both successful and failed builds.
    if ($nativeStage -ne $stageRoot -and (Test-Path -LiteralPath $nativeStage)) {
        $resolvedRoot = (Resolve-Path -LiteralPath $stageRoot).ProviderPath
        $resolvedStage = Get-Item -LiteralPath $nativeStage
        if ($resolvedStage.Parent.FullName -eq $resolvedRoot -and
            -not ($resolvedStage.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Remove-Item -LiteralPath $resolvedStage.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
if (-not $TestInstaller) {
    # Only a successful release build retires older installers and refreshes the hash.
    foreach ($old in Get-ChildItem -LiteralPath $distDir -Filter 'Aisland-Setup-*.exe' -File) {
        if ($old.Name -ne $setupName -and $old.Name -notlike '*-test.exe') {
            Remove-Item -LiteralPath $old.FullName -Force
        }
    }
    foreach ($old in Get-ChildItem -LiteralPath $distDir -Filter 'Aisland-Update-*.exe' -File) {
        if ($old.Name -notlike "Aisland-Update-*-to-$version-x64.exe" -and $old.Name -notlike '*-test.exe') {
            Remove-Item -LiteralPath $old.FullName -Force
        }
    }
    # Explicit -UpdateFrom builds may target several older releases. Keep every
    # verified package for the current destination version in the checksum list.
    $releasePaths = @($setupPath) + @(Get-ChildItem -LiteralPath $distDir -Filter "Aisland-Update-*-to-$version-x64.exe" -File |
        Select-Object -ExpandProperty FullName)
    $hashLines = foreach ($releasePath in $releasePaths) {
        $releaseHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $releasePath).Hash
        "$releaseHash  $([IO.Path]::GetFileName($releasePath))"
    }
    Set-Content -LiteralPath (Join-Path $distDir 'SHA256.txt') -Value $hashLines -Encoding ASCII
}
Get-Item -LiteralPath $releasePaths | Select-Object FullName, Length
Get-FileHash -Algorithm SHA256 -LiteralPath $releasePaths | Format-List Algorithm, Hash, Path
Write-Output 'Public installer: no bundled translation credentials.'
