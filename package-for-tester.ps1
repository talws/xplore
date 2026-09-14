# package-for-tester.ps1 - build a zip of Xplore to send to someone else.
#
#   powershell -ExecutionPolicy Bypass -File package-for-tester.ps1
#
# Writes two files to dist\ :
#   Xplore-<version>-<date>.zip          the app: code, UI, assets, extension, README
#   Xplore-extension-<version>.zip       only the Chrome extension, for convenience
#
# Built from the last COMMIT with "git archive", never from the folder, so the
# database, exports, logs and unparsed payloads cannot be included: they are not
# tracked. Internal handoff notes are left out too. The build then lists the zip
# and stops if anything that looks like data got in anyway.
# ASCII only: Windows PowerShell 5.1 reads a script without a BOM in the legacy codepage.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (git status --porcelain) {
    Write-Warning "There are uncommitted changes. The package is built from the last commit, so they will NOT be in it."
}

$version = (Get-Content (Join-Path $here "extension\manifest.json") -Raw | ConvertFrom-Json).version
$stamp = Get-Date -Format "yyyy-MM-dd"
$commit = (git rev-parse --short HEAD).Trim()
$dist = Join-Path $here "dist"
New-Item -ItemType Directory -Force $dist | Out-Null

# Everything tracked, minus the owner's internal notes.
$exclude = @("HANDOFF*", "package-for-tester.ps1")
$files = git ls-files | Where-Object {
    $f = $_
    -not ($exclude | Where-Object { $f -like $_ })
}

$app = Join-Path $dist "Xplore-$version-$stamp.zip"
$ext = Join-Path $dist "Xplore-extension-$version.zip"
Remove-Item $app, $ext -ErrorAction SilentlyContinue

git archive --format=zip --prefix="Xplore/" -o $app HEAD -- $files
git archive --format=zip --prefix="Xplore-extension/" -o $ext "HEAD:extension"

# Verify: nothing that looks like collected data.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$bad = @()
foreach ($zip in $app, $ext) {
    $z = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $bad += $z.Entries | Where-Object {
            $_.FullName -match '\.(db|db-wal|db-shm|sqlite|log|csv)$' -or
            $_.FullName -match 'x-links' -or $_.FullName -match '/unparsed/' -or
            $_.FullName -match 'HANDOFF'
        } | ForEach-Object { "$zip :: $($_.FullName)" }
        if ($zip -eq $app) { $count = $z.Entries.Count; $size = (Get-Item $zip).Length }
    } finally { $z.Dispose() }
}
if ($bad) {
    Remove-Item $app, $ext -ErrorAction SilentlyContinue
    throw "Refusing to package: data-like files found:`n$($bad -join "`n")"
}

"Built from commit $commit"
"  $app  ($count files, $([math]::Round($size / 1KB)) KB)"
"  $ext"
"No database, exports, logs or handoff notes inside."
