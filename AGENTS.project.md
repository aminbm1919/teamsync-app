# AGENTS.md

**This is a team project.** Several people work on this folder at the same time,
one machine each, every one of them with their own AI agent. There may be two of
you or ten - run `who.ps1` to find out rather than assuming.

## Read this first

`TEAM-PROJECT-REFERENCE.md`, in this same folder, is the reference for how team
projects work: how to publish, what to do when a conflict appears, and what never
to touch. **Read it now, before any other action.**

Do not restate its rules here. A rule written in two places is a rule that will
eventually disagree with itself.

## This project

- **Mode: autosync.** A background app publishes your work. To publish
  immediately, run `powershell -NoProfile -ExecutionPolicy Bypass -File push-now.ps1`
  from this folder. The reference explains the exit codes.

## Ownership

Fill this in after the first design note, not before — guessing boundaries early
creates more conflicts than it prevents. Until it is filled, ask before editing
anything you did not create.

One owner per module, and the table records only what the PEOPLE decided — an
agent may propose a split, never enact one.

| Path | Owner |
| --- | --- |
| *(unassigned)* | |

## Names

Everybody here publishes under a different `git config user.name`; that name is
what ties presence, file warnings and authorship together. One person on two
machines is fine and gets a number automatically (`amin`, `amin-2`).

## Project notes

*(Anything specific to this project goes below: what it is, how to run it, how to
test it. Keep the general team rules in the reference, not here.)*
