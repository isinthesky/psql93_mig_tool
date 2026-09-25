<#
.SYNOPSIS
  Release gate for bundled third-party prerequisites (audit M-09).

.DESCRIPTION
  For every entry in the pinned hash file ("<sha256>  <file name>"), the file in
  -PrereqDir must
    1. exist,
    2. match the pinned SHA-256 exactly,
    3. carry an Authenticode signature whose status is Valid,
    4. be signed by -ExpectedSigner (CN and O), and
    5. be timestamped (vendor certificates expire; the timestamp keeps it Valid).
  Any failure exits 1 so build_installer.bat stops before ISCC runs.

  Keep this file ASCII only: Windows PowerShell 5.1 reads BOM-less scripts using
  the system code page.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File installer\verify_prerequisites.ps1 `
    -PrereqDir dist\prerequisites -HashFile installer\prerequisites.sha256
#>
[CmdletBinding()]
param(
    [string]$PrereqDir = 'dist\prerequisites',
    [string]$HashFile = 'installer\prerequisites.sha256',
    [string]$ExpectedSigner = 'Microsoft Corporation'
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host "[ERROR] $Message"
    $script:failed = $true
}

$failed = $false

if (-not (Test-Path -LiteralPath $HashFile -PathType Leaf)) {
    Write-Host "[ERROR] pinned hash file not found: $HashFile"
    exit 1
}

$entries = @()
foreach ($line in (Get-Content -LiteralPath $HashFile -Encoding ASCII)) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    if ($trimmed -notmatch '^([0-9A-Fa-f]{64})\s+\*?(\S+)$') {
        Write-Host "[ERROR] malformed line in ${HashFile}: $trimmed"
        exit 1
    }
    $entries += [pscustomobject]@{ Hash = $Matches[1].ToUpperInvariant(); Name = $Matches[2] }
}
if ($entries.Count -eq 0) {
    Write-Host "[ERROR] no entries in $HashFile"
    exit 1
}

foreach ($entry in $entries) {
    $file = Join-Path $PrereqDir $entry.Name
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        Fail "missing prerequisite: $file"
        continue
    }

    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToUpperInvariant()
    if ($actual -ne $entry.Hash) {
        Fail "SHA-256 mismatch for ${file}: expected $($entry.Hash), got $actual"
        continue
    }

    $sig = Get-AuthenticodeSignature -FilePath $file
    if ($sig.Status -ne 'Valid') {
        Fail "signature status for $file is '$($sig.Status)' (expected 'Valid'): $($sig.StatusMessage)"
        continue
    }
    $cert = $sig.SignerCertificate
    $cn = $cert.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    $orgOk = $cert.Subject -match ('(^|,\s*)O=' + [regex]::Escape($ExpectedSigner) + '(,|$)')
    if ($cn -ne $ExpectedSigner -or -not $orgOk) {
        Fail "unexpected signer for ${file}: $($cert.Subject)"
        continue
    }
    if (-not $sig.TimeStamperCertificate) {
        Fail "signature on $file has no timestamp"
        continue
    }

    $version = (Get-Item -LiteralPath $file).VersionInfo.FileVersion
    Write-Host "[OK] $($entry.Name) sha256=$actual signer=$cn version=$version"
}

if ($failed) {
    Write-Host '[ERROR] prerequisite verification failed - refusing to package.'
    exit 1
}
Write-Host '[OK] all prerequisites match pinned hash and Microsoft signature'
exit 0
