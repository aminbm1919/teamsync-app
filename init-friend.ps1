# Connect to a shared project someone else started, then keep it synced.
#
# Role   : one-time setup for the person who joins.
# Input  : -RepoName <the name they gave you>  -Path <where to put it on your disk>
# Output : the project downloaded into that folder, and teamsync running.
# Never  : creates anything on GitHub. It only joins what already exists.
#
# Usage:
#   pwsh init-friend.ps1 -RepoName my-project -Owner their-github-name -Path "C:\work\my-project"
#
# Accept the GitHub invitation FIRST. Until you do, this fails with
# "repository not found", which looks like a typo but is not.

param(
    [Parameter(Mandatory = $true)][string]$RepoName,
    # Whose account the project lives on. Required, and deliberately without a
    # default: it once carried this author's own username, which was invisible
    # here and wrong for everybody else who cloned the public source - they
    # would have been sent to a stranger's account with no idea why.
    [Parameter(Mandatory = $true)][string]$Owner,
    [string]$Path,
    [string]$Me,
    [string]$MyEmail,
    [switch]$NoWatch
)

$ErrorActionPreference = 'Stop'
function Step($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Die($t)  { Write-Host $t -ForegroundColor Red; exit 1 }

if (-not $Path) { $Path = Join-Path (Get-Location) $RepoName }

if (Test-Path -LiteralPath $Path) {
    $existing = Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($existing) { Die "Folder already exists and is not empty: $Path`nChoose an empty folder, or one that does not exist yet." }
}

. (Join-Path $PSScriptRoot 'sync-core.ps1')
if (-not (Test-Prerequisites)) { exit 1 }

$url = "https://github.com/$Owner/$RepoName"

Step "Downloading $Owner/$RepoName"
git clone $url $Path
if ($LASTEXITCODE -ne 0) {
    Die "Could not download it.`n`nMost likely you have not accepted the invitation yet - check your email or`nhttps://github.com/notifications . Until you accept, GitHub answers`n'repository not found', which is not a typo on your side.`n`nOtherwise: check the name, and check your VPN."
}

$repo = (Resolve-Path -LiteralPath $Path).Path
Set-Location -LiteralPath $repo

Step 'Setting your identity for this project'
if (git config core.hooksPath) { git config core.hooksPath '.git/hooks' }

# push-now.ps1 normally arrives with the download. Restore it if it is missing,
# e.g. the project was started before this button existed.
if (-not (Test-Path (Join-Path $repo 'push-now.ps1'))) {
    $tpl = Join-Path $PSScriptRoot 'push-now.template.ps1'
    if (Test-Path $tpl) { Copy-Item -LiteralPath $tpl -Destination (Join-Path $repo 'push-now.ps1') -Force }
}
if ($Me)      { git config user.name  $Me }
if ($MyEmail) { git config user.email $MyEmail }
if (-not (git config user.name)) {
    Write-Host '    No git user.name is set. Your commits will not say who wrote them.' -ForegroundColor Yellow
    Write-Host '    Fix with:  git config user.name "Your Name"' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Connected.' -ForegroundColor Green
Write-Host "  Repository : $url"
Write-Host "  Folder     : $repo"
Write-Host ''

if ($NoWatch) {
    Write-Host 'Start syncing whenever you are ready:' -ForegroundColor Yellow
    Write-Host "  pwsh teamsync.ps1 -Path `"$repo`""
} else {
    Write-Host 'Starting teamsync...' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'teamsync.ps1') -Path $repo
}
