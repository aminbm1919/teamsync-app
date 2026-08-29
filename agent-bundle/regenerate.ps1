# Rebuild both bundle files from the app's canonical prompt.
#
# Role   : keeps the bundle a COPY, never a fork. Edit the prompt asset
#          (ui\assets\help\3-agent-prompt.md), run this, done.
# Input  : the asset file.
# Output : claude-skill\teamsync-team-project\SKILL.md and codex\AGENTS-teamsync.md
$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent $PSScriptRoot
$asset = Join-Path $root 'ui\assets\help\3-agent-prompt.md'
$body  = Get-Content -LiteralPath $asset -Raw
$parts = $body -split "(?m)^---\s*$", 2
$core  = if ($parts.Count -eq 2) { $parts[1].Trim() } else { $body.Trim() }

$skillHead = @'
---
name: teamsync-team-project
description: >
  Working inside a TeamSync shared team project - any folder whose root
  contains TEAM-PROJECT-REFERENCE.md or a .teamsync.lock heartbeat, or when
  the user says a project is a team/shared project. Covers the three standing
  commands (who.ps1, working.ps1, push-now.ps1), the write-only announcement
  rule, conflict and crossed-edit handling, and the agent's part in helping
  the humans run the project. Follow this before editing anything in such a
  project.
---

# TeamSync team project - how to work here

First: if the project root has its own `TEAM-PROJECT-REFERENCE.md`, read it -
it is canonical and wins over this skill wherever they differ. What follows
is the standing protocol.

'@
$skillTail = @'

## One more thing, for Claude specifically

Run the three commands through your shell tool from the project root, exactly
as written. Report their outcomes honestly to your user - especially exit 1
(offline: committed and safe, the engine retries by itself; do not loop) and
exit 2 (conflict: stop and follow the conflict section).
'@
$codexHead = @'
# TeamSync shared project - standing rules for Codex

Add this file's content to the project's AGENTS.md, or paste it into Codex's
global standing instructions. If the project root has its own
TEAM-PROJECT-REFERENCE.md, that file is canonical and wins wherever they
differ.

'@
# A blank line between each head and the body. A here-string keeps only ONE
# trailing newline, so the head's last sentence and the body's first ran
# together into a single paragraph in every generated file.
$enc = New-Object Text.UTF8Encoding $false
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'claude-skill\teamsync-team-project\SKILL.md'),
                        $skillHead + "`n" + $core + "`n" + $skillTail + "`n", $enc)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'codex\AGENTS-teamsync.md'),
                        $codexHead + "`n" + $core + "`n", $enc)

# The charter also lives in TEAM-PROJECT-REFERENCE.md, which ships into every
# shared project. It used to be a hand-kept second copy and had already
# drifted - so it is generated from the same asset now, between two markers.
# The reference's own advice, applied to the reference: a rule written in two
# places is a rule that will eventually disagree with itself.
$charter = ''
if ($core -match '(?ms)^(## Running the project, together with the humans.*?)(?=^## )') {
    $charter = $Matches[1].TrimEnd()
}
$ref = Join-Path (Split-Path -Parent $root) 'TEAM-PROJECT-REFERENCE.md'
if ($charter -and (Test-Path -LiteralPath $ref)) {
    $begin = '<!-- charter:begin - generated from ui\assets\help\3-agent-prompt.md by agent-bundle\regenerate.ps1 -->'
    $end   = '<!-- charter:end -->'
    $text  = [IO.File]::ReadAllText($ref)
    $block = $begin + "`n`n" + $charter + "`n`n" + $end
    if ($text -match [regex]::Escape($begin)) {
        $pattern = [regex]::Escape($begin) + '.*?' + [regex]::Escape($end)
        $text = [regex]::Replace($text, $pattern, { $block }, 'Singleline')
    } else {
        # First run: replace the hand-written section in place, so the file
        # keeps its shape instead of gaining a duplicate.
        $pattern = '(?ms)^## Running the project, together with the humans.*?(?=^#{2,3} )'
        if ($text -match $pattern) {
            $text = [regex]::Replace($text, $pattern, { $block + "`n`n" })
        } else {
            Write-Host 'TEAM-PROJECT-REFERENCE.md: no charter section found - left alone.' -ForegroundColor Yellow
            $block = ''
        }
    }
    if ($block) {
        [IO.File]::WriteAllText($ref, $text, $enc)
        Write-Host 'Charter refreshed in TEAM-PROJECT-REFERENCE.md.'
    }
}
Write-Host 'Bundle regenerated from the canonical prompt.'
