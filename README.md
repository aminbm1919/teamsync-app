# TeamSync

A shared folder for a small team on Windows, kept in step through a private
GitHub repository - built for teams where each person drives an AI agent
(Claude Code, Codex, or any other) and nobody wants to think about git.

Change a file on one machine and it is on the others in a few seconds. Work
offline and it catches up by itself. Edit the same lines and nothing is lost:
every version is laid side by side and sync pauses until a human (or an
agent) combines them.

Two people or ten: nothing in it assumes there are only two of you.

## What it does

- **Live sync** - publishes your work and brings in everybody else's, usually
  within 1-3 seconds, via cheap conditional requests that cost no API quota.
- **A visible team** - up to five people online are named with their own
  lights; past that it shows the count, with the names a hover away and the
  whole team a click away. An amber line names the files each person has
  unpublished work on right now, from the moment their hands touch them.
- **Read-side safety** - a file you are mid-change on is never replaced under
  your hands; incoming versions wait until you publish.
- **Honest conflicts** - same-line edits stop with every version saved
  (MINE / THEIRS / BASE) plus a plain-language note naming who is actually
  inside the incoming side; same-file different-line edits merge cleanly but
  are announced, with the originals kept.
- **Conflicts are the team's business** - everyone is told who is stuck and
  on which file, and can read all three versions of it. Only the stuck
  machine can finish its own merge, but anyone may settle the text and
  publish it; a tick box marks who volunteered, so two people never settle
  the same file twice. While somebody resolves, automatic publishing stands
  back - a deliberate publish never does.
- **Agents as first-class teammates** - three commands (`who.ps1`,
  `working.ps1`, `push-now.ps1`) let any AI agent see who is there, announce
  what it is about to change, and publish; a complete standing prompt ships
  in the app (Help - AI prompt) and in `agent-bundle/`.
- **Safe self-update** - consent-first (nothing downloads until you click),
  ed25519-signed packages verified against a key built into the app, handed
  to your own antivirus, swapped by a helper that rolls back on failure.

## Install

1. Grab `TeamSync.zip` from the newest [release](../../releases), extract it
   to a normal folder such as `C:\Apps\TeamSync` (not Program Files), run
   `TeamSync.exe`.
2. Press **Help** in the app - the first tab walks through the once-per-machine
   setup (git, GitHub CLI, sign-in) and how to start or join a project.

Docs, outside the app: [getting connected](docs/getting-connected.md) ·
[how it works](docs/how-it-works.md) · [the AI agent prompt](docs/agent-prompt.md)

## Bring your agent

`agent-bundle/` has a ready-made Claude Code skill and a Codex `AGENTS.md`
section - one copy step per machine, and your agent knows the whole protocol:
announce before writing, read freely, publish when done, resolve conflicts
keeping EVERY intent inside them, and help the humans divide the work (the
people always hold the final decision).

Both files are generated from the app's own prompt by
`agent-bundle/regenerate.ps1`, which also refreshes the copy inside
`TEAM-PROJECT-REFERENCE.md`. Edit the asset and run it; never edit the
copies.

## Build from source

```
pwsh build.ps1
```

Needs Python 3 with PyInstaller (`pip install pyinstaller sv-ttk`) and
produces `dist\TeamSync\` plus the distributable `dist\TeamSync.zip`.
Releases are published with `publish-release.ps1`, which refuses to publish
anything it cannot sign and verify; the signing key never leaves the
maintainer's machine.

## License

MIT - see [LICENSE](LICENSE).
