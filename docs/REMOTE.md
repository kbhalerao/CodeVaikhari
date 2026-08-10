# Reaching the inbox from a phone

Design sketch, not yet built. Records the decisions taken so far so the work
can start cold.

## The goal

Read and clear the inbox from a phone, on cell data, without the messages
leaving hardware you own.

## What actually changes

Not much code — one concept. Today *playback* means the desk speakers, and the
daemon is both the synthesizer and the output device. Away from the desk that
is the wrong output: the system narrates to an empty room, and the notification
is lost.

The inbox already models the fix. Messages accumulate undismissed; a phone
becomes a second consumer that fetches and plays them. SQLite stays the single
source of truth, so dismissing on the phone clears it on the desktop with no
sync logic. Most of the server work is therefore access control and transport,
not features.

Already done for this: audio is served with `Accept-Ranges` and `206`
responses over HTTP/1.1 (mobile Safari will not play media otherwise), and
message text is retained permanently while only audio expires.

## Phase 0 — prerequisites, needed under every option

**Authentication.** The HTTP server has none today. It is safe only because it
binds `127.0.0.1`. Notification text carries project names, paths, and things
like "migration needs your approval before it runs against production".
Nothing may bind beyond loopback until this exists.

Shape: a random token in `settings`, printed once by the daemon, sent as
`Authorization: Bearer` or a long-lived cookie set from a `?token=` link so
the phone can be enrolled by scanning a QR code. Constant-time compare.
Loopback can stay exempt so the CLI and existing UI are unaffected.

**Bind address.** `VAIKHARI_BIND`, defaulting to `127.0.0.1`. Refuse a
non-loopback bind when no token is set — a misconfiguration here is silent and
total.

**PWA shell.** `manifest.webmanifest` plus a service worker. Needed for
home-screen install, which on iOS is also the precondition for web push. The
service worker should cache the shell only, never `/api/*` — a stale inbox is
worse than no inbox.

**Bandwidth.** WAV is 48 KB/s, so a 5s message is ~240 KB. Fine on wifi,
wasteful on cell. Transcode to Opus on demand via ffmpeg (~24 kbps, roughly a
tenth), cached alongside the WAV, chosen by `Accept` or an explicit `?fmt=`.

## Phase 1 — transport

Deliberately left open; pick when the time comes.

| Option | Shape | Trade |
|---|---|---|
| **Tailscale** | private mesh, phone app, device-level auth | nothing public, works on cell, best fit for "data stays local"; needs a daemon and login on both devices |
| **Cloudflare Tunnel** | `cloudflared` already installed; real hostname and TLS, no ports opened | it *is* a public endpoint — needs Cloudflare Access in front, or the token above is the only thing between the internet and the inbox |
| **LAN only** | bind `0.0.0.0` + token | smallest change, home wifi only, no TLS — which blocks web push and PWA install on iOS |

TLS matters beyond eavesdropping: service workers and web push require a
secure context, so LAN-only quietly forecloses Phase 2.

## Phase 2 — push notifications

Chosen direction: **Web Push + PWA**, self-contained, no third party.

- VAPID keypair generated once, stored in `settings`; public key served to the
  page.
- `push_subscriptions` table: endpoint, p256dh, auth, created, last_seen.
  Prune on `410 Gone`.
- Service worker `push` handler shows the notification; `notificationclick`
  focuses the inbox. Actions for *Dismiss* and *Play* go straight to the API.
- The daemon posts to each subscription when a message arrives and is not
  dismissed. Once per message; the desk speakers say a thing once, and a
  phone that re-notifies on a timer is a different product.
- **iOS caveat:** web push only works once the PWA is installed to the home
  screen. That is a manual, non-obvious step and should be called out in the
  UI rather than left to fail silently.

Sending web push requires signing JWTs (ES256) and encrypting payloads
(RFC 8291). That is the one place a dependency is probably worth it rather
than hand-rolling crypto.

## Phase 3 — away mode

Chosen direction: **away means phone only.** One switch that stops local
playback and routes to the phone.

Mechanically this is the existing mute path plus a delivery target: messages
still synthesize and still land in the inbox undismissed, they just do not
reach the speakers. The push goes out in place of the sound.

**Not worth building yet.** Until a remote consumer exists, away mode is
indistinguishable from `say --mute`, so building it now would add a second
name for a thing that already works. It lands with Phase 2, not before.

Worth deciding then: should away flip automatically? Screen lock, session idle
and "no dismiss in N minutes" are all available signals, but an automatic
switch that guesses wrong is worse than a manual one that never surprises you.

## Threat model, briefly

The realistic risk is not a targeted attacker. It is binding to `0.0.0.0` on a
shared network with no token, or putting a tunnel up and forgetting Access is
off. Both are silent. Hence: refuse non-loopback binds without a token, and
log loudly, every start, whatever the server is reachable on.

Audio and text both live in one SQLite file with no encryption at rest. That
matches the threat model — anyone with the file already has the machine — but
it means the archive is as sensitive as the notifications in it, and it now
retains text indefinitely.
