HOW TEAMSYNC WORKS, PIECE BY PIECE

The idea
  Every machine holds a full copy of the project. A background engine watches
  your folder, publishes your work to a private GitHub repository, and brings
  everybody else's work in - usually within a few seconds. Two people or ten;
  nothing here assumes there are only two of you.

The lights (top right)
  everything published      all your work is on GitHub
  N not published yet       press Publish now, or the 4-minute timer sends it
  offline, retrying         no network; NOTHING is lost, it keeps trying
  conflict (red)            see "Conflicts" below
  who is here               up to five people online are named, each with its
                            own light: green is syncing now, grey is away with
                            when they were last seen. Past five it shows the
                            count - hover it for up to ten, click it for
                            everyone. With more than ten online, the ten shown
                            are the busiest of the last few hours.
  "NAME is working on ..."  a standing amber line, one entry per person: they
                            have unpublished work on those files right now.
                            Not a lock - a warning.
  "NAME is resolving a
   conflict in ..."         red, and it outranks the amber line. Their machine
                            is untangling that file; leave it alone until the
                            fixed version arrives. Nothing of yours is blocked.

The buttons
  Publish now      send finished work immediately (AI agents use push-now.ps1)
  Stop sync        pause the engine (closing the window does NOT stop it)
  Open folder      the project in Explorer
  Change folder    move the project somewhere else, safely, while syncing
  History          older log lines - the live log keeps about a day and
                   never fewer than the last 100 lines
  Conflicts        everything stuck on the project, whoever it belongs to
  Add people       invite more people to this project, later than day one
  Switch project   another shared project
  Disconnect       remove this project from the app; files and GitHub stay

Publishing
  Work is committed the moment it is saved, published when you press the
  button - or 4 minutes after the last change, as a backstop. Half-finished
  files travel too, on purpose; keep secrets and not-yet-shareable things
  outside the project folder.

  While anybody is resolving a conflict, that 4-minute backstop WAITS, so the
  ground does not move under them. Publish now still sends immediately - that
  is your decision, and decisions stay with the people. If a conflict is left
  open for ten minutes the backstop starts again by itself.

Conflicts (red light)
  Your work and the shared branch changed the same lines. Nothing was pushed,
  nothing was lost. A folder opens with three copies of each file:
    <path>.MINE.ext    yours
    <path>.THEIRS.ext  the version from GitHub. That is the shared branch, so
                       with more than two people it can already hold SEVERAL
                       teammates' work - CONFLICT.md names who is in it.
    <path>.BASE.ext    the version everybody started from
  Keep EVERY intent listed there, not two. Fix the live file, then:
    git add . ; git rebase --continue
  Sync resumes by itself. Resolving one file can raise the conflict for the
  next commit; a new folder appears for that one too.

  Somebody else's conflict: the Conflicts button lists them all and writes out
  all three versions of anybody's. Only their machine can finish their own
  merge, but you can decide what the file should say and publish it - their
  side takes your version when it arrives. Tick the box to say you are doing
  it, so two people do not settle the same file twice.

Crossed edits (a warning, not a stop)
  Two of you changed the SAME FILE at the same time, but different lines.
  The texts merge cleanly and nothing stops - but neither edit saw the other,
  so both originals are kept in _conflicts\...-crossed\ and an alert asks you
  to give the merged file one human look.

While a file is under change here (saved-but-unpublished work, unsaved typing
in a supported editor, or an AI agent's announcement), incoming versions of
that file WAIT, and land right after you publish.

Names
  Everyone must publish under a different name (git config user.name) - it is
  what ties presence, warnings and authorship together. The app refuses to
  start on a name already held by a different GitHub account. One person on
  two machines is not a collision: the second is numbered automatically, so
  "amin" and "amin-2" are one person at two desks.

Updates
  The app offers a new version with its size and date - NOTHING downloads
  until you press the button. What arrives is signature-checked against a key
  built into the app, then handed to your own antivirus, then swapped in by a
  helper that puts the old version back if anything fails.
