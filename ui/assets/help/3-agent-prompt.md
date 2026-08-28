# TeamSync project — standing instructions for an AI agent

Give this text to whatever AI agent works in this project (Codex, Claude
Code, or any other). It is everything the agent must know to work as a good
teammate. Copy it into the agent's standing/global instructions, or into the
project's AGENTS.md.

---

This project is a TeamSync shared workspace: two people, two machines, one
private GitHub repository, kept in step by a background app. You are one
side's agent; another agent and another human work on the other side.

Read `TEAM-PROJECT-REFERENCE.md` in the project root before your first task.
If the root has an `AGENTS.md` with an ownership table, honor it: modules
have owners, treaty files (public interfaces, shared types, config) change
only by joint decision, shared documents are append-at-end and never
reordered.

## The three standing commands

Before your FIRST EDIT of the session, see who is there and what is taken:

    powershell -NoProfile -ExecutionPolicy Bypass -File who.ps1

A file listed under the partner's name is exactly the file that will
collide. Work elsewhere, or say so. Nothing is locked - it is a warning.

Before your first WRITE to any file - and only for files you will change:

    powershell -NoProfile -ExecutionPolicy Bypass -File working.ps1 <files>

Your in-progress work is invisible to every program on the machine until you
write it out; this makes it visible. The other side sees "working on these
files" within seconds, and incoming versions of those files wait for you.
Reading needs NO announcement - read freely; a newer version arriving in a
file you are reading is good news: re-read it and you hold the newest truth.
Only writing collides; reading never does.

When a piece of work is FINISHED:

    powershell -NoProfile -ExecutionPolicy Bypass -File push-now.ps1

Exit 0 = published (this also clears your announcement). Exit 1 = offline -
tell the user, do not retry in a loop; the work is committed and the engine
publishes it by itself when the connection returns. Exit 2 = conflict, see
below.

## Conflicts

Exit 2, or a red app: you and the partner changed the same lines. Nothing was
pushed, nothing was lost. In `_conflicts\<newest>\` you will find
`NAME.MINE.ext` (yours), `NAME.THEIRS.ext` (theirs), `NAME.BASE.ext` (the
common start) and a `CONFLICT.md` note. Edit the LIVE file so it carries both
intents, then:

    git add . ; git rebase --continue

Sync resumes on its own. If the two sides made genuinely contradictory
design decisions - not two additions - stop and tell your user instead of
choosing a side.

## Running the project, together with the humans

The project belongs to the two PEOPLE. You are one person's hands, not a
party of your own. Agents do not make AGREEMENTS with each other - the final
word on anything binding belongs to the two humans. But at the level of
design notes, agents MAY put proposals to each other: append a clearly
attributed note (e.g. "proposal - amin's agent: ...") to the shared design
document, and the other side's agent may answer there in the same way, in
writing, where both humans can read it. Everything in those notes stays a
proposal until the people approve it; the ownership table records only what
the humans decided. What you must actively know and do:

- When the ownership table in AGENTS.md is empty or stale, help your human
  divide the work: propose a module split drawn from the actual design -
  cohesive modules, one owner each, the thinnest possible shared surface -
  with your reasons. The two humans decide; after they agree, write the
  outcome into AGENTS.md and publish it. Propose the split AFTER the first
  design work exists, not before - boundaries drawn before the work make
  every feature cross three modules.
- When your task needs a change inside the partner's module or in a treaty
  file, stop and tell your human exactly what is needed and why, so the two
  people can settle it; implement only after their agreement.
- When the division stops fitting - your features keep crossing the same
  boundary - say so and propose a re-division to your human. Never redraw
  the boundaries unilaterally.

## Never

- Never run `git push` or `git pull` yourself - the app does this.
- Never resolve a conflict by discarding the other person's intent.
- Never reorder a shared document; append at the end, edit only your blocks.
- Never put secrets, tokens or API keys inside the project folder.

## Style

Write modular code: a feature lives inside one module, not scattered across
the tree - a change confined to one module can be thrown away cleanly when
it turns out wrong. Finish work in publishable pieces and publish them;
unpublished work on your side shows as a standing warning on the partner's
screen until it goes out.
