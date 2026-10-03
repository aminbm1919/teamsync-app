# push-now - "I am done, publish my work now."
#
# Role   : publish immediately instead of waiting out the 4-minute quiet window.
# Input  : none. Run it from anywhere inside the project folder.
#          -CheckOnly publishes nothing: it lists what the guard would hold
#          back - work it would destroy, and large content - and exits 0.
# Output : your work on GitHub, or a clear reason why not. Exit code 0 = published
#          (or nothing to publish), 1 = blocked, 2 = conflict, 3 = this would
#          delete or roll back work, or upload large new content, and needs a
#          human's word first.
# Never  : force-pushes or discards anything.
#
#   pwsh push-now.ps1
#
# AI agents: run this when you have finished a piece of work. It is safe to run at
# any time, and safe to run twice. If it reports a conflict, read the CONFLICT.md
# file it names before changing anything.

param([int]$TimeoutSeconds = 120, [switch]$CheckOnly)

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
Set-Location -LiteralPath $repo

# Git prints UTF-8, but Windows PowerShell decodes what a program prints with
# the CONSOLE's code page - 437 or 720 on most machines. Measured: a Persian
# file name came back as box-drawing garbage, and a changed Persian file was
# reported as not existing, so the guard below never saw it. Read git as UTF-8
# while this runs, and give the console its own setting back afterwards.
$script:ConsoleEncodingWas = $null
try { $script:ConsoleEncodingWas = [Console]::OutputEncoding } catch { }
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { }
try {

$lock   = Join-Path $repo '.teamsync.lock'
$signal = Join-Path $repo '.teamsync-push-now'

function Say($t, $c = 'Gray') { Write-Host $t -ForegroundColor $c }

if ((git rev-parse --is-inside-work-tree 2>$null) -ne 'true') {
    Say 'This folder is not a git repository. Nothing to publish.' 'Red'; exit 1
}

# Git escapes non-ASCII paths in its output unless told not to, so a Persian
# or Arabic file name comes back as "\331\201..." and matches nothing. The
# engine sets this too; this script can run before the engine ever has.
git config core.quotePath false 2>$null | Out-Null

# A conflict outranks everything. Publishing on top of one is never right.
if (@(git diff --name-only --diff-filter=U 2>$null).Count -gt 0) {
    # The newest CONFLICT export: named by its moment, -02, -03 for a second
    # that already had one (New-ExportFolder in sync-core.ps1), so name order
    # is time order. A crossed-edits keepsake is not one, and neither is a
    # teammate's report saved from the window (<name>-report) - it sorted
    # after every export and was offered as the conflict to read.
    $latest = Get-ChildItem (Join-Path $repo '_conflicts') -Directory -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?$' } |
              Sort-Object Name | Select-Object -Last 1
    Say 'A conflict is open. Nothing was published.' 'Red'
    if ($latest) { Say "Read: _conflicts\$($latest.Name)\CONFLICT.md" 'Yellow' }
    Say 'Finish it, then run this again.' 'Yellow'
    exit 2
}

# Adding work needs no permission; destroying it does. The engine refuses to
# publish a deletion or a roll-back on its own - but THIS script publishes
# directly whenever the engine is not running, so without the same check it
# would be the way round the guard. That matters most here: this is the script
# AGENTS run, and an agent that tidied a file away would send that tidy-up to
# everybody's machine with no one having seen it.
#
# The rule is the block below, a byte-for-byte copy of the one in sync-core.ps1.

# >>> destructive guard
# This block is IDENTICAL in sync-core.ps1 and push-now.template.ps1, and
# test_destructive_agreement fails the moment the two copies differ by a
# single character. push-now cannot dot-source this file - it is planted into
# projects and runs without the app - so the rule travels as a copy, and the
# test is what keeps it one rule.
#
# What is about to leave this machine that DESTROYS work rather than adding
# to it. Two shapes, both measured on this engine before the guard existed:
#
#   deleted  - a file the team has, or this machine had, that will no longer
#              exist once this is published. It disappears from every
#              teammate's disk, and the log used to say only "pushed 1
#              commit(s)". A MOVE is not a deletion: when the file's exact
#              content arrives under a new name in the same publish, nothing
#              is destroyed and nobody is asked (the user's ruling,
#              2026-10-01 - asking on every move teaches people to approve
#              without reading, and then the real deletion slips through).
#              Moved AND changed, or moved somewhere the project ignores,
#              still asks.
#   reverted - a file whose new content is byte-identical to an EARLIER
#              version of itself. Not a guess: the blob hash matches one the
#              file really held, which is exactly what putting a backup back
#              looks like. Newer text is replaced by older text wherever it
#              lands.
#
# "About to leave" means everything not yet on the shared branch: work still
# on the disk AND work already committed here but not yet sent. The second
# half used to be invisible - a deletion made with `git commit` by hand, or
# with an editor's commit button, went out without anybody being asked.
#
# "Earlier version" means EVERY earlier version. It used to mean the last
# forty, a ceiling that existed only because each version cost one git
# process: 14 changed files of a real project meant about 250 processes per
# look, every four seconds, which froze the app's window for good. One
# `git log` pass now reads every version of every file at once, so the
# ceiling saved nothing and is gone.
#
# A machine that is merely BEHIND is still never caught. Everything is
# measured from where this machine and the team last agreed - the merge base
# of HEAD and the shared branch - never from the server's tip, so a file this
# machine simply has not received yet is not "deleted". "I never had it" and
# "I deleted it" stay different states.
#
# Any git question that goes unanswered throws. It must never read as
# "nothing destructive here"; the caller holds the publish instead.
$script:DG_Zero = '0' * 40

function Invoke-DGGit {
    param([string[]]$GitArgs, [string]$What)
    $ErrorActionPreference = 'Continue'
    # Read-only questions must not take git's optional index lock: the app's
    # window asks the same questions while the engine is committing, and a
    # lock held for a moment by a reader made the writer's `git add` fail.
    $locks = $env:GIT_OPTIONAL_LOCKS
    $env:GIT_OPTIONAL_LOCKS = '0'
    try {
        $out = & git @GitArgs 2>$null
        $code = $LASTEXITCODE
    } finally {
        $env:GIT_OPTIONAL_LOCKS = $locks
    }
    if ($code -ne 0) { throw "git could not answer the destructive check ($What)" }
    $out
}

function Split-DGZ {
    # git -z output, as PowerShell hands it over: lines, with NULs inside.
    param($Lines)
    foreach ($t in ((@($Lines) -join "`n").Split([char]0))) {
        $t = $t.TrimStart([char]13, [char]10)
        if ($t) { $t }
    }
}

function Split-DGBatches {
    # Windows caps a command line at 32,767 characters, and a guard that died
    # on a large change set would hide exactly the large accidents.
    param([string[]]$Items, [int]$Limit = 8000)
    $batches = New-Object 'System.Collections.Generic.List[object]'
    $batch = New-Object 'System.Collections.Generic.List[string]'
    $size = 0
    foreach ($it in $Items) {
        if ($batch.Count -gt 0 -and ($size + $it.Length + 3) -gt $Limit) {
            $batches.Add($batch.ToArray())
            $batch = New-Object 'System.Collections.Generic.List[string]'
            $size = 0
        }
        $batch.Add($it)
        $size += $it.Length + 3
    }
    if ($batch.Count -gt 0) { $batches.Add($batch.ToArray()) }
    return ,$batches
}

function Get-DGChanged {
    # Paths that differ between $Base and the disk, by kind: D, M or A.
    # --no-renames: a rename is a deletion of the old name for everybody else,
    # whether or not it happened to be staged with `git mv`.
    param([string]$Base, [string]$Letter)
    Split-DGZ (Invoke-DGGit @('diff', '--name-only', '-z', '--no-renames',
                              "--diff-filter=$Letter", $Base, '--') "diff $Letter $Base")
}

function Get-DGVersions {
    # Every blob each path has held anywhere in the history of $Rev - both
    # sides of every change, and merges too: -c lists a merge whose result
    # differs from all of its parents, which is where a hand-made conflict
    # resolution lives. One pass for all paths.
    param([string]$Rev, [string[]]$Paths)
    $found = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    foreach ($p in $Paths) {
        $found[$p] = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }
    foreach ($batch in (Split-DGBatches $Paths)) {
        $gitArgs = @('--literal-pathspecs', 'log', $Rev, '--full-history', '--root', '-c', '--raw',
                     '--no-abbrev', '--no-renames', '-z', '--format=', '--') + $batch
        $tokens = @(Split-DGZ (Invoke-DGGit $gitArgs "log $Rev"))
        $i = 0
        while ($i -lt $tokens.Count) {
            $meta = $tokens[$i]
            if ($meta.StartsWith(':') -and ($i + 1) -lt $tokens.Count) {
                # ":<modes> <blobs> <status>" with one colon per parent.
                $body = $meta.TrimStart(':')
                $parents = $meta.Length - $body.Length
                $fields = $body.Split(' ')
                $path = $tokens[$i + 1]
                if ($found.ContainsKey($path)) {
                    for ($k = $parents + 1; $k -lt (2 * ($parents + 1)) -and $k -lt $fields.Count; $k++) {
                        if ($fields[$k] -ne $script:DG_Zero) { [void]$found[$path].Add($fields[$k]) }
                    }
                }
                $i += 2
            } else {
                $i += 1
            }
        }
    }
    return ,$found
}

function Get-DGWorkingBlobs {
    # What each file on the disk would be recorded as: its blob id after git's
    # own clean filters, exactly what a commit would store.
    param([string]$Root, [string[]]$Paths)
    $out = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    $byFull = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    foreach ($p in $Paths) {
        $full = Join-Path $Root $p
        if (Test-Path -LiteralPath $full -PathType Leaf) { $byFull[$full] = $p }
    }
    foreach ($batch in (Split-DGBatches @($byFull.Keys))) {
        $shas = @(Invoke-DGGit (@('hash-object', '--') + $batch) 'hash-object' | Where-Object { $_ })
        if ($shas.Count -ne $batch.Count) { throw 'git hash-object answered for the wrong number of files' }
        for ($k = 0; $k -lt $batch.Count; $k++) { $out[$byFull[$batch[$k]]] = "$($shas[$k])".Trim() }
    }
    return ,$out
}

function Get-DGRemoved {
    # Paths that will be gone, measured from $Base, each with the blob it held
    # there - what a move would have to carry somewhere else intact.
    param([string]$Base)
    $out = New-Object 'System.Collections.Generic.List[object]'
    $tokens = @(Split-DGZ (Invoke-DGGit @('diff', '--raw', '-z', '--no-renames', '--no-abbrev',
                                          '--diff-filter=D', $Base, '--') "diff --raw D $Base"))
    $i = 0
    while ($i -lt $tokens.Count) {
        $meta = $tokens[$i]
        if ($meta.StartsWith(':') -and ($i + 1) -lt $tokens.Count) {
            # ":<old mode> <new mode> <old blob> <new blob> D"
            $fields = $meta.TrimStart(':').Split(' ')
            $out.Add(@($tokens[$i + 1], $fields[2]))
            $i += 2
        } else {
            $i += 1
        }
    }
    return ,$out
}

function Get-DGArrivals {
    # The blob of every file that will exist after this publish but was not
    # there at one of $Bases: added and tracked, or new on the disk and not
    # ignored - exactly what `git add -A` is about to take in. A file that
    # disappears while its exact content arrives here was MOVED.
    param([string]$Root, [string[]]$Bases)
    $paths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($b in $Bases) {
        foreach ($p in @(Get-DGChanged $b 'A')) { [void]$paths.Add($p) }
    }
    foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z', '--others', '--exclude-standard') 'ls-files --others'))) {
        [void]$paths.Add($p)
    }
    $blobs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    if ($paths.Count -gt 0) {
        $now = Get-DGWorkingBlobs -Root $Root -Paths @($paths)
        foreach ($v in $now.Values) { [void]$blobs.Add($v) }
    }
    return ,$blobs
}

