# TEAM-PROJECT-REFERENCE.md

**The reference for team projects.** Read this whenever the user says a project is
a team project, or whenever you find this file in a project root.

It is the same file on every machine. A copy lives in every shared project, and
the master copy lives in the toolkit. If they ever disagree, the copy inside the
project wins — it is the one everybody is actually working against.

---

## 1. What a team project is

Several people work on the same folder at the same time, one Windows machine
each, every one of them driving their own AI agent (Claude Code, Codex, or
another). Every machine holds a full copy. The copies are kept in step through a
**private** GitHub repository.

There may be two people or ten. Nothing here assumes two — when you need to know
who is there, run `who.ps1` and read the answer rather than guessing at "the
other person".

This changes three things about how you work, and nothing else:

1. Someone else's changes can arrive at any moment.
2. Your changes must be published to be useful.
3. When your work and the shared branch changed the same lines, that is a
   conflict, and a conflict is never resolved by guessing. With more than two
   people the incoming side can hold SEVERAL teammates' work at once — see §5.

### Everybody needs a different name

Presence, the "working on this file" warnings and commit authorship are all
joined by one key: the person's `git config user.name`. Two people sharing a
name would erase each other's warnings, so the app refuses to start on a name
already held by a different GitHub account and says what to change.

One person working from two machines is NOT a collision. The second machine is
numbered automatically, so `amin` and `amin-2` are one human at two desks — and
their teammates can tell the two apart, which is the point.

## 2. First: which mode is this project in?

Two modes exist. They are not compatible, and a project uses exactly one. Find out
before you touch anything — the answer changes how work is published.

| You see in the project root | Mode | How work is published |
| --- | --- | --- |
| `push-now.ps1`, `.teamsync.log` | **autosync** | a background app publishes; you can ask it to publish now |
| `scripts/share.ps1`, `.githooks/` | **review** | a person publishes deliberately, and `main` changes only through a reviewed pull request |

If neither is present, this is not a team project yet — ask the user.

## 3. Autosync mode

<!-- charter:begin - generated from ui\assets\help\3-agent-prompt.md by agent-bundle\regenerate.ps1 -->

## Running the project, together with the humans

The project belongs to the PEOPLE. You are one person's hands, not a party of
your own. Agents do not make AGREEMENTS with each other - the final word on
anything binding belongs to the humans, all of them. But at the level of
design notes, agents MAY put proposals to each other: append a clearly
attributed note (e.g. "proposal - amin's agent: ...") to the shared design
document, and any other agent may answer there in the same way, in writing,
where every human can read it. Everything in those notes stays a proposal
until the people approve it; the ownership table records only what the humans
decided. What you must actively know and do:

- When the ownership table in AGENTS.md is empty or stale, help your human
  divide the work: propose a module split drawn from the actual design -
  cohesive modules, one owner each, the thinnest possible shared surface -
  with your reasons. The people decide; after they agree, write the outcome
  into AGENTS.md and publish it. Propose the split AFTER the first design
  work exists, not before - boundaries drawn before the work make every
  feature cross three modules.
- When your task needs a change inside somebody else's module or in a treaty
  file, stop and tell your human exactly what is needed and why, so the
  people can settle it; implement only after their agreement.
- When the division stops fitting - your features keep crossing the same
  boundary - say so and propose a re-division to your human. Never redraw the
  boundaries unilaterally.

<!-- charter:end -->

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
| 3 | **it would delete or roll back work** | stop; go to section 5b |

If you never run it, the work still goes out once the folder has been quiet for
four minutes. That backstop exists for forgetfulness, not as the normal path.
Publishing yourself is immediate and marks a finished state instead of a pause.

### Before you edit a file

```
powershell -NoProfile -ExecutionPolicy Bypass -File who.ps1
```

It prints who is online, which files **each of them** has unpublished work on,
which files **you** have unpublished work on, and whether anybody is in the
middle of resolving a conflict.

A file with unpublished work on it is exactly the file that collides if you edit
it too, so treat that list as a warning: work elsewhere, or ask them to publish.
**Nothing is locked** - you can still edit it, you just now know what it costs.

Nothing has to be declared or released by hand. The warning is derived from git: a
file is flagged from the moment it is saved until the moment it is published, and
it clears itself. That is also why publishing each finished piece matters to
everybody else and not only to you - it is what frees the file in their view.

### Useful checks

```
powershell -NoProfile -Command "Get-Content .teamsync.log -Tail 20"
git rev-list --count origin/main..HEAD
```

The first shows what the sync app has been doing; the second shows how many of
your commits have not reached anybody else.

## 4. Review mode

- **`live`** is the shared working branch. Everybody commits there all day. It is
  allowed to be broken.
- **`main`** is the truth. It changes only through a pull request somebody else
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

A conflict means your work and the shared branch changed the same lines.
**Nothing was pushed and nothing was lost.** Every version still exists.

A conflict is LOCAL: it happens on one machine. Everybody else's repository is
healthy and the shared branch is a consistent state, so nobody else is blocked.
What they get told is that it is happening, so they leave that file alone.

1. Read the newest `_conflicts/<timestamp>/CONFLICT.md` (autosync mode) or
   `.sync-conflict.md` (review mode).
