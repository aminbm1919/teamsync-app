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
$enc = New-Object Text.UTF8Encoding $false
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'claude-skill\teamsync-team-project\SKILL.md'),
                        $skillHead + $core + "`n" + $skillTail, $enc)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'codex\AGENTS-teamsync.md'),
                        $codexHead + $core + "`n", $enc)
Write-Host 'Bundle regenerated from the canonical prompt.'
