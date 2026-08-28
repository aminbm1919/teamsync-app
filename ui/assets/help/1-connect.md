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

Starting a new shared project (the owner)
  1. Create the folder and do the first real work in it.
  2. Press "Start a new shared project", pick the folder, give the repository
     a lower-case latin name, enter YOUR GitHub name and email.
  3. Send your teammate the repository name. They get a GitHub invitation.

Joining a project someone shared with you
  1. Accept the invitation first:
       gh api user/repository_invitations --jq '.[].id' |
         ForEach-Object { gh api -X PATCH "user/repository_invitations/$_" }
  2. Press "Join a project someone shared with me", enter the repository name
     and the owner's GitHub username, choose ANY EMPTY folder to download into.
  3. Enter YOUR OWN GitHub name and email - that is what marks your work
     as yours.

If it says "repository not found", the invitation is not accepted yet.
That is not a typo on your side.

From then on: just open TeamSync. It remembers the project and reconnects.
