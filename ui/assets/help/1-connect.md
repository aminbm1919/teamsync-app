GETTING CONNECTED

What you need, once per machine
  1. Install git and GitHub CLI:
       winget install --id Git.Git -e --source winget
       winget install --id GitHub.cli -e --source winget
     Then CLOSE PowerShell and open a new one, or Windows will not see them.
  2. Sign in to GitHub:  gh auth login
     (choose GitHub.com, HTTPS, then "Login with a web browser")
  3. Keep your VPN on while working, if GitHub needs one where you are.
  4. Put TeamSync in a normal folder such as C:\Apps\TeamSync
     - NOT Program Files, Windows protects it and updates cannot install there.

Your name and your GitHub username are filled in for you, from the account
you signed in to above and from git's own settings. Change them if they are
wrong; you are not asked for them twice.

Starting a new shared project (the owner)
  1. Create the folder and do the first real work in it.
  2. Press "Start a new shared project", pick the folder, give the repository
     a lower-case latin name.
  3. Choose who to share with. Everyone you have shared with before is listed
     with a tick box - tick as many as the project is for. For somebody new,
     type their GitHub username. You can also invite nobody now and add people
     later.
     The list is a history: sharing with somebody once keeps them there for
     next time. The x beside a name takes them out of the list - that changes
     only this list on this machine, removes nobody from any project, and
     inviting them again puts them back.
  4. The repository is created PRIVATE. Only the people you invite can see it.
     Everyone you invite gets a GitHub invitation, which appears under
     "Requests received" in THEIR TeamSync.
  5. Later on, "Add people" invites more of them to the project that is already
     running - a team does not have to be complete on day one. Only whoever
     created the project can invite; for anybody else the button says so.

Everybody needs a different name
  Presence, the "working on this file" warnings and who wrote what are all tied
  together by your name (git config user.name). Two PEOPLE sharing one name
  would erase each other's warnings, so the app refuses to start on a name that
  belongs to a different GitHub account, and says what to change.
  One person on two computers is not a collision: the second is numbered by
  itself, so "amin" and "amin-2" are the same person at two desks.

Joining a project someone shared with you
  1. Open TeamSync. When somebody has invited you, a number appears on
     "Join a project someone shared with me" - and if you are already inside
     a project, on the "Requests received" button on the top line beside Help,
     so an invitation is never missed just because you were busy working.
     It checks about once a minute and updates on its own.
  2. Press it and choose "Requests received". Each request names who invited
     you and which repository. Accept downloads the project; Decline says no
     and the request disappears for both of you.
  3. On Accept, choose ANY EMPTY folder to download into. Accepting on GitHub
     and downloading happen together.

  If no request appears - nobody has sent one yet, or this machine is signed
  in to a different GitHub account (check with: gh auth status).
  You can still join by hand: "Join a project someone shared with me" offers
  that door too. But a repository you were never invited to answers
  "repository not found", and that is not a typo on your side.

From then on: just open TeamSync. It remembers the project and reconnects.
