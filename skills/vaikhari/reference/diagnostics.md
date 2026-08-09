# Diagnosing it

Prefer checking the database over trusting the logs. There is no test suite —
it is an audio daemon, and the useful assertions are about timing and sound.

## Two voices talking over each other

Reach for the trace first. Overlap cannot be diagnosed after the fact.

```bash
VAIKHARI_TRACE=1        # logs every playback start and end, with pid and thread
```

Playback is serial by construction: one worker thread drains the queue and
each job blocks until `pw-play` exits. That only holds if **one process owns
the queue**. Two pids in the trace means two daemons — an `flock` on
`daemon.lock`, taken before the model loads, is what prevents it. `say`
auto-starts a daemon when it cannot connect, and a restart leaves a ~14 s
window with no socket.

## One session speaking in two voices

`voice_for_project()` is the single source of truth, cached in the `voices`
table. Something re-derived the voice somewhere else. That bug shipped twice.

## Nothing plays

```bash
wpctl status                                   # is there a sink
pw-play --format=s16 --rate=24000 --channels=1 - < /dev/zero
say --status                                   # muted? messages waiting?
journalctl --user -u vaikhari -f               # or ~/.local/state/vaikhari/daemon.log
```

Check `say --status` before anything else — a muted daemon is working
correctly and silently.

## Reading the database

```bash
sqlite3 ~/.local/state/vaikhari/vaikhari.db \
  'SELECT session,voice,played,dismissed,text FROM utterances ORDER BY ts DESC LIMIT 5;'
```

| Table | Holds |
|---|---|
| `utterances` | text, metadata, audio as a BLOB |
| `sessions` | session id, label, project, cwd, voice, start/end |
| `voices` | the sticky project → voice cache, and pins |
| `settings` | mute, repeat interval |

Rows are written **when playback ends**, so `ts` deltas minus `dur` give the
real silence between utterances — not the `ts` deltas themselves.

## Restarting

```bash
systemctl --user restart vaikhari
```

`pkill -f vaikharid` matches its own shell when the pattern appears in the
command text, killing the shell mid-script. Use systemctl, or
`pkill -O 3 -f vaikharid`.

Editing `ui.html` needs no restart — it is read from disk per request.
Editing `vaikharid.py` or `say` does.

## Testing without making noise

```bash
say -o /tmp/out.wav "text"
say --repeat 1               # exercise the repeat loop in ~60 s; put it back to 10
```
