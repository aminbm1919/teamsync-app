# Put a passphrase on the release signing key - or change it.
#
# Role   : make publishing require something only you know, not just access to
#          your files.
# Input  : typed by you, masked. Nothing is read from a file or an argument.
# Output : the key on disk becomes encrypted. Verified afterwards, not assumed.
# Never  : writes the passphrase anywhere, or reports success without proving it.
#
#   pwsh set-release-passphrase.ps1
#
# ssh-keygen's own prompt shows nothing at all while you type - not even dots -
# which is how people end up with a mistyped repeat. This asks with masking and
# compares the two before touching the key.

param(
    [string]$Key = "$env:USERPROFILE\.ssh\teamsync-release",
    [int]$MinLength = 8
)

$ErrorActionPreference = 'Stop'
function Die($t) { Write-Host ''; Write-Host $t -ForegroundColor Red; exit 1 }

if (-not (Test-Path $Key)) { Die "Signing key not found: $Key" }

function Read-Masked($prompt) {
    $secure = Read-Host -Prompt $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Test-KeyLocked {
    # Proof, not assumption: an encrypted key stops to ask, so with nothing to
    # read from it fails instead of succeeding. Checking the file for the word
    # "ENCRYPTED" does NOT work - the modern key format never contains it.
    #
    # Built on .NET directly rather than Start-Process, which on this shell
    # rejects a shared output/error target and treats "NUL" as a relative path -
    # both of which fail in the direction that makes every key look protected.
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'ssh-keygen'
    # .Arguments, not .ArgumentList: ArgumentList exists only on the newer .NET
    # of pwsh 7 and is null under Windows PowerShell 5.1 - which is what this
    # script gets run with. The key path is quoted by hand for the same reason.
    $psi.Arguments = '-y -f "' + $Key + '"'

    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true
    try { $p = [Diagnostics.Process]::Start($psi) } catch { return $true }
    $p.StandardInput.Close()        # nothing to answer a passphrase prompt with
    if (-not $p.WaitForExit(8000)) { try { $p.Kill() } catch { }; return $true }
    return $p.ExitCode -ne 0
}

Write-Host ''
$locked = Test-KeyLocked
Write-Host ("Current state: " + $(if ($locked) { 'the key already has a passphrase' } else { 'the key has NO passphrase' })) -ForegroundColor Cyan
Write-Host ''

$old = ''
if ($locked) {
    $old = Read-Masked 'Current passphrase'
    if (-not $old) { Die 'Nothing entered. Nothing was changed.' }
}

while ($true) {
    $a = Read-Masked 'New passphrase'
    if ($a.Length -lt $MinLength) {
        Write-Host "  Too short - at least $MinLength characters. Try again." -ForegroundColor Yellow
        continue
    }
    $b = Read-Masked 'Repeat it'
    if ($a -ne $b) {
        Write-Host '  The two did not match. Try again - nothing has been changed yet.' -ForegroundColor Yellow
        continue
    }
    break
}

Write-Host ''
Write-Host 'Applying...' -ForegroundColor Cyan
# The command line is built by hand, for one load-bearing reason: Windows
# PowerShell 5.1 silently DROPS an empty-string argument when calling a native
# program - and on a key with no passphrase yet, the old passphrase IS the
# empty string. With it dropped, -P swallowed -N, the new passphrase became a
# stray argument, and ssh-keygen answered "Too many arguments" for every
# valid passphrase. Hand-built quoting keeps "" alive. The passphrase is
# briefly visible to anything reading process arguments on this machine -
# acceptable on your own laptop; ssh-keygen's own hidden prompt is what
# caused the mistyped-repeat problem this script exists to prevent.
function Quote-Arg([string]$v) {
    '"' + ($v -replace '(\*)"', '$1$1\"' -replace '(\+)$', '$1$1') + '"'
}
$psi2 = [Diagnostics.ProcessStartInfo]::new()
$psi2.FileName = 'ssh-keygen'
$psi2.Arguments = '-p -f ' + (Quote-Arg $Key) + ' -P ' + (Quote-Arg $old) + ' -N ' + (Quote-Arg $a)
$psi2.RedirectStandardOutput = $true
$psi2.RedirectStandardError  = $true
$psi2.UseShellExecute = $false
$psi2.CreateNoWindow  = $true
$p2 = [Diagnostics.Process]::Start($psi2)
$out2 = $p2.StandardOutput.ReadToEnd() + $p2.StandardError.ReadToEnd()
$p2.WaitForExit()
$out2.Trim() -split "`n" | ForEach-Object { "  $_" }
if ($p2.ExitCode -ne 0) { Die 'ssh-keygen refused. The key was not changed.' }

$a = $null; $b = $null; $old = $null
[GC]::Collect()

Write-Host ''
if (Test-KeyLocked) {
    Write-Host 'Done - the key is now locked.' -ForegroundColor Green
    Write-Host ''
    Write-Host 'From now on, publishing a release asks for this passphrase.' -ForegroundColor DarkGray
    Write-Host 'It is checked HERE, by the key file on this laptop. GitHub never sees it' -ForegroundColor DarkGray
    Write-Host 'and cannot reset it. If it is forgotten, a new key must be generated and' -ForegroundColor DarkGray
    Write-Host 'one new exe handed over by hand, once.' -ForegroundColor DarkGray
} else {
    Die 'ssh-keygen reported success but the key still opens without a passphrase. Nothing is protected - tell Amin.'
}
