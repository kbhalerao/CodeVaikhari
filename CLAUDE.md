# Working in this repo

Local TTS daemon that speaks coding-agent notifications, giving each project
its own voice. Read `README.md` for what it does; this file is about changing
it safely.

## Shape

Four files, no framework, no build step.

| File | Role |
|---|---|
| `vaikharid.py` | the daemon: model, SQLite, unix socket, HTTP UI |
| `say` | the client. **stdlib only** |
| `ui.html` | the whole front end. Vanilla JS, no dependencies |
| `hooks/*.py` | Claude Code hook adapters. **stdlib only** |
| `avatars/*.webp` | one portrait per voice, generated and committed |
| `tools/generate-avatars.py` | build-time only; regenerates the above |
| `logo.svg` | the mark; also the favicon, inlined again in the header |

`ui.html` is read from disk per request, so editing it needs no restart.
Editing `vaikharid.py` or `say` does: `systemctl --user restart vaikhari`.

## Invariants

Break these and the thing stops being useful:

1. **`say` and the hooks import stdlib only.** They are the hot path, run on
   every notification. Importing torch there would put the 10.7 s cold start
   back on every call. All model work belongs in the daemon.

2. **Playback is serial, and there is exactly one daemon.** One worker thread
   drains `_jobs`; each job blocks until `pw-play` exits. Never play from
   another thread. `gap()` enforces the 3 s floor between utterances.

   The serial queue only helps if one process owns it. `say` auto-starts a
   daemon when it cannot connect, and a restart leaves a ~14 s window with no
   socket, so a hook firing then used to start a second daemon which stole the
   socket while the first kept its own repeater running — two processes
   talking over each other. An `flock` on `daemon.lock`, taken before the
   model loads, is what actually guarantees serial output.

   `VAIKHARI_TRACE=1` logs every playback start and end with pid and thread.
   Overlap cannot be diagnosed after the fact; reach for this first.

3. **A voice is a lookup, not an allocation.** `voice_for_project()` is the
   single source of truth, cached in the `voices` table and sticky. Do not
   re-derive a voice anywhere else; that bug shipped twice and both times the
   symptom was one session speaking in two voices.

4. **Hooks never fail loudly.** They wrap everything and swallow errors. A
   broken TTS setup must not break the user's coding session.

5. **The inbox is not backlog.** Pruning only ever touches `dismissed=1` rows.
   An undismissed message is never dropped, by count or by byte budget.

6. **`dismissed` and `played` are separate axes.** Heard-but-not-acted-on
   still sits in the inbox. Conflating them breaks repeat.

7. **The HTTP server has no auth.** It is safe only because it binds
   `127.0.0.1`. Never widen the bind without adding a token first — the
   message text is genuinely sensitive. See `docs/REMOTE.md`.

8. **`--auto` means skippable, not automatic.** Hook messages use it so a
   session that already spoke deliberately does not get announced twice. The
   window is tracked in `_last_manual` at enqueue time, not read from the
   utterances table, because rows are written when playback ends.

9. **The daemon prefixes the project name onto everything it speaks.** Do
   not also prefix in the hooks or callers — it gets said twice.

## Gotchas

- **`pkill -f vaikharid` matches its own shell** when the pattern appears in
  the command text, killing the shell mid-script. Use
  `systemctl --user restart vaikhari`, or `pkill -O 3 -f vaikharid`.

- **`_db_lock` is an `RLock`** because the db helpers nest
  (`register_session` → `voice_for_project`). Keep it reentrant.

- **Browsers restore `<select>` values on reload and fire `change`.** The
  voice picker is guarded with `autocomplete="off"` and a no-op check against
  the current value. Without both, merely reloading the UI silently pins
  whatever was on screen.

- **Session labels are `project.xxxx`, not session ids.** `say -S` accepts
  either; `resolve_session` tries the session table first, then falls back to
  project. Pass the real `CLAUDE_CODE_SESSION_ID` when you have it.

- **The env var is `CLAUDE_CODE_SESSION_ID`.** Not `CLAUDE_SESSION_ID`, which
  is never set.

- **Claude Code transcripts hold one content block per row.** Text and
  `tool_use` are never in the same row, so "does this row also have a tool
  call" can never distinguish a preamble from a conclusion. What marks text as
  a preamble is a `tool_use` row appearing *after* it. `claude-notify.py`
  walks back over assistant rows and stops at whichever comes first.

- **Avatars are optional.** `avatars/` is committed, but the UI falls back to
  an SVG monogram per voice, so never assume a file exists. The daemon tells
  the page which ones it actually found.

## Testing

There is no test suite; it is an audio daemon and the useful assertions are
about timing and sound. Verify by hand, and prefer checking the database over
trusting the logs:

```bash
say --status                      # muted, pending, repeat interval
say --sessions                    # who holds which voice

sqlite3 ~/.local/state/vaikhari/vaikhari.db \
  'SELECT session,voice,played,dismissed,text FROM utterances ORDER BY ts DESC LIMIT 5;'
```

Synthesize without making noise: `say -o /tmp/out.wav "text"`.

When testing timing, remember rows are written **when playback ends**, so
`ts` deltas minus `dur` give the real silence between utterances.

Set `say --repeat 1` to exercise the repeat loop in ~60 s rather than 10 min,
and put it back to 10 afterwards.

## Style

Match what is there: plain functions, no classes beyond the HTTP handler, no
speculative extension points. Comments explain *why* a thing is the way it is,
not what the line does — the non-obvious tradeoffs are what a reader needs.
Prefer deleting a requirement to implementing it.