function Get-DestructiveChangesIn {
    param([string]$Root, [string]$Upstream)
    $ErrorActionPreference = 'Continue'
    $head = git rev-parse -q --verify HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $head) { return @{ Deleted = @(); Reverted = @() } }
    $head = "$head".Trim()
    $base = git merge-base HEAD $Upstream 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $base) { $base = $head } else { $base = "$base".Trim() }

    # Gone, with the content each one held - from this machine's last commit,
    # and (below) from what the team has.
    $removed = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    $gone = { param($pair)
        if (-not $removed.ContainsKey($pair[0])) {
            $removed[$pair[0]] = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        }
        [void]$removed[$pair[0]].Add($pair[1])
    }
    foreach ($pair in (Get-DGRemoved 'HEAD')) { & $gone $pair }
    # Changed on the disk since this machine's last commit: judged against
    # every version this machine has ever held.
    $uncommitted = New-Object 'System.Collections.Generic.List[string]'
    foreach ($p in @(Get-DGChanged 'HEAD' 'M')) { $uncommitted.Add($p) }
    # Committed here but not yet sent: judged against every version the TEAM
    # has held, so an edit that is merely unpublished is never taken for a
    # roll-back of itself.
    $committedOnly = New-Object 'System.Collections.Generic.List[string]'
    if ($base -ne $head) {
        foreach ($pair in (Get-DGRemoved $base)) { & $gone $pair }
        $readded = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($p in @(Get-DGChanged 'HEAD' 'A')) { [void]$readded.Add($p) }
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($p in $uncommitted) { [void]$seen.Add($p) }
        foreach ($p in @(Get-DGChanged $base 'M')) {
            if (-not $seen.Add($p)) { continue }
            # Removed by a local commit and then put back on the disk: that is
            # uncommitted work like any other edit.
            if ($readded.Contains($p)) { $uncommitted.Add($p) } else { $committedOnly.Add($p) }
        }
    }

    $now = Get-DGWorkingBlobs -Root $Root -Paths (@($uncommitted) + @($committedOnly))
    $reverted = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $group = @($uncommitted | Where-Object { $now.ContainsKey($_) })
    if ($group.Count -gt 0) {
        $hist = Get-DGVersions 'HEAD' $group
        foreach ($p in $group) { if ($hist[$p].Contains($now[$p])) { [void]$reverted.Add($p) } }
    }
    $group = @($committedOnly | Where-Object { $now.ContainsKey($_) })
    if ($group.Count -gt 0) {
        $hist = Get-DGVersions $base $group
        foreach ($p in $group) { if ($hist[$p].Contains($now[$p])) { [void]$reverted.Add($p) } }
    }

    # A deletion is excused only when EVERY version it takes away arrives
    # intact under another name: moved, not destroyed. Moved and changed -
    # or changed here before it moved, so the team's version is not what
    # arrives - still asks.
    $deleted = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    if ($removed.Count -gt 0) {
        $bases = if ($base -ne $head) { @('HEAD', $base) } else { @('HEAD') }
        $arrived = Get-DGArrivals -Root $Root -Bases $bases
        foreach ($p in $removed.Keys) {
            foreach ($blob in $removed[$p]) {
                if (-not $arrived.Contains($blob)) { [void]$deleted.Add($p); break }
            }
        }
    }

    $d = [string[]]@($deleted)
    [Array]::Sort($d, [StringComparer]::Ordinal)
    $r = [string[]]@($reverted)
    [Array]::Sort($r, [StringComparer]::Ordinal)
    return @{ Deleted = $d; Reverted = $r }
}

