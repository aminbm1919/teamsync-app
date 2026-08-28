# TEAM-PROJECT-REFERENCE.md

**The reference for team projects.** Read this whenever the user says a project is
a team project, or whenever you find this file in a project root.

It is the same file on both machines. A copy lives in every shared project, and
the master copy lives in the toolkit. If they ever disagree, the copy inside the
project wins — it is the one both people are actually working against.

---

## 1. What a team project is

Two people work on the same folder at the same time, on two separate Windows
machines, each driving their own AI agent (Claude Code on one side, Codex on the
other). Both machines hold a full copy. The copies are kept in step through a
**private** GitHub repository.

This changes three things about how you work, and nothing else:

1. Someone else's changes can arrive at any moment.
2. Your changes must be published to be useful.
3. When both sides changed the same lines, that is a conflict, and a conflict is
   never resolved by guessing.

## 2. First: which mode is this project in?

Two modes exist. They are not compatible, and a project uses exactly one. Find out
before you touch anything — the answer changes how work is published.

| You see in the project root | Mode | How work is published |
| --- | --- | --- |
| `push-now.ps1`, `.teamsync.log` | **autosync** | a background app publishes; you can ask it to publish now |
| `scripts/share.ps1`, `.githooks/` | **review** | a person publishes deliberately, and `main` changes only through a reviewed pull request |

If neither is present, this is not a team project yet — ask the user.

## 3. Autosync mode

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

### Announcing work in progress

Before your first WRITE to any file — and only for files you will change — run:

```
powershell -NoProfile -ExecutionPolicy Bypass -File working.ps1 <files you will change>
```

An agent's working copy lives invisibly in its own context until it is written
out; this command is what makes it visible. The other side sees "working on
these files" within seconds, and incoming versions of the named files are held
until you publish. Publishing (push-now.ps1) clears the announcement; so does
`working.ps1 -Done`; so does a 15-minute timeout.

Reading needs no announcement. Read freely — a newer version arriving in a file
you are reading is good news: re-read it and you hold the newest truth. Only
writing collides; reading never does.

### Publishing


When a piece of work is finished — the change is complete and the files are in the
state you intend — run this from the project root:

```
powershell -NoProfile -ExecutionPolicy Bypass -File push-now.ps1
```

Check the exit code:

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | published, or nothing to publish | carry on |
| 1 | blocked: network, VPN, or the app did not answer | tell the user; do not retry in a loop |
| 2 | **conflict** | stop; go to section 5 |

If you never run it, the work still goes out once the folder has been quiet for
four minutes. That backstop exists for forgetfulness, not as the normal path.
Publishing yourself is immediate and marks a finished state instead of a pause.

### Before you edit a file

```
powershell -NoProfile -ExecutionPolicy Bypass -File who.ps1
```

It prints whether the other person is online, which files **they** have
unpublished work on, and which files **you** have unpublished work on.

A file with unpublished work on it is exactly the file that collides if the other
person edits it too, so treat that list as a warning: work elsewhere, or ask them
to publish. **Nothing is locked** - you can still edit it, you just now know what
it costs.

Nothing has to be declared or released by hand. The warning is derived from git: a
file is flagged from the moment it is saved until the moment it is published, and
it clears itself. That is also why publishing each finished piece matters to the
other person and not only to you - it is what frees the file in their view.

### Useful checks

```
powershell -NoProfile -Command "Get-Content .teamsync.log -Tail 20"
git rev-list --count origin/main..HEAD
```

The first shows what the sync app has been doing; the second shows how many of
your commits have not reached the other person.

## 4. Review mode

- **`live`** is the shared working branch. Both people commit there all day. It is
  allowed to be broken.
- **`main`** is the truth. It changes only through a pull request the other person
  has read, and it must stay green.

```
pwsh scripts/catchup.ps1     # take their work
pwsh scripts/share.ps1       # publish yours
gh pr create --base main --head live --fill   # when a feature is finished
```

A direct push to `main` is refused by `.githooks/pre-push`. GitHub Free cannot
protect a branch in a private repository, so that hook is the only guard that
exists. Never disable it and never suggest `--no-verify` as a shortcut.

## 5. Conflicts

A conflict means both people changed the same lines. **Nothing was pushed and
nothing was lost.** Every version still exists.

1. Read the newest `_conflicts/<timestamp>/CONFLICT.md` (autosync mode) or
   `.sync-conflict.md` (review mode).
2. In autosync mode you are given three real files per conflicted file:
   - `NAME.MINE.ext` — this machine's version
   - `NAME.THEIRS.ext` — the other person's version, from GitHub
   - `NAME.BASE.ext` — the file before either side touched it
   Compare each side against `BASE`. That, not the two sides alone, tells you what
   each person was actually trying to change.
