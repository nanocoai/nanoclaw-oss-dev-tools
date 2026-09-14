---
name: shared-terminal
description: Open one local browser terminal that the operator and agent can both control, for supervised installers, interactive menus, and sign-in handoffs on macOS or Linux.
---

# Shared terminal

Use one real PTY for both browser typing and agent input. The browser renders
ANSI redraws with bundled xterm.js; the controller reads the current rendered
screen through pyte. The shell starts in the requested directory, without shell
startup files or command history. SSH can run inside it when the task is remote.

## Start and show the session

Requires macOS or Linux, Python 3.10+, Bash and a browser on the same machine.
Resolve this skill's installed directory as `SKILL_DIR`. Create a task-owned
private session directory and a venv outside the source checkout:

```bash
umask 077
SESSION_DIR=$(mktemp -d "${TMPDIR:-/tmp}/shared-terminal.XXXXXX")
python3 -m venv "$SESSION_DIR/venv"
"$SESSION_DIR/venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r "$SKILL_DIR/requirements.txt"
"$SESSION_DIR/venv/bin/python" "$SKILL_DIR/scripts/server.py" --cwd "$PWD" --state-file "$SESSION_DIR/session.json"
```

Keep the server running through the agent's long-running command facility. Use
the task's actual working directory for `--cwd`; do not launch an installer just
because the terminal is ready. The server binds only `127.0.0.1` on a free port.
The shell inherits PATH and SSH_AUTH_SOCK, but most environment variables are
intentionally omitted. If a task needs another variable, set it in that shell
using the task's existing credential handling.

The server prints the path to `session.access.md`, which contains a clickable
private session link. Show that local Markdown file to the operator, using the
app's file-opening facility when available. Keep the access file and state file
private: the link grants control of the local shell. Do not print the state JSON,
copy the access link into reports, or expose the server through a public tunnel.
The browser removes the token fragment after exchanging it for a session cookie.
Opening the bare address without that link does not grant terminal access.

## Share control

Use the controller with the same Python and state file:

```bash
"$SESSION_DIR/venv/bin/python" "$SKILL_DIR/scripts/control.py" --state-file "$SESSION_DIR/session.json" status
"$SESSION_DIR/venv/bin/python" "$SKILL_DIR/scripts/control.py" --state-file "$SESSION_DIR/session.json" match --text 'Choose a provider'
"$SESSION_DIR/venv/bin/python" "$SKILL_DIR/scripts/control.py" --state-file "$SESSION_DIR/session.json" send --expect 'Choose a provider' --key down
"$SESSION_DIR/venv/bin/python" "$SKILL_DIR/scripts/control.py" --state-file "$SESSION_DIR/session.json" send --expect 'Choose a provider' --key enter
```

Send command text through stdin to `send`, ending with a carriage return when it
should execute. Named keys include arrows, Enter, Tab, Escape and Ctrl-C.
`--expect` rejects input if the expected literal is absent from the current
screen. Inspect the visible prompt before a menu action; old scrollback does
not establish the current menu state. The guard cannot serialize a human's
simultaneous keystrokes, so agree who is driving during each handoff.

For ordinary, non-sensitive output, `screen --raw` explicitly returns the current
rendered screen. During authentication or secret entry, use metadata `status`
and a known public prompt with `match`; let the human use the live terminal and
authentication page. Do not read terminal contents into agent context, capture
screenshots, or export raw output while credentials or one-time codes are visible.
Resume agent input after the human finishes and the public next prompt is known.

The browser's typing toggle disables its own input, and resizing the window
updates the PTY size. Reconnection replays bounded output or restores the current
rendered screen if the old cursor has expired. Output is kept in memory; this
utility does not save or sanitize an evidence transcript. Collect proof through
the workflow's own safe verifier when an E2E receipt is needed.

## End the session

At task completion, after the foreground command has finished, use the browser's
**End session** button or:

```bash
"$SESSION_DIR/venv/bin/python" "$SKILL_DIR/scripts/control.py" --state-file "$SESSION_DIR/session.json" stop
```

This closes the owned shell and its foreground process group, shuts down the
server, and removes its unchanged private state/access files. It does not remove
installed software, remote guests, services or the session directory/venv.
A task intentionally left running needs its terminal kept open; do not end it
merely to clean up the interface.

## Qualification

The automated tests exercise a real local HTTP server and PTY: terminal input,
window size, browser cookie access, prompt guards, reconnect recovery, private
access files and foreground-process shutdown. Linux and macOS are the supported
hosts. Native Windows is unsupported; WSL and remote-browser forwarding have not
been qualified. Vendored xterm assets have exact versions, SHA-256 digests and
upstream licenses in `assets/vendor-manifest.json`.