function Get-DestructiveSignature {
    # Identifies one particular set of destructive changes, so a confirmation
    # covers what the person actually looked at and nothing else. Delete one
    # more file afterwards and the signature changes, so it is asked again.
    param($Changes)
    # ORDINAL sort, deliberately: the window computes this same signature in
    # Python, and PowerShell's default Sort-Object is culture-aware. Two
    # spellings of "sorted" would make the app and the engine disagree about
    # which accident the person confirmed - silently, and only for names where
    # the cultures differ.
    $all = [Collections.Generic.List[string]]@(@($Changes.Deleted) + @($Changes.Reverted))
    if ($all.Count -eq 0) { return '' }
    $all.Sort([StringComparer]::Ordinal)
    $text = ($all -join "`n")
    $sha  = [Security.Cryptography.SHA1]::Create()
    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))) -replace '-', '').Substring(0, 12)
}

# --- large new content ------------------------------------------------------
# The second thing a publish waits for a person's word on: something BIG
# about to go out for the first time. Measured on the first real project: the
# folder was 144 MB and 124 MB of it was node_modules - kept home only because
# the project's own .gitignore happened to say so. Without that line every
# byte would have gone, into a history that never forgets, and the next
# `npm install` anywhere in any project would publish within four minutes.
#
# "Going out" means what the upload really carries, in two parts:
#
#   on the disk - new files that are not ignored, and files added or changed
#                 since this machine's last commit. Sized from the disk.
#   in commits  - every file version inside a commit made here that the team
#                 does not have yet. A push sends commits, not the folder's
#                 final state: something committed by hand and then deleted
#                 still travels, inside the first commit.
#
# Content the team already has uploads nothing - a moved or copied file is
# only a new name - so a file whose exact content the team already holds, or
# that one of these commits already carries, does not count again.
#
# Each piece is filed under an ITEM, the thing a person would name: the
# outermost folder the team does not have yet (all of node_modules/ is ONE
# item, however many small files it holds, and however it got here), or else
# the file itself. "Keep it home" names exactly that and nothing above it. An
# item is held when one file in it is 25 MB or more (GitHub warns at 50 and
# refuses at 100), or when it adds 50 MB or more in all.
#
# Shared marks a file the team already has: only its new version would go,
# and keeping it home would mean taking it out of the project for everybody.
# Committed marks content inside a commit made here, which keeping it home
# has to take back out of that commit.
$script:DG_FileLimit  = 25MB
$script:DG_GroupLimit = 50MB
$script:DG_TeamTree   = $null

