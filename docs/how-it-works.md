HOW TEAMSYNC WORKS, PIECE BY PIECE

The idea
  Both machines hold a full copy of the project. A background engine watches
  your folder, publishes your work to a private GitHub repository, and brings
  the other person's work in - usually within a few seconds.

The lights (top right)
  everything published      all your work is on GitHub
  N not published yet       press Publish now, or the 4-minute timer sends it
  offline, retrying         no network; NOTHING is lost, it keeps trying
  conflict (red)            see "Conflicts" below
  partner light             green = their engine is running now,
                            amber = last seen a while ago, grey = never joined
  "NAME is working on: ..." a standing amber line: they have unpublished work
                            on those files right now. Not a lock - a warning.

The buttons
  Publish now      send finished work immediately (AI agents use push-now.ps1)
  Stop sync        pause the engine (closing the window does NOT stop it)
  Open folder      the project in Explorer
  Change folder    move the project somewhere else, safely, while syncing
  History          older log lines - the live log keeps about a day and
                   never fewer than the last 100 lines
  Switch project   another shared project
  Disconnect       remove this project from the app; files and GitHub stay

Publishing
  Work is committed the moment it is saved, published when you press the
  button - or 4 minutes after the last change, as a backstop. Half-finished
  files travel too, on purpose; keep secrets and not-yet-shareable things
  outside the project folder.

Conflicts (red light)
  You both changed the same lines. Nothing was pushed, nothing was lost.
  A folder opens with three copies of each file:
    NAME.MINE.ext    yours     NAME.THEIRS.ext  theirs
    NAME.BASE.ext    what both of you started from
  Fix the live file to carry BOTH intents (an AI agent can do it: "Resolve
  the conflict. Read CONFLICT.md and keep both intents"), then:
    git add . ; git rebase --continue
  Sync resumes by itself.

Crossed edits (a warning, not a stop)
  You both changed the SAME FILE at the same time, but different lines.
  The texts merge cleanly and nothing stops - but neither edit saw the other,
  so both originals are kept in _conflicts\...-crossed\ and an alert asks you
  to give the merged file one human look.

While a file is under change here (saved-but-unpublished work, unsaved typing
in a supported editor, or an AI agent's announcement), incoming versions of
that file WAIT, and land right after you publish.

Updates
  The app offers a new version with its size and date - NOTHING downloads
  until you press the button. What arrives is signature-checked against a key
  built into the app, then handed to your own antivirus, then swapped in by a
  helper that puts the old version back if anything fails.
