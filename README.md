# kokoro-say

Local neural text-to-speech for this machine, plus an inbox and review UI.
Replaces `spd-say` for Claude Code notifications and anything else that wants
to talk.

Runs [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) on
the local GPU. Nothing leaves the machine.

## Why a daemon

Kokoro is small but torch is not. Measured on this box (RTX 3060):

| | |
|---|---|
| `import torch` + `kokoro` | 5.4s |
| model load to GPU | 2.7s |
| **cold total per invocation** | **10.7s** |
| warm synthesis | **0.13s** for 5.6s of audio (43× realtime) |

So a script that loads the model per call would take ten seconds to say "build
finished". The model lives in a resident daemon; `say` is a stdlib-only client
that talks to it over a unix socket.

## Install

```bash
./install.sh --systemd     # venv + PATH link + keep the daemon warm from login
./install.sh               # without systemd; first say of each boot pays 10s
```

Requires [uv](https://docs.astral.sh/uv/) and PipeWire (`pw-play`).

## Use

```bash
say "build finished"                 # fire and forget
echo "long text" | say               # stdin
say -w "done"                        # block until playback ends
say -v bm_george "hello"             # override the voice
say --voices                         # list all 28 English voices, with grades
say -o out.wav "render to a file"

say --mute                           # global mute; messages queue instead
say --unmute
say --inbox                          # speak everything that queued while muted
say --status                         # muted? how many waiting?
say --stop                           # shut it up mid-sentence

say --dismiss                        # dismiss everything waiting
say --repeat 20                      # re-speak undismissed every 20 min
say --no-repeat

say --sessions                       # who is registered, and with which voice
say --pin bm_fable                   # pin this project to a voice
say --unpin                          # back to automatic allocation
say --pins                           # list pinned projects
say --ui                             # open the review UI
```

## The UI

`http://127.0.0.1:8765` — served by the daemon. Two panes:

**Left rail** — live sessions, each with a coloured pip, its working
directory, and a voice dropdown. Changing the dropdown **pins** that project
to the voice and immediately plays a sample, so you can choose by ear.
Preferences sit at the bottom.

**Right pane** — the inbox. Undismissed messages first, each with a replay
player and a **Dismiss** button; dismissed ones fall below a divider. One big
**Dismiss all**. `delete` removes a message from the database outright.

The tab title carries the pending count, so a background tab still tells you
how many are waiting.

### Repeat

Undismissed messages are re-spoken every N minutes (default **10**, toggle and
interval in Preferences, or `say --repeat 20` / `say --no-repeat`). The clock
runs from the newest undismissed message *or* the last repeat, whichever is
later, so a message is never nagged sooner than the full interval after it
arrives.

Repeat is paused while muted — mute means make no noise, and a reminder is
still noise. A single repeat reads out at most 5 messages and then says how
many more are stacked up, rather than working through twenty of them.

## Sessions and voices

Each Claude Code session registers on start (`SessionStart` hook) and claims a
voice. Uniqueness is enforced **among live sessions only** — that is what you
need to tell two speakers apart — so a voice returns to the pool on session
end rather than being burned forever. A project reclaims its previous voice
when that voice is free, so a repo tends to sound the same day to day.

Two sessions in the same repo get different voices. A session that speaks
without having registered self-registers from its `cwd`.

The pool is all **28 English voices**, ordered by the model card's quality
grade (best first) and alternating accent/gender, so the first few sessions
get both the best-sounding and the most distinguishable voices. Kokoro ships
54 voices in total; the other 26 are Japanese, Chinese, Spanish, French,
Hindi, Italian and Portuguese, and are not in the rotation.

### Pinning a project to a voice

```bash
cd ~/Documents/farmworth && say --pin bm_fable
```

A pin is your stated preference and outranks everything, including collision
avoidance — pin two projects to one voice and they will both use it. It
survives daemon restarts. `say --unpin` clears the pin; live sessions keep the
voice they are already using for the rest of their life, since reshuffling
mid-session would change the thing you just learned to recognise.

## Playback

Playback is strictly serial: one worker thread drains the queue and each job
blocks until `pw-play` exits, so two messages can never overlap. A **3 second
gap** separates consecutive utterances (`KOKORO_GAP`). It is enforced as a
minimum interval since the last playback ended rather than a blanket sleep, so
an idle system still speaks immediately and only back-to-back messages get
spaced.

`say --stop` kills whatever is speaking and drops the queue.

## Message size

There is no practical text limit. Measured on this box: a 3,105-character
passage (~480 words) synthesized in 3.4s and played for 198s, and a
pathological 400-word sentence with no punctuation came through intact —
misaki chunks on a token budget, not just on punctuation, so run-on text does
not get truncated.

The real cost is archive size: audio is stored as a BLOB at 48 KB/s, so that
198s message is ~9.5 MB. The archive is therefore bounded two ways — the last
250 dismissed messages, and a 200 MB byte budget, oldest dismissed dropped
first. The inbox is exempt from both; an undismissed message is not backlog.

Note that serial playback means one very long message holds the queue for its
full duration. `say --stop` skips it.

Storage is SQLite at `~/.local/state/kokoro-say/say.db`: `utterances` (with
the audio as a BLOB, so replay never re-synthesizes), `sessions`, `voices`,
`settings`. `dismissed` and `played` are separate axes: a message you heard
but did not act on still sits in the inbox.

## Claude Code wiring

`~/.claude/settings.json`:

| Hook | Script | Effect |
|---|---|---|
| `SessionStart` | `hooks/claude-session.py` | claim a voice |
| `SessionEnd` | `hooks/claude-session.py` | release it |
| `Notification` | `hooks/claude-notify.py` | speak what needs attention |
| `Stop` | `hooks/claude-notify.py` | "\<project\> finished" |

All hooks are fire-and-forget — they never add latency to a session, and TTS
failure never breaks Claude Code.

`/close` (in `~/.claude/commands/close.md`) releases the voice, saves
memories, and tidies merged local branches at end of session.

## Config

Environment variables, read by both client and daemon:

| | |
|---|---|
| `KOKORO_VOICE` | default voice (`af_heart`) |
| `KOKORO_DEVICE` | `cuda` or `cpu` |
| `KOKORO_UI_PORT` | UI port (8765) |
| `KOKORO_GAP` | silence between utterances, seconds (3.0) |
| `KOKORO_SOCKET` | socket path |
| `KOKORO_VENV` | venv location |

## Notes

- `nvidia-smi` on this box reports an NVML version mismatch. That breaks the
  monitoring interface only; the CUDA runtime is fine and Kokoro uses the GPU.
- misaki's spaCy model downloader shells out to `pip`, which breaks under a
  `uv` shim. `install.sh` installs the `en_core_web_sm` wheel directly.
