# Embedded in the uninstaller so elevated cleanup does not load a script from {app}.
$ErrorActionPreference = 'Stop'
try {
    $override = $env:AISLAND_USER_HOME
    if (-not $override) { $override = $env:WISLAND_USER_HOME }
    if ($override) {
        $stateRoots = @([Environment]::ExpandEnvironmentVariables($override.Trim()))
    }
    else {
        $localRoots = @([Environment]::GetFolderPath('LocalApplicationData'))
        $profileKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\*'
        foreach ($profile in Get-ItemProperty -Path $profileKey) {
            if ($profile.ProfileImagePath -and $profile.PSChildName -match '^S-1-(5-21|12-1)-[0-9-]+$') {
                $localRoots += Join-Path $profile.ProfileImagePath 'AppData\Local'
            }
        }
        $stateRoots = foreach ($local in $localRoots | Sort-Object -Unique) {
            if ($local) {
                Join-Path $local 'Aisland'
                Join-Path $local 'Wisland'
            }
        }
    }

    $keyFields = @('api_key', 'api_key_protected', 'import_from')
    foreach ($root in $stateRoots) {
        if ($root -notmatch '^[A-Za-z]:[\\/]') { throw 'Expected local user data' }
        $configPath = Join-Path $root 'config.json'
        if (-not [IO.File]::Exists($configPath)) { continue }

        $node = Get-Item -LiteralPath $configPath -Force
        while ($node) {
            if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Redirected user data requires manual cleanup'
            }
            $node = if ($node -is [IO.FileInfo]) { $node.Directory } else { $node.Parent }
        }
        $config = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($configPath))
        $translation = $config.translate
        if ($translation -isnot [PSCustomObject]) { continue }
        $found = $keyFields | Where-Object { [bool]$translation.$_ }
        if (-not $found) { continue }
        foreach ($field in $keyFields) {
            $translation.PSObject.Properties.Remove($field)
        }
        $translation | Add-Member -NotePropertyName use_bundled_defaults -NotePropertyValue $false -Force
        $tempPath = $configPath + '.uninstall-' + [guid]::NewGuid().ToString('N')
        try {
            $content = ConvertTo-Json -InputObject $config -Depth 100
            [IO.File]::WriteAllText($tempPath, $content, [Text.UTF8Encoding]::new($false))
            [IO.File]::Replace($tempPath, $configPath, [NullString]::Value)
        }
        finally {
            if ([IO.File]::Exists($tempPath)) { [IO.File]::Delete($tempPath) }
        }
    }
}
catch {
    # A malformed configuration can contain a credential; keep it out of uninstall logs.
    exit 1
}
exit 0