function Get-DGTeamTree {
    # What the team has at $Rev: its folders, the content id of each file,
    # and the size of each content id. Read once per version of the shared
    # branch - it changes only when somebody publishes.
    param([string]$Rev)
    if ($script:DG_TeamTree -and $script:DG_TeamTree.Rev -eq $Rev) { return $script:DG_TeamTree }
    $dirs  = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $files = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    $sizes = New-Object 'System.Collections.Generic.Dictionary[string,long]' ([StringComparer]::Ordinal)
    if ($Rev) {
        foreach ($t in @(Split-DGZ (Invoke-DGGit @('ls-tree', '-r', '-t', '-l', '-z', $Rev) "ls-tree $Rev"))) {
            # "<mode> <type> <id> <size>`t<path>"
            $tab = $t.IndexOf("`t")
            if ($tab -lt 0) { continue }
            $f = $t.Substring(0, $tab) -split ' +'
            $path = $t.Substring($tab + 1)
            if ($f[1] -eq 'tree') { [void]$dirs.Add($path) }
            elseif ($f[1] -eq 'blob') { $files[$path] = $f[2]; $sizes[$f[2]] = [long]$f[3] }
        }
    }
    $script:DG_TeamTree = @{ Rev = $Rev; Dirs = $dirs; Files = $files; Sizes = $sizes }
    return $script:DG_TeamTree
}

