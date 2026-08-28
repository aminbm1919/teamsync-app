# TeamSync agent bundle

Everything an AI agent needs to work inside a TeamSync shared project,
packaged for the two agent platforms. Hand this folder (or its pieces) to any
team that uses TeamSync; nothing else is required.

One rule above all of it: if the project's own TEAM-PROJECT-REFERENCE.md
differs from these files, the project's copy wins.

## For Claude Code

Copy the folder `claude-skill/teamsync-team-project/` into the user's skills
directory:

    %USERPROFILE%\.claude\skills\teamsync-team-project\

That is the whole install. From then on, whenever Claude works in a folder
that carries TeamSync's markers (TEAM-PROJECT-REFERENCE.md or .teamsync.lock)
- or the user says a project is a team project - the skill loads the protocol.

## For Codex

Take `codex/AGENTS-teamsync.md` and either:

- append its content to the shared project's `AGENTS.md` (reaches every agent
  that opens the project, travels with the sync), or
- paste it into Codex's global standing instructions, once per machine.

## Where this content comes from

Both files are GENERATED from the app's canonical prompt
(`ui/assets/help/3-agent-prompt.md` - the "AI prompt" tab of the in-app
Help). When that prompt changes, regenerate rather than editing these copies,
so there is exactly one source of truth. The generator lives in the session
notes; the rule is simply: prompt asset first, bundle second.
