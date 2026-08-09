<img src="logo.svg" alt="" width="64" align="right">

# Code Vaikhari

Give every coding-agent session its own voice.

Local neural text-to-speech with an inbox, a review UI, and per-project voices,
so when a background session speaks you know *which* one spoke without looking.
Built for [Claude Code](https://claude.com/claude-code) notifications, but the
`say` command works from anything.

Runs [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) on a
local GPU or CPU. Nothing leaves the machine.

> *Vaikharī* is the fourth and final stage of speech in Sanskrit grammar: the
> articulated, audible one, as against the mental (*madhyamā*) and visionary
> (*paśyantī*) levels. This is the last hop from text to something you can hear.
>
> The mark is those stages: an unmanifest source, then arcs radiating out,
> each more solid than the last. Only the outermost — the audible one — is
> fully drawn.

---

## Why a daemon

Kokoro is small. Torch is not. Measured on an RTX 3060:

| | |
|---|---|
| `import torch` + `kokoro` | 5.4 s |
| model load to GPU | 2.7 s |
| **cold total, per invocation** | **10.7 s** |
| warm synthesis | **0.13 s** for 5.6 s of audio (43× realtime) |

A script that loaded the model per call would take ten seconds to say "build
finished". So the model lives in a resident daemon, and `say` is a
stdlib-only client that talks to it over a unix socket. Measured end-to-end
cost of a warm `say`: **41 ms**.

## Requirements

- Linux with PipeWire (`pw-play`)
- [uv](https://docs.astral.sh/uv/)
- A CUDA GPU, or set `VAIKHARI_DEVICE=cpu` (Kokoro is comfortably realtime on
  a modern CPU; only the cold start gets slower)
- ~5 GB of disk for the venv, ~350 MB for model weights

## Install

```bash
git clone https://github.com/<you>/CodeVaikhari && cd CodeVaikhari
./install.sh --systemd     # venv, PATH link, daemon warm from login
./install.sh               # no systemd; the first say of each boot pays 10 s
```

## Use

```bash
say "build finished"            # fire and forget
echo "long text" | say          # stdin
say -w "done"                   # block until playback ends
say --auto "..."                # skippable: dropped if this session just spoke
say -v bm_george "hello"        # override the voice
say --voices                    # all 28 English voices, with quality grades
say -o out.wav "to a file"

say --mute                      # global mute; messages queue instead of vanishing
say --unmute
say --inbox                     # speak what is waiting
say --dismiss                   # dismiss everything waiting
say --repeat 20                 # re-speak undismissed every 20 min
say --no-repeat
say --stop                      # shut it up mid-sentence
say --status                    # muted? how many waiting?

say --sessions                  # who is registered, and with which voice
say --pin bm_fable              # pin this project to a voice
say --unpin
say --ui                        # open the review UI
```

## The UI

`http://127.0.0.1:8765`, served by the daemon.

**Left rail** — live sessions, each with a generated avatar, its working
directory, and a voice picker. Choosing a voice pins the project to it and
plays a sample, so you can pick by ear. Preferences at the bottom.

**Right pane** — the inbox. Undismissed messages first, each with a replay
player and a **Dismiss**; dismissed ones fall below a divider. `delete` drops
a message from the database. The tab title carries the pending count, so a
background tab still tells you how many are waiting.

Replay never re-synthesizes: the audio is archived with the message.

## Sessions and voices

**A voice is a cached preference, keyed by project, and it is sticky.** The
first session in a repo takes the next voice no other project owns; every
session after reads the same voice back. So a voice always means one repo,
which is the entire point of hearing which session spoke. Two sessions in the
same repo therefore sound alike — they *are* the same project, and the label
(`myproject.a3f1`) separates them in the UI.

The pool is all 28 English voices, ordered by the model card's quality grade
(best first) and alternating accent and gender, so the first few projects get
voices that are both good and easy to tell apart. Past 28 it wraps. Kokoro
ships 54 voices; the other 26 are Japanese, Chinese, Spanish, French, Hindi,
Italian and Portuguese, and are not in the rotation.

### Attribution

The client works out who is speaking on its own, so a bare `say "done"` sounds
like the session it ran in:

| Where `say` runs | Attributed to |
|---|---|
| Inside Claude Code | that session, via `CLAUDE_CODE_SESSION_ID` |
| A plain shell in a repo | that project, via `cwd` |
| Anywhere else | `cli`, default voice |

`-S` and `--cwd` override both.

### Avatars

Kokoro ships no artwork for its voices — they are style tensors, not
characters — so `avatars/` holds one flat-vector portrait per voice,
generated once with FLUX.1 [schnell] (Apache-2.0) and committed. All 28 are
256px and come to 125 KB, so a clone needs no image model and no Cloudflare
account.

Regenerate or restyle them with `tools/generate-avatars.py` (needs a
Cloudflare token with Workers AI write, or a current `wrangler login`). Seeds
are derived from the voice name, so a rerun reproduces the same faces.

Any voice without a file falls back to a generated SVG monogram — hue hashed
from the name, initial of its given name, an inner ring for British voices —
so deleting `avatars/` degrades cleanly rather than breaking the UI.

## Repeat

Undismissed messages are re-spoken every N minutes (default 10). The clock
runs from the newest undismissed message *or* the last repeat, whichever is
later, so nothing is nagged sooner than the full interval after it arrives.

Repeat pauses while muted: mute means make no noise, and a reminder is still
noise. One repeat reads at most 5 messages, then says how many more are
stacked up, rather than working through twenty.

## Playback

Strictly serial. One worker thread drains the queue and each job blocks until
`pw-play` exits, so two messages can never overlap. A **3 second gap**
separates consecutive utterances (`VAIKHARI_GAP`), enforced as a minimum
interval since the last playback ended rather than a blanket sleep — an idle
system still speaks immediately, and only back-to-back messages get spaced.

One consequence: a very long message holds the queue for its full duration.
`say --stop` skips it.

## Message size

No practical text limit. A 3,105-character passage (~480 words) synthesized in
3.4 s and played for 198 s, and a 400-word sentence with no punctuation came
through intact — misaki chunks on a token budget, not just on punctuation, so
run-on text is not truncated.

The cost is archive size: audio is stored at 48 KB/s, so that 198 s message is
~9.5 MB. Text is ~100 bytes and audio is not, so they do not share a fate —
**message text is kept forever** and only the audio expires, bounded two ways:
the last 250 dismissed messages keep theirs, under a 200 MB ceiling, oldest
first. Expired messages stay in the log and in search, marked "audio expired";
you lose replay, not the record. The inbox is exempt from both.

## Storage

SQLite at `~/.local/state/vaikhari/vaikhari.db`.

| Table | Holds |
|---|---|
| `utterances` | text, metadata, and the audio as a BLOB |
| `sessions` | session id, label, project, cwd, voice, start/end |
| `voices` | the sticky project → voice cache, and pins |
| `settings` | mute, repeat interval |

`dismissed` and `played` are separate axes: a message you heard but did not
act on still sits in the inbox.

## Claude Code wiring

`install.sh` does not touch your Claude Code config. Add these to
`~/.claude/settings.json` yourself, with absolute paths to this checkout:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "/path/to/CodeVaikhari/hooks/claude-session.py"}]}],
    "SessionEnd":   [{"hooks": [{"type": "command",
      "command": "/path/to/CodeVaikhari/hooks/claude-session.py"}]}],
    "Notification": [{"hooks": [{"type": "command",
      "command": "/path/to/CodeVaikhari/hooks/claude-notify.py"}]}],
    "Stop":         [{"hooks": [{"type": "command",
      "command": "/path/to/CodeVaikhari/hooks/claude-notify.py"}]}]
  }
}
```

| Hook | Effect |
|---|---|
| `SessionStart` | register the session, take the project's voice |
| `SessionEnd` | release it |
| `Notification` | speak what needs attention |
| `Stop` | speak the conclusion of what just happened |

### What Stop actually says

"*project* finished" tells you nothing you did not already know. The
conclusion is sitting in the transcript the hook is handed, so the hook reads
it: the last assistant message that is not mid-turn narration, stripped of
markdown, cut to the first couple of sentences (~200 characters).

In practice that turns *"agwx2026 finished"* into *"Done. Nine branches
deleted across both repos, all verified merged first. agwx is now clean."*

Because agents lead with the result, the first sentence is almost always the
useful one.

Preambles are not conclusions. "Let me check whether that stuck" is text, but
a tool call follows it, so it is not what you want read out. Transcripts store
one content block per row, so the hook walks back over assistant rows and
stops at the first `tool_use` it meets — if the turn was still working there
is no conclusion yet, and it falls back to "*project* finished".

Identical hook messages inside 10 minutes (`VAIKHARI_AUTO_DEDUP`) are dropped
too. Claude Code re-emits "is waiting for your input" while it sits idle, and
hearing it five times tells you nothing the first one did not.

### Always naming the project

Every spoken message is prefixed with its project — *"CodeVaikhari: branches
cleaned in both repos"* — including a bare `say "done"` from a shell. The
voice identifies the project too, but only once you have learned the voices,
and a message you cannot place is not much better than no message.

The daemon adds it, not the hooks, so it applies to everything. Text that
already starts with the project name is left alone.

### Not saying it twice

Hook messages are sent with `--auto`, which means *skippable*. If the session
already said something deliberate with `say` in the last 120 seconds
(`VAIKHARI_AUTO_WINDOW`), the daemon drops the hook message rather than
announcing the same news again.

So an agent that summarises its own work out loud silences the hook by doing
so, and one that says nothing still gets announced. The window is tracked at
enqueue rather than from the archive, because rows are written when playback
*ends* — a manual message still being spoken would otherwise be invisible to
the hook right behind it.

All hooks are fire-and-forget. They never add latency to a session, and a TTS
failure never breaks Claude Code.

## Config

Read by both client and daemon:

| | |
|---|---|
| `VAIKHARI_VOICE` | default voice (`af_heart`) |
| `VAIKHARI_DEVICE` | `cuda` or `cpu` |
| `VAIKHARI_UI_PORT` | UI port (8765) |
| `VAIKHARI_GAP` | silence between utterances, seconds (3.0) |
| `VAIKHARI_AUTO_WINDOW` | suppress a hook message this long after a manual one (120s) |
| `VAIKHARI_AUTO_DEDUP` | suppress an identical hook message within this long (600s) |
| `VAIKHARI_SOCKET` | socket path |
| `VAIKHARI_VENV` | venv location |

## Troubleshooting

**`nvidia-smi` reports an NVML version mismatch.** That breaks the monitoring
interface only. The CUDA runtime is usually fine — check with
`python -c "import torch; torch.randn(8, device='cuda')"` before assuming you
need a reboot.

**Install fails on the spaCy model.** misaki's downloader shells out to `pip`,
which breaks under a `uv` shim. `install.sh` installs the `en_core_web_sm`
wheel directly for this reason.

**Nothing plays.** Check the sink with `wpctl status`, and test the path
directly: `pw-play --format=s16 --rate=24000 --channels=1 - < /dev/zero`.

**Logs.** `journalctl --user -u vaikhari -f`, or
`~/.local/state/vaikhari/daemon.log` when started without systemd.

## Remote access

Reaching the inbox from a phone is designed but not built. See
[docs/REMOTE.md](docs/REMOTE.md) for the sketch: the prerequisites that apply
under any transport (auth, bind config, PWA shell), the transport options,
Web Push, and away mode.

The one hard rule from it: **the HTTP server has no authentication today** and
is safe only because it binds loopback. Do not bind it wider until that lands.

## Licence

MIT — see [LICENSE](LICENSE). Kokoro-82M is Apache-2.0 and is downloaded at
install time, not vendored here.