function Get-DGCommitted {
    # Every file version inside commits reachable from $Tip that the team
    # does not have - what a push of $Tip carries besides the disk. Each
    # content id once, under the first name git reports for it.
    param([string]$Tip, [string]$Upstream)
    $range = if ($Upstream) { @($Tip, '--not', $Upstream) } else { @($Tip) }
    $named = New-Object 'System.Collections.Generic.List[object]'
    foreach ($line in @(Invoke-DGGit (@('rev-list', '--objects') + $range + @('--')) 'rev-list --objects')) {
        # "<id> <path>"; a commit has no path, the top folder an empty one
        $line = "$line"
        $sp = $line.IndexOf(' ')
        if ($sp -gt 0 -and $sp -lt ($line.Length - 1)) { $named.Add(@($line.Substring(0, $sp), $line.Substring($sp + 1))) }
    }
    $out = New-Object 'System.Collections.Generic.List[object]'
    if ($named.Count -eq 0) { return ,$out }
    $ErrorActionPreference = 'Continue'
    $info = @($named | ForEach-Object { $_[0] } |
              & git cat-file '--batch-check=%(objectname) %(objecttype) %(objectsize)' 2>$null)
    if ($LASTEXITCODE -ne 0 -or $info.Count -ne $named.Count) {
        throw 'git could not answer the destructive check (cat-file sizes)'
    }
    for ($k = 0; $k -lt $named.Count; $k++) {
        $f = "$($info[$k])".Split(' ')
        if ($f.Count -ge 3 -and $f[1] -eq 'blob') {
            $out.Add(@{ Path = $named[$k][1]; Blob = $f[0]; Size = [long]$f[2]; Committed = $true })
        }
    }
    return ,$out
}

function Get-LargeOutgoingIn {
    # Large content about to leave: one @{Item; Bytes; Files; Shared;
    # Committed} per held item, ordinal by item. $Tip other than HEAD, with
    # -CommittedOnly, measures what pushing that commit would carry.
    param([string]$Root, [string]$Upstream, [string]$Tip = 'HEAD', [switch]$CommittedOnly)
    $ErrorActionPreference = 'Continue'
    $tipId = git rev-parse -q --verify "$Tip^{commit}" 2>$null
    $tipId = if ($LASTEXITCODE -eq 0 -and $tipId) { "$tipId".Trim() } else { '' }
    $upId = git rev-parse -q --verify "$Upstream^{commit}" 2>$null
    $upId = if ($LASTEXITCODE -eq 0 -and $upId) { "$upId".Trim() } else { '' }

    $pieces = New-Object 'System.Collections.Generic.List[object]'
    if ($tipId) { foreach ($c in (Get-DGCommitted $tipId $upId)) { $pieces.Add($c) } }
    if (-not $CommittedOnly) {
        $disk = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z', '--others', '--exclude-standard') 'ls-files --others'))) {
            [void]$disk.Add($p)
        }
        if ($tipId) {
            foreach ($letter in 'A', 'M') { foreach ($p in @(Get-DGChanged 'HEAD' $letter)) { [void]$disk.Add($p) } }
        } else {
            # Nothing committed yet: everything staged goes in the first commit.
            foreach ($p in @(Split-DGZ (Invoke-DGGit @('ls-files', '-z') 'ls-files'))) { [void]$disk.Add($p) }
        }
        foreach ($p in $disk) {
            # .NET rather than Test-Path/Get-Item: a folder nobody ignored can
            # hold fifty thousand files, and this runs on every publish.
            try { $fi = New-Object IO.FileInfo ([IO.Path]::Combine($Root, $p)) } catch { continue }
            if ($fi.Exists) { $pieces.Add(@{ Path = $p; Blob = $null; Size = [long]$fi.Length; Committed = $false }) }
        }
    }
    if ($pieces.Count -eq 0) { return }

    $team = Get-DGTeamTree $upId
    $items = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    $under = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    foreach ($pc in $pieces) {
        $path = $pc.Path
        $shared = $team.Files.ContainsKey($path)
        $item = $path
        $cut = $path.LastIndexOf('/')
        if (-not $shared -and $cut -gt 0) {
            # The outermost folder the team does not have - once per folder.
            $dir = $path.Substring(0, $cut)
            if (-not $under.ContainsKey($dir)) {
                $owner = ''
                $acc = ''
                foreach ($part in $dir.Split('/')) {
                    $acc = if ($acc) { "$acc/$part" } else { $part }
                    if (-not $team.Dirs.Contains($acc)) { $owner = "$acc/"; break }
                }
                $under[$dir] = $owner
            }
            if ($under[$dir]) { $item = $under[$dir] }
        }
        if (-not $items.ContainsKey($item)) {
            $items[$item] = @{ Item = $item; Shared = $shared; Pieces = New-Object 'System.Collections.Generic.List[object]' }
        }
        $items[$item].Pieces.Add($pc)
    }

    $measure = {
        param($it)
        $total = [long]0; $biggest = [long]0; $committed = $false
        $paths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($pc in $it.Pieces) {
            if ($pc.Known) { continue }
            $total += $pc.Size
            if ($pc.Size -gt $biggest) { $biggest = $pc.Size }
            [void]$paths.Add($pc.Path)
            if ($pc.Committed) { $committed = $true }
        }
        @{ Total = $total; Big = ($biggest -ge $script:DG_FileLimit -or $total -ge $script:DG_GroupLimit)
           Files = $paths.Count; Committed = $committed }
    }
    $candidates = @($items.Values | Where-Object { (& $measure $_).Big })
    if ($candidates.Count -eq 0) { return }

    # Already there, or already on its way. Reading a file's content id means
    # reading all of it, so a file is read only when that could change the
    # answer: its size matches something known, AND without it the item
    # would no longer be large. A folder of fresh downloads stays large
    # whatever a few of its files turn out to be, and costs no reading.
    $knownIds = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $knownSizes = New-Object 'System.Collections.Generic.HashSet[long]'
    foreach ($kv in $team.Sizes.GetEnumerator()) { [void]$knownIds.Add($kv.Key); [void]$knownSizes.Add($kv.Value) }
    foreach ($pc in $pieces) { if ($pc.Committed) { [void]$knownIds.Add($pc.Blob); [void]$knownSizes.Add($pc.Size) } }
    $toHash = New-Object 'System.Collections.Generic.List[string]'
    foreach ($it in $candidates) {
        $rest = [long]0; $restBig = [long]0
        $maybe = New-Object 'System.Collections.Generic.List[string]'
        foreach ($pc in $it.Pieces) {
            if ($pc.Committed) {
                if ($team.Sizes.ContainsKey($pc.Blob)) { $pc.Known = $true; continue }
            } elseif ($knownSizes.Contains($pc.Size)) {
                $maybe.Add($pc.Path); continue
            }
            $rest += $pc.Size
            if ($pc.Size -gt $restBig) { $restBig = $pc.Size }
        }
        if ($maybe.Count -gt 0 -and $restBig -lt $script:DG_FileLimit -and $rest -lt $script:DG_GroupLimit) {
            $toHash.AddRange($maybe)
        }
    }
    if ($toHash.Count -gt 0) {
        $now = Get-DGWorkingBlobs -Root $Root -Paths @($toHash)
        foreach ($it in $candidates) {
            foreach ($pc in $it.Pieces) {
                if (-not $pc.Committed -and $now.ContainsKey($pc.Path) -and $knownIds.Contains($now[$pc.Path])) { $pc.Known = $true }
            }
        }
    }

    $held = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    foreach ($it in $candidates) {
        $m = & $measure $it
        if ($m.Big) {
            $held[$it.Item] = @{ Item = $it.Item; Bytes = $m.Total; Files = $m.Files
                                 Shared = $it.Shared; Committed = $m.Committed }
        }
    }
    # One item per pipeline object: callers collect them with @(...), which
    # gives an empty array for none - never one nested array counted as one.
    $names = [string[]]@($held.Keys)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    foreach ($n in $names) { $held[$n] }
}

