#!/usr/bin/env python3
"""Claude Code hook -> spoken notification.

Wire into ~/.claude/settings.json for the Notification and Stop events. Claude
Code feeds hook JSON on stdin (session_id, cwd, hook_event_name, message,
transcript_path).

On Stop, this speaks the *conclusion* rather than "project finished". Claude's
closing message almost always leads with the result, and the transcript is
already handed to the hook, so the useful sentence is right there — inventing
a generic string instead was throwing it away.

Kept stdlib-only and fire-and-forget so it never delays the session.
"""
import json
import os
import re
import subprocess
import sys

SAY = os.path.expanduser("~/.local/bin/say")
MAX_CHARS = 200          # ~13 seconds spoken; past that nobody is listening
TAIL_BYTES = 512 * 1024  # enough for the last few messages of a long session


def last_assistant_text(path):
    """Final assistant prose from the transcript, read from the tail so a
    long session does not cost a multi-megabyte parse on every Stop."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()               # discard the partial first line
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return ""

    fallback = ""
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "assistant":
            continue
        content = row.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        texts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(t for t in texts if t.strip()).strip()
        if not joined:
            continue
        # Text alongside a tool_use is mid-turn narration ("Let me check X"),
        # not a conclusion. The closing message is text only.
        if any(isinstance(c, dict) and c.get("type") == "tool_use"
               for c in content):
            fallback = fallback or joined
            continue
        return joined
    return fallback


def speakable(md):
    """Markdown reads terribly aloud. Strip it to prose and keep the opening
    sentences, which is where the conclusion lives."""
    md = re.sub(r"```.*?```", " ", md, flags=re.S)      # code blocks
    md = re.sub(r"^\s*\|.*\|\s*$", " ", md, flags=re.M)  # table rows
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)        # images
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)     # links -> text
    md = re.sub(r"`([^`]*)`", r"\1", md)                 # inline code
    md = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", md)
    md = re.sub(r"^\s{0,3}#{1,6}\s*", "", md, flags=re.M)
    md = re.sub(r"^\s*[-*+]\s+", "", md, flags=re.M)     # bullets
    md = re.sub(r"^\s*\d+\.\s+", "", md, flags=re.M)
    md = re.sub(r"^\s*>\s?", "", md, flags=re.M)
    md = re.sub(r"\s+", " ", md).strip()

    if len(md) <= MAX_CHARS:
        return md
    # Cut at a sentence end if there is one in range, else at a word.
    window = md[:MAX_CHARS + 40]
    ends = [m.end() for m in re.finditer(r"[.!?](?:\s|$)", window)]
    ends = [e for e in ends if e <= MAX_CHARS + 40]
    if ends:
        return window[:ends[-1]].strip()
    return md[:MAX_CHARS].rsplit(" ", 1)[0] + "…"


def main():
    try:
        ev = json.load(sys.stdin)
    except (ValueError, OSError):
        ev = {}

    cwd = ev.get("cwd") or os.getcwd()
    project = os.path.basename(cwd.rstrip("/")) or "claude"
    sid_full = ev.get("session_id") or ""

    if ev.get("hook_event_name") == "Stop":
        summary = speakable(last_assistant_text(ev.get("transcript_path")))
        # Fall back only when there is genuinely nothing to say.
        text = f"{project}: {summary}" if summary else f"{project} finished"
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
        # --auto: if this session already said something deliberate, the
        # daemon drops this rather than repeating the news.
        subprocess.Popen([SAY, "-S", sid_full or project, "--cwd", cwd,
                          "--auto", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass                                   # never break the session over TTS
    return 0


if __name__ == "__main__":
    sys.exit(main())