3. Edit the **real file in the project folder** so that both intents survive.
   The files under `_conflicts/` are copies for reading; editing them changes
   nothing.
4. Never resolve by taking one side wholesale, and never with `--ours` or
   `--theirs`. Those are ways of making the error disappear, not of resolving it.
5. If the two sides made genuinely contradictory decisions — two incompatible
   designs, not two additions — **stop and ask the user.** A silently merged
   contradiction is worse than an unresolved conflict.
6. Finish it:

```
git add .
git rebase --continue
```

Sync resumes on its own. To abandon the attempt without losing commits:
`git rebase --abort`.

## 6. Never

- **Never `git push`, `git pull`, or `git merge` by hand.** Use the mode's own
  command. Doing it by hand is how two people end up with histories that cannot
  be reconciled.
- **Never force-push.** No `--force`, no `--force-with-lease`.
- **Never `git reset --hard`** on shared work, and never delete `.git`.
- **Never commit a secret.** `.env` and `*secret*.json` are ignored; if one must
  be shared, tell the user to do it outside the repository.
- **Never rename, move, or reformat files across the project as a side effect of
  another task.** This is the single largest source of surprise conflicts in
  AI-assisted teamwork: the other person's agent is editing those same files right
  now, and a reflow turns their one-line change into a whole-file conflict.
- **Never edit outside your ownership area** once the project's `AGENTS.md` names
  one.

## 7. Working habits that prevent conflicts

- **Small, finished changes, published often.** A conflict over ten minutes of
  work is a two-minute fix; a conflict over a day of work costs a day.
- **Append, never reorder.** In shared documents — roadmaps, design notes, task
  lists — add at the end and edit only your own blocks. Reordering rewrites every
  line and turns a small change into a total conflict.
- **One file per entry.** A new design note is a new file, numbered and never
  renamed, not an addition to one growing document.
- **Say what you published.** When you finish, tell the user in one line what went
  out, so they know what the other person is about to receive.

## 8. Modules and ownership — the boundary that prevents conflicts

A conflict happens when two people change the same file. Everything in this
section exists to make that rare by construction rather than by good intentions.

### The three classes of file

Do not treat them the same. Find out which class a file is in before editing it.

| Class | Examples | Rule |
| --- | --- | --- |
| **Module code** | `src/parser/`, `app/billing/` | One owner at a time. Do not edit a module outside your owner's area. |
| **Treaty files** | a module's public surface: `index.ts`, `__init__.py`, `api.py`, a shared schema, shared types, config | Everyone reads them; changing one is a **joint decision**, not an edit. If your task requires changing a treaty, stop and say so. |
| **Shared documents** | `README.md`, roadmaps, `docs/design/*` | Both write. Append, never reorder. Edit only your own blocks. |

### The ownership table

The project's `AGENTS.md` holds a table of paths and owners. **Read it before your
first edit.** If it is still empty, ask before touching anything you did not
create yourself.

When filling it in: assign whole folders, not individual files, and assign them
**after** the first design note rather than before. Boundaries invented ahead of
the work create more conflicts than they prevent — every feature then crosses
three modules and every change becomes a five-file diff.

### Why module size matters here more than usual

Two reasons that have nothing to do with the other person:

1. **Your output quality tracks how much you must hold at once.** A 2,000-line
   file forces you to load all of it and rewrite regions you did not fully
   understand. A 300-line module with a clear public surface lets you read that
   module plus the signatures of its neighbours, and stop.
2. **Reverting.** Work is thrown away often. A change confined to one module can
   be discarded cleanly. A change spread across twelve files cannot — the good
   part is tangled with the bad.

So: when you add something, put it inside an existing module or make a new one.
Do not scatter a feature across the tree because each individual edit looked
small.

### Git merges text, not meaning

Two people can produce changes that merge perfectly and still make broken
software — one changed a function's contract, the other kept assuming the old
one. Git cannot see this. Nothing in the sync app can see it.

The only cheap guard is a test on each module's public surface. When you change a
treaty file, check what depends on it and run whatever tests exist. When you add a
module, give it at least one test of its public surface — that is what will catch
the other person's incompatible change later.

### The most common way an agent causes a conflict

Not by editing the wrong module deliberately. By tidying: renaming a variable
project-wide, reformatting a file it happened to open, reordering imports, moving
a helper "where it belongs". Every one of those rewrites lines the other person's
agent is editing right now, and turns their one-line change into a whole-file
conflict.

**Change what the task requires and nothing else.** If you see something worth
cleaning up outside your area, say so; do not do it.

## 9. Language and explanation

Answer the user in Persian unless they ask otherwise, in plain readable Markdown.
Explain from zero: never point at a document section, a file name, or an internal
term as if it were the explanation. If you need to refer to a document, summarise
what it says in place.
