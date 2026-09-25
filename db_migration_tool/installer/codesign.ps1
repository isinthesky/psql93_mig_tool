<#
.SYNOPSIS
  Optional Authenticode signing hook for release artifacts (audit M-11).

.DESCRIPTION
  Signs and verifies each -Path with signtool when a code-signing certificate is
  configured through environment variables; otherwise prints a loud
  "UNSIGNED BUILD" warning and exits 0 (or 1 when CODESIGN_REQUIRED=1).

  Environment variables (never pass secrets on the command line):
    CODESIGN_CERT_THUMBPRINT  SHA-1 thumbprint of a certificate already in the
                              CurrentUser\My or LocalMachine\My store (HSM/token OK).
    CODESIGN_PFX              Path to a .pfx file (used when no thumbprint is set).
    CODESIGN_PFX_PASSWORD     Password for CODESIGN_PFX. The PFX is imported into
                              CurrentUser\My for the duration of the run and removed
                              afterwards, so the password never appears on the
                              signtool command line / process list.
    CODESIGN_TIMESTAMP_URL    RFC 3161 timestamp server (default DigiCert).
    CODESIGN_REQUIRED=1       Treat a missing certificate or an unsigned file as an
                              error (use for release builds).
    SIGNTOOL                  Full path to signtool.exe (otherwise PATH, then the
                              newest Windows 10/11 SDK x64 signtool).

  -CheckOnly reports whether each file already has a Valid signature and never signs.

  Keep this file ASCII only: Windows PowerShell 5.1 reads BOM-less scripts using
  the system code page.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File installer\codesign.ps1 -Path dist\DBMigrationTool.exe
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

$required = ($env:CODESIGN_REQUIRED -eq '1')
$thumbprint = if ($env:CODESIGN_CERT_THUMBPRINT) { ($env:CODESIGN_CERT_THUMBPRINT -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() } else { '' }
$pfxPath = $env:CODESIGN_PFX
$timestampUrl = if ($env:CODESIGN_TIMESTAMP_URL) { $env:CODESIGN_TIMESTAMP_URL } else { 'http://timestamp.digicert.com' }

function Write-UnsignedBanner([string]$Reason) {
    Write-Host ''
    Write-Host '=================================================================='
    Write-Host ' WARNING: UNSIGNED BUILD'
    Write-Host " $Reason"
    Write-Host ' Windows cannot verify the publisher or detect tampering of:'
    foreach ($p in $Path) { Write-Host "   $p" }
    Write-Host ' Do not distribute as a release. Set CODESIGN_CERT_THUMBPRINT or'
    Write-Host ' CODESIGN_PFX + CODESIGN_PFX_PASSWORD to sign (see BUILD_GUIDE.md).'
    Write-Host '=================================================================='
    Write-Host ''
}

function Find-SignTool {
    if ($env:SIGNTOOL) {
        if (Test-Path -LiteralPath $env:SIGNTOOL -PathType Leaf) { return $env:SIGNTOOL }
        throw "SIGNTOOL points to a missing file: $($env:SIGNTOOL)"
    }
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path -LiteralPath $kits) {
        $found = Get-ChildItem -Path $kits -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.DirectoryName -like '*\x64' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw 'signtool.exe not found (install the Windows SDK or set SIGNTOOL)'
}

function Assert-ValidSignature([string]$File) {
    $sig = Get-AuthenticodeSignature -FilePath $File
    if ($sig.Status -ne 'Valid') {
        throw "signature status for $File is '$($sig.Status)': $($sig.StatusMessage)"
    }
    if (-not $sig.TimeStamperCertificate) {
        throw "signature on $File has no timestamp"
    }
    Write-Host "[OK] signed: $File"
    Write-Host "     signer   : $($sig.SignerCertificate.Subject)"
    Write-Host "     thumbprint: $($sig.SignerCertificate.Thumbprint)"
    Write-Host "     timestamp: $($sig.TimeStamperCertificate.Subject)"
}

foreach ($p in $Path) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        Write-Host "[ERROR] file to sign not found: $p"
        exit 1
    }
}

if ($CheckOnly) {
    $unsigned = @()
    foreach ($p in $Path) {
        $sig = Get-AuthenticodeSignature -FilePath $p
        if ($sig.Status -eq 'Valid') {
            Write-Host "[OK] already signed: $p ($($sig.SignerCertificate.Subject))"
        } else {
            $unsigned += $p
        }
    }
    if ($unsigned.Count -gt 0) {
        Write-UnsignedBanner ('No valid Authenticode signature on: ' + ($unsigned -join ', '))
        if ($required) { exit 1 }
    }
    exit 0
}

if (-not $thumbprint -and -not $pfxPath) {
    Write-UnsignedBanner 'No code-signing certificate configured (CODESIGN_CERT_THUMBPRINT / CODESIGN_PFX).'
    if ($required) {
        Write-Host '[ERROR] CODESIGN_REQUIRED=1 but no certificate is configured.'
        exit 1
    }
    exit 0
}

$exitCode = 0
$importedThumbprint = $null
try {
    $signtool = Find-SignTool
    Write-Host "[INFO] signtool: $signtool"

    if (-not $thumbprint) {
        if (-not (Test-Path -LiteralPath $pfxPath -PathType Leaf)) {
            throw "CODESIGN_PFX not found: $pfxPath"
        }
        $plain = [Environment]::GetEnvironmentVariable('CODESIGN_PFX_PASSWORD')
        if (-not $plain) { throw 'CODESIGN_PFX is set but CODESIGN_PFX_PASSWORD is empty' }
        $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
        $plain = $null
        $pfx = Get-PfxData -FilePath $pfxPath -Password $secure
        $thumbprint = $pfx.EndEntityCertificates[0].Thumbprint
        if (-not (Test-Path -LiteralPath "Cert:\CurrentUser\My\$thumbprint")) {
            Import-PfxCertificate -FilePath $pfxPath -CertStoreLocation Cert:\CurrentUser\My -Password $secure | Out-Null
            $importedThumbprint = $thumbprint
        }
    }

    foreach ($p in $Path) {
        Write-Host "[INFO] signing $p"
        & $signtool sign /sha1 $thumbprint /fd SHA256 /tr $timestampUrl /td SHA256 /d 'DB Migration Tool' $p
        if ($LASTEXITCODE -ne 0) { throw "signtool sign failed for $p (exit $LASTEXITCODE)" }
        & $signtool verify /pa /tw $p
        if ($LASTEXITCODE -ne 0) { throw "signtool verify failed for $p (exit $LASTEXITCODE)" }
        Assert-ValidSignature $p
    }
}
catch {
    Write-Host "[ERROR] code signing failed: $($_.Exception.Message)"
    $exitCode = 1
}
finally {
    if ($importedThumbprint) {
        Remove-Item -LiteralPath "Cert:\CurrentUser\My\$importedThumbprint" -DeleteKey -ErrorAction SilentlyContinue
    }
}
exit $exitCode