function Get-LargeSignature {
    # One particular set of large items, so "send them" covers only those.
    param($Large)
    $all = New-Object 'System.Collections.Generic.List[string]'
    foreach ($l in @($Large)) { if ($l -and $l.Item) { $all.Add($l.Item) } }
    if ($all.Count -eq 0) { return '' }
    $all.Sort([StringComparer]::Ordinal)
    $sha = [Security.Cryptography.SHA1]::Create()
    return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($all -join "`n")))) -replace '-', '').Substring(0, 12)
}
# <<< destructive guard

try {
    $held = Get-DestructiveChangesIn -Root $repo -Upstream 'refs/remotes/origin/main'
    $large = @(Get-LargeOutgoingIn -Root $repo -Upstream 'refs/remotes/origin/main')
} catch {
    Say "Could not check whether this would delete or roll back work: $($_.Exception.Message)" 'Red'
    Say 'Nothing was published. Run it again in a moment; if it keeps failing, tell your human.' 'Yellow'
    exit 1
}
if ($CheckOnly) {
    foreach ($f in $held.Deleted)  { Write-Output "deleted: $f" }
    foreach ($f in $held.Reverted) { Write-Output "rolled back: $f" }
    Write-Output ('signature: ' + (Get-DestructiveSignature $held))
    foreach ($l in $large) {
        $what = if ($l.Shared) { ', already in the project' } elseif ($l.Committed) { ', inside a commit made here' } else { '' }
        Write-Output ("large: {0} ({1} bytes, {2} files{3})" -f $l.Item, $l.Bytes, $l.Files, $what)
    }
    Write-Output ('large signature: ' + (Get-LargeSignature $large))
    exit 0
}
# Large content waits for a person too: once sent it is in the history for
# good, and most of what is that big (installed libraries, caches, build
# output) can be rebuilt on each machine instead.
if ($large.Count -gt 0 -and (git config --local --get teamsync.largeok 2>$null) -ne (Get-LargeSignature $large)) {
    Say 'This would upload LARGE content. Nothing was published.' 'Red'
    foreach ($l in ($large | Select-Object -First 12)) {
        $what = if ($l.Shared) { ', a new version of a file the team has' } elseif ($l.Committed) { ', inside a commit made here' } else { '' }
        $mb = ($l.Bytes / 1MB).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
        Say ("  {0} ({1} MB{2})" -f $l.Item, $mb, $what) 'Yellow'
    }
    if ($large.Count -gt 12) { Say "  ... and $($large.Count - 12) more" 'Yellow' }
    Say ''
    Say 'Keep it home or send it: TeamSync window > Needs your OK. Then run this again.' 'Yellow'
    Say 'Agents: this is a decision for your human.' 'Yellow'
    exit 3
}
$destroyed = @(@($held.Deleted | ForEach-Object { "deleted: $_" }) +
               @($held.Reverted | ForEach-Object { "rolled back: $_" }))
