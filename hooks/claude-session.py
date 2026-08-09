#!/usr/bin/env python3
"""Claude Code SessionStart / SessionEnd hook -> register with the say daemon.

On start the session claims a voice (unique among live sessions) so you can
tell which of several running Claude sessions is talking. On end the voice is
released back to the pool.

Start is fire-and-forget: it must never add latency to session startup, and on
a cold boot registering would otherwise block on the ~10s model load.
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

    ending = ev.get("hook_event_name") == "SessionEnd"
    args = [SAY, "-S", sid, "--unregister" if ending else "--register"]
    if not ending:
        args.append(cwd)

    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass                                   # never break the session over TTS
    return 0


if __name__ == "__main__":
    sys.exit(main())