2. In autosync mode you are given three real files per conflicted file. The
   names keep the whole path with folders joined by `__`, so two files called
   `notes.md` in different folders stay apart:
   - `<path>.MINE.ext` — this machine's version
   - `<path>.THEIRS.ext` — **the version from GitHub. That is the shared
     branch, so with more than two people it can already carry SEVERAL
     teammates' work.** `CONFLICT.md` names who is inside it for each file.
   - `<path>.BASE.ext` — the file before anybody touched it
   Compare each side against `BASE`. That, not the two sides alone, tells you
   what each person was actually trying to change.
3. **Keep EVERY intent, not two.** This is the mistake here that destroys
   somebody's work: reconstructing "mine plus theirs" from a two-sided picture
   drops a third person's change that was also inside THEIRS. To see who is
   really in there: `git log --oneline <BASE-commit>..origin/main -- <path>`.
4. Edit the **real file in the project folder** so that all those intents
   survive. The files under `_conflicts/` are copies for reading; editing them
   changes nothing.
5. Never resolve by taking one side wholesale, and never with `--ours` or
   `--theirs`. Those are ways of making the error disappear, not of resolving it.
6. If the sides made genuinely contradictory decisions — two incompatible
   designs, not two additions — **stop and ask the user.** A silently merged
   contradiction is worse than an unresolved conflict.
7. Finish it:

```
git add .
git rebase --continue
```

Resolving one file can raise the conflict for the next commit being replayed;
a new `_conflicts` folder appears for that one too. Sync resumes on its own
when the last is finished. To abandon the attempt without losing commits:
`git rebase --abort`.

### While somebody is resolving

- The app stops publishing your work AUTOMATICALLY, so the ground does not
  move under them. `push-now.ps1` and Publish now still send at once — that is
  a decision, and decisions stay with the people. After ten minutes the
  automatic publishing resumes by itself, so a machine that went down
  mid-resolve cannot freeze the team.
- They stay online and keep announcing what they hold. They cannot sync their
  OTHER files meanwhile: integration here is `git rebase`, which git will not
  let anything else touch until it finishes. Everything catches up the moment
  it is done.

### Somebody else's conflict

Any teammate can read all three versions of it — two of them are ordinary
history everybody has, and the stuck person's own side is published as a ref
for exactly this. Only their machine can finish their own merge, but anyone
may decide what the file should say and publish it, and their side takes that
version when it arrives.

Tick the box beside it first. That marks you as the volunteer, and for
everybody else the button disappears and your name shows instead — two people
settling the same file separately is not help, it is the next conflict.

## 5b. Exit 3 — it would destroy work

Adding work needs no permission. Destroying it does. When what is about to be
published DELETES a file, or puts a file back to an older version of itself,
`push-now.ps1` stops and names the files. Nothing was pushed; nothing is lost.

Published, it removes that file — or that newer wording — from **every**
teammate's machine, and until this guard existed it went out with the log
saying only "pushed 1 commit(s)". It is caught by comparing the disk against
this machine's own history, so a machine that is merely behind is never caught
by it: a file it has not received yet is not a file it deleted.

**This is a decision for the people, never for an agent.**

- A mistake: `git checkout -- <file>` puts the newest version back, then run
  push-now again.
- Meant: the person confirms it in the TeamSync window ("Needs your OK"), and
  push-now then goes through. The confirmation covers exactly the files that
  were listed — a later deletion is asked about again.

Never route around it: no `git rm`, no hand-editing the config that records
the confirmation, no pushing by hand.

## 6. Never

- **Never `git push`, `git pull`, or `git merge` by hand.** Use the mode's own
  command. Doing it by hand is how a team ends up with histories that cannot
  be reconciled.
- **Never force-push.** No `--force`, no `--force-with-lease`.
- **Never `git reset --hard`** on shared work, and never delete `.git`.
- **Never commit a secret.** `.env` and `*secret*.json` are ignored; if one must
  be shared, tell the user to do it outside the repository.
- **Never rename, move, or reformat files across the project as a side effect of
  another task.** This is the single largest source of surprise conflicts in
  AI-assisted teamwork: somebody else's agent is editing those same files right
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
  out, so they know what everybody else is about to receive.

## 8. Modules and ownership — the boundary that prevents conflicts

A conflict happens when two of you change the same file. Everything in this
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

Two reasons that have nothing to do with anybody else:

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

Two of you can produce changes that merge perfectly and still make broken
software — one changed a function's contract, the other kept assuming the old
one. Git cannot see this. Nothing in the sync app can see it.

The only cheap guard is a test on each module's public surface. When you change a
treaty file, check what depends on it and run whatever tests exist. When you add a
module, give it at least one test of its public surface — that is what will catch
somebody else's incompatible change later.

### The most common way an agent causes a conflict

Not by editing the wrong module deliberately. By tidying: renaming a variable
project-wide, reformatting a file it happened to open, reordering imports, moving
a helper "where it belongs". Every one of those rewrites lines somebody else's
agent is editing right now, and turns their one-line change into a whole-file
conflict.

**Change what the task requires and nothing else.** If you see something worth
cleaning up outside your area, say so; do not do it.

## 9. Language and explanation

Answer the user in Persian unless they ask otherwise, in plain readable Markdown.
Explain from zero: never point at a document section, a file name, or an internal
term as if it were the explanation. If you need to refer to a document, summarise
what it says in place.
