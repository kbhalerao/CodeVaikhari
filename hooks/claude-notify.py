#!/usr/bin/env python3
"""Claude Code hook -> spoken notification.

Wire into ~/.claude/settings.json for the Notification and Stop events. Claude
Code feeds hook JSON on stdin (session_id, cwd, hook_event_name, message).

Kept stdlib-only and fire-and-forget so it never delays the session: it hands
the text to the `say` daemon and returns.
"""
import json
import os
import subprocess
import sys

SAY = os.path.expanduser("~/.local/bin/say")


def main():
    try:
        ev = json.load(sys.stdin)
    except (ValueError, OSError):
        ev = {}

    cwd = ev.get("cwd") or os.getcwd()
    project = os.path.basename(cwd.rstrip("/")) or "claude"
    sid_full = ev.get("session_id") or ""
    sid = sid_full[:4]
    label = f"{project}.{sid}" if sid else project

    event = ev.get("hook_event_name", "")
    if event == "Stop":
        text = f"{project} finished"
    else:
        # Notification messages are things like "Claude needs your permission
        # to use Bash" — strip the redundant prefix, we know who is talking.
        msg = (ev.get("message") or "needs attention").strip()
        for prefix in ("Claude Code ", "Claude "):
            if msg.startswith(prefix):
                msg = msg[len(prefix):]
                break
        text = f"{project}: {msg}"

    try:
        subprocess.Popen([SAY, "-S", sid_full or label, "--cwd", cwd, text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass                                   # never break the session over TTS
    return 0


if __name__ == "__main__":
    sys.exit(main())