if ($destroyed.Count -gt 0) {
    if ((git config --local --get teamsync.destructiveok 2>$null) -ne (Get-DestructiveSignature $held)) {
        Say 'This would DESTROY work on everybody machine. Nothing was published.' 'Red'
        foreach ($d in ($destroyed | Select-Object -First 12)) { Say "  $d" 'Yellow' }
        if ($destroyed.Count -gt 12) { Say "  ... and $($destroyed.Count - 12) more" 'Yellow' }
        Say ''
        Say 'If it was a mistake, put them back: TeamSync window > Needs your OK > Put them back.' 'Yellow'
        Say 'If you meant it, confirm it there instead (Publish these), then run this again.' 'Yellow'
        Say 'Agents: this is a decision for your human.' 'Yellow'
        exit 3
    }
}

# Is the sync engine running and listening?
#
# This used to be decided by the heartbeat's timestamp alone, with no check on
# the process at all. The engine writes that timestamp once per pass, and a
# pass with many network round trips can take longer than the 30 seconds this
# allowed - so a perfectly healthy engine was declared dead and the fallback
# below started publishing DIRECTLY, on top of an engine mid-commit or
# mid-rebase. `git add -A` at that moment stages the other process's
# half-applied conflict markers as if they were content.
#
# So the process decides. The pid alone would not be enough - Windows reuses
# ids - which is why the engine records its start time in the lock and this
# compares both. Only a lock written before that scheme falls back to the old
# timestamp rule.
$daemonAlive = $false
$daemonPid = 0
if (Test-Path -LiteralPath $lock) {
    $lines = @(Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue)
    $get = { param($k) (($lines | Where-Object { $_ -like "$k=*" }) -replace "^$k=", '') }
    $lockPid = & $get 'pid'
    $recorded = & $get 'started'
    if ($lockPid) {
        $proc = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
        if ($proc) {
            if ($recorded) {
                $actual = ''
                try { $actual = $proc.StartTime.ToString('o') } catch { }
                if ($actual -and $actual -eq $recorded) { $daemonAlive = $true; $daemonPid = [int]$lockPid }
            } else {
                $t = & $get 'time'
                if ($t) {
                    try {
                        if (((Get-Date) - [datetime]::Parse($t)).TotalSeconds -lt 30) {
                            $daemonAlive = $true; $daemonPid = [int]$lockPid
                        }
                    } catch { }
                }
            }
        }
    }
}

# A rebase in progress belongs to whoever started it. Publishing into one
# means committing somebody else's unfinished merge, so refuse outright
# rather than choosing a side.
$gitDir = git rev-parse --git-dir 2>$null
if ($gitDir -and ((Test-Path -LiteralPath (Join-Path $gitDir 'rebase-merge')) -or
                  (Test-Path -LiteralPath (Join-Path $gitDir 'rebase-apply')))) {
    Say 'A merge is in progress in this folder. Nothing was published.' 'Red'
    Say 'Wait for it to finish, or resolve it, then run this again.' 'Yellow'
    exit 1
}

