#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook -> clear this session's inbox.

Going back to a session and typing in it means you have dealt with whatever it
was telling you, so chasing the same messages down in the UI afterwards is
busywork. Only messages that were actually spoken are cleared; one that queued
while muted was never heard, so returning is no reason to drop it silently.

Fire-and-forget, so it never sits between you and your prompt.
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

    sid = ev.get("session_id") or ""
    cwd = ev.get("cwd") or os.getcwd()
    if not sid:
        return 0

    try:
        subprocess.Popen([SAY, "-S", sid, "--cwd", cwd, "--dismiss-session"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass                                   # never break the session over TTS
    return 0


if __name__ == "__main__":
    sys.exit(main())
