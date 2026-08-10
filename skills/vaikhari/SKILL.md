---
name: vaikhari
description: The `say` command — a local TTS notifier that speaks coding-agent output, one voice per project. Covers speaking from a session, muting and the inbox, dismissal, session registration and closeout, and diagnosing speech that overlaps, repeats, or never arrives. Use whenever `say`, vaikhari, spoken notifications, or the notification inbox come up.
---

# say / Code Vaikhari

A resident daemon holds a neural TTS model; `say` is a stdlib-only client that
talks to it over a unix socket. Each project gets its own voice, so a
background session that speaks identifies itself without you looking. Messages
that were queued while muted, or spoken but not acted on, wait in an inbox at
`http://127.0.0.1:8765`.

## Speaking

```bash
say "migration finished"     # fire and forget, ~41 ms
say -w "done"                # block until playback ends
echo "long text" | say
say -o /tmp/out.wav "text"   # synthesize without making noise
say --stop                   # cut off whatever is playing
```

Four things determine whether a `say` lands the way you meant:

1. **The daemon prefixes the project name.** Never write it yourself; it gets
   said twice.
2. **A deliberate `say` suppresses the Stop hook for 120 seconds.** Say the
   conclusion yourself, or write a good closing line and let the hook read
   it — not both.
3. **Playback is serial with a 3 s gap.** A long message holds the queue for
   its full duration.
4. **Attribution is automatic.** Inside Claude Code it uses
   `CLAUDE_CODE_SESSION_ID`; in a plain shell it uses the directory. Only pass
   `-S`/`--cwd` when speaking *as* some other session.

## Going further

Read the file that matches the question. Don't read them all.

| Question | File |
|---|---|
| Muting, what the inbox holds, why a message did or didn't clear itself, `--dismiss` vs `--dismiss-session`, retention | `reference/inbox.md` |
| Which hook does what, how a project gets its voice, pinning, and closing a session down | `reference/sessions.md` |
| Two voices talking over each other, nothing plays, a session speaking in the wrong voice, reading the database | `reference/diagnostics.md` |

Full prose documentation lives in this repo's `README.md`; `CLAUDE.md` there
carries the invariants that matter when changing the daemon itself.