if ($daemonAlive) {
    # Nothing to commit and nothing to send: say that truthfully instead of
    # "Published." - and clear any work-in-progress announcement, because
    # finished-with-nothing-left is still finished.
    if (@(git status --porcelain 2>$null).Count -eq 0 -and
        (git rev-list --count 'origin/main..HEAD' 2>$null) -eq '0') {
        Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
        Say 'Nothing to publish - everything is already out.' 'Green'
        exit 0
    }

    Say 'Asking the sync app to publish now...' 'Cyan'
    New-Item -ItemType File -Path $signal -Force | Out-Null

    # Wait on EVIDENCE, not on a clock.
    #
    # A fixed two-minute deadline reported "the sync app did not answer" while
    # the engine published the very same work thirty seconds later. Measured,
    # on a live test, with the partner's agent waiting on the result. For an
    # agent that is the worst answer available: it says UNDONE about work that
    # is done, and the obvious next move - run it again - is exactly wrong.
    #
    # A pass with several network round trips can take minutes, especially on
    # a machine that has just woken. But the engine stamps its heartbeat every
    # pass, so "still working" is observable. Keep waiting while that keeps
    # moving; stop when it stops, which is the honest end of the story.
    function Get-Beat {
        foreach ($l in (Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue)) {
            if ($l -like 'time=*') {
                $t = [datetime]::MinValue
                if ([datetime]::TryParse($l.Substring(5), [ref]$t)) { return $t }
            }
        }
        return [datetime]::MinValue
    }
    $lastBeat  = Get-Beat
    $beatSeen  = Get-Date
    $ceiling   = (Get-Date).AddSeconds([Math]::Max($TimeoutSeconds, 600))
    $stalled   = $false
    $taken = $false
    while ((Get-Date) -lt $ceiling) {
        Start-Sleep -Milliseconds 500

        $beat = Get-Beat
        if ($beat -gt $lastBeat) { $lastBeat = $beat; $beatSeen = Get-Date }
        if (((Get-Date) - $beatSeen).TotalSeconds -gt 90) { $stalled = $true; break }

        # The engine writes its network state into its heartbeat every second.
        # Offline is an answer, not something to time out on for two minutes.
        if ((Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue) -contains 'net=offline') {
            Say 'The app is offline (VPN or network). Nothing was sent - and nothing is lost:' 'Yellow'
            Say 'your work is committed, and the engine publishes it by itself once the connection returns.' 'Yellow'
            exit 1
        }

        if (-not $taken) {
            # Phase 1: has the app picked the request up? It deletes the signal file.
            if (-not (Test-Path -LiteralPath $signal)) { $taken = $true }
            continue
        }

        # Phase 2: wait for the OUTCOME. A push to GitHub over a VPN takes seconds,
        # and how many is not predictable - so poll for the finished state instead
        # of sleeping a fixed amount and guessing.
        if (@(git diff --name-only --diff-filter=U 2>$null).Count -gt 0) {
            Say 'Conflict while publishing. Nothing was pushed, nothing was lost.' 'Red'
            Say 'Look in _conflicts\ for both versions side by side.' 'Yellow'
            exit 2
        }
        # Done means both: nothing left to commit, and nothing left to send.
        # Checking only the second would report success before the app has
        # committed the work at all.
        $dirty = @(git status --porcelain 2>$null).Count
        $ahead = git rev-list --count 'origin/main..HEAD' 2>$null
        if ($dirty -eq 0 -and $ahead -eq '0') {
            # Published means the announced work is out - clear the announcement
            # here too; only the direct-push path used to do this, and the
            # daemon path is the one that normally runs.
            Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
            Say 'Published.' 'Green'; exit 0
        }
    }

    # Before saying anything failed, LOOK. The engine may have finished in the
    # gap between the last poll and here, and reporting failure over finished
    # work is the fault this whole block exists to prevent.
    if (@(git status --porcelain 2>$null).Count -eq 0 -and
        (git rev-list --count 'origin/main..HEAD' 2>$null) -eq '0') {
        Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
        Say 'Published.' 'Green'; exit 0
    }

    if ($stalled) {
        Say 'The sync app stopped responding - its heartbeat has not moved for 90s.' 'Yellow'
        Say 'Nothing is lost: your work is on disk. Check the app window.' 'Yellow'
    } elseif ($taken) {
        Say "The sync app took the request and is still working after $([int]((Get-Date) - $beatSeen).TotalSeconds)s." 'Yellow'
        Say 'Your work is on disk and the engine publishes it by itself. Do not run this in a loop.' 'Yellow'
    } else {
        Say 'The sync app did not answer. Check its window.' 'Yellow'
    }
    exit 1
}

# No daemon: do it here. Same steps, without the side-by-side conflict export.
Say 'The sync app is not running, publishing directly...' 'Yellow'
git add -A 2>$null | Out-Null
if (@(git diff --cached --name-only 2>$null).Count -gt 0) {
    git commit -q -m "sync: $(Get-Date -Format 'MM-dd HH:mm:ss')" 2>$null | Out-Null
}
$ahead = git rev-list --count 'origin/main..HEAD' 2>$null
if ($ahead -eq '0' -or -not $ahead) { Say 'Nothing to publish.' 'Green'; exit 0 }

git fetch -q origin main 2>$null
if ($LASTEXITCODE -ne 0) { Say 'Could not reach GitHub. Check the network or VPN.' 'Yellow'; exit 1 }

git rebase origin/main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say 'Conflict. Nothing was pushed, nothing was lost.' 'Red'
    Say 'Start the sync app to get both versions saved side by side,' 'Yellow'
    Say 'or resolve the markers in the files and run: git add . ; git rebase --continue' 'Yellow'
    Say 'To back out entirely: git rebase --abort' 'Yellow'
    exit 2
}

git push -q origin main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Say 'Push failed. Check the network or VPN, then try again.' 'Yellow'; exit 1 }
# A "yes" covers the publish it was given for, and nothing after it.
git config --local --unset teamsync.destructiveok 2>$null | Out-Null
git config --local --unset teamsync.largeok 2>$null | Out-Null
# Published means the work is out - whatever was announced as "in progress"
# is in progress no longer.
Remove-Item -LiteralPath (Join-Path $repo '.teamsync-agent.json') -Force -ErrorAction SilentlyContinue
Say "Published $ahead commit(s)." 'Green'
exit 0

} finally {
    if ($script:ConsoleEncodingWas) { try { [Console]::OutputEncoding = $script:ConsoleEncodingWas } catch { } }
}
