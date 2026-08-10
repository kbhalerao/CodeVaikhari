# Muting, the inbox, and dismissal

## Two independent axes

Every message carries `played` and `dismissed`, and they mean different
things:

- `played` — it came out of the speakers.
- `dismissed` — you have dealt with it.

Heard but not acted on is the normal state of a useful notification, so it
stays in the inbox. Anything that conflates the two empties it too early.

## Muting

```bash
say --mute      # queue instead of speaking
say --unmute
say --status    # muted? how many waiting? UI url
say --inbox     # speak what is waiting, now
```

Mute does not drop messages, it queues them.

Muted messages have `played=0`, which is what keeps them safe from the
auto-dismissal below.

## Auto-dismissal

Typing into a session means you have dealt with whatever it was telling you,
so the `UserPromptSubmit` hook clears that session's inbox. Combined with the
hook suppression, the inbox mostly empties itself and what remains is what you
never went back to.

**It only clears `played=1` rows.** A message that queued while muted was
never heard, so returning to the session is no reason to drop it silently.

By hand:

```bash
say --dismiss-session    # this session's played messages
say --dismiss            # everything waiting, all sessions
```

These are deliberately separate flags. Session defaults from
`CLAUDE_CODE_SESSION_ID`, so overloading `--dismiss` would silently narrow it
whenever it ran inside a session.

## Not saying it twice

Hook messages are sent with `--auto`, meaning *skippable*. If the session
already said something deliberate in the last 120 seconds
(`VAIKHARI_AUTO_WINDOW`), the daemon drops the hook message. Identical hook
messages within 10 minutes (`VAIKHARI_AUTO_DEDUP`) are dropped too — Claude
Code re-emits "is waiting for your input" while it sits idle.

## Retention

This is a notifier, not an archive. One rule: the newest 50 dismissed messages
are kept (`VAIKHARI_KEEP`), under a 100 MB audio cap (`VAIKHARI_KEEP_BYTES`),
and the rest are deleted outright — audio and text together.

**Undismissed messages are exempt, at any age or size.** Pruning only ever
touches `dismissed=1`. It runs when a message arrives and hourly regardless.

Audio is stored with the message and costs ~48 KB/s, so a three-minute
utterance is ~9.5 MB. Replay never re-synthesizes.
