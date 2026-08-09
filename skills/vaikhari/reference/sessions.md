# Sessions, voices, and closeout

## Startup is automatic

Nothing here needs doing by hand inside Claude Code. Five hooks carry it:

| Hook | Effect |
|---|---|
| `SessionStart` | register the session, take the project's voice |
| `UserPromptSubmit` | clear that session's spoken messages |
| `SessionEnd` | release the session |
| `Notification` | speak what needs attention |
| `Stop` | speak the conclusion of what just happened |

They are fire-and-forget and swallow their own errors: a broken TTS setup must
never break a coding session.

A session that speaks without having registered — the hook never fired, or it
predates the hook — self-registers on its first utterance, as long as it told
the daemon its cwd. `say` always sends cwd for exactly this reason.

## What Stop reads out

Not "*project* finished". The hook walks back through the transcript for the
last assistant message that isn't mid-turn narration, strips markdown, and
cuts to roughly the first 200 characters.

So **lead the final message with the conclusion** — what changed, what is
outstanding — rather than a preamble. That first sentence is what gets heard.

Preambles are excluded structurally: transcripts hold one content block per
row, so text and `tool_use` are never in the same row. What marks text as a
preamble is a `tool_use` row appearing *after* it. The hook stops at the first
one it meets walking back; if the turn was still working there is no
conclusion yet, and the hook says nothing at all.

## Voices

A voice is a **cached preference keyed by project, and it is sticky**. The
first session in a repo takes the next voice no other project owns; every
session after reads the same one back. Two sessions in the same repo therefore
sound alike — they are the same project, and the label (`myproject.a3f1`)
separates them in the UI.

```bash
say --sessions          # who is registered, with which voice and cwd
say --voices            # all 28 English voices with quality grades
say -v bm_george "hi"   # one-off override
say --pin bm_fable      # pin this project (survives restarts)
say --unpin
say --pins
```

Never override the voice with `-v` in normal use — the user identifies
sessions by ear, and a wrong voice is worse than no voice.

Session labels are `project.xxxx`, not session ids. `say -S` accepts either,
and falls back to matching by project.

## Closing a session down

```bash
say -S "$(pwd)" --unregister
```

Frees the voice back into the pool. It is a no-op when nothing was registered,
so run it unconditionally. Passing the working directory rather than a session
id is deliberate: `end_session()` tries the session id first and falls back to
matching on cwd, because a closing procedure knows its directory but not the
id the hook registered under.

The `/close` slash command does this as its first step, then saves memories
and tidies branches.
