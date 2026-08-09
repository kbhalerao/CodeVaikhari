#!/usr/bin/env python
"""CodeVaikhari: warm Kokoro TTS daemon, inbox, and local review UI.

Keeps the model resident so `say` costs ~130ms instead of the ~10.7s a cold
torch + model load takes per invocation. Every utterance is synthesized,
stored in SQLite with its audio, and either spoken immediately or held in an
inbox when muted.

Socket protocol: one JSON object per line, one reply line.
  {"text":..,"voice":..,"speed":..,"session":..,"wait":bool,"out":path}
  {"stop":true}                     kill current playback, drop the queue
  {"mute":true|false}               global mute
  {"play_pending":true}             drain the inbox through the speakers
  {"status":true}                   -> {"muted":bool,"pending":int,"ui":url}
"""
import fcntl
import json
import os
import queue
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import wave
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

warnings.filterwarnings("ignore")

SOCKET_PATH = os.environ.get("VAIKHARI_SOCKET") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "vaikhari.sock"
)
STATE = os.path.expanduser("~/.local/state/vaikhari")
DB_PATH = os.path.join(STATE, "vaikhari.db")
UI_PORT = int(os.environ.get("VAIKHARI_UI_PORT", "8765"))
# Bind and advertised address are different things: binding to a single LAN
# address silently breaks 127.0.0.1, which is where the desktop UI and
# `say --ui` look. Bind 0.0.0.0 to serve both, and advertise a name a phone
# can actually type.
BIND = os.environ.get("VAIKHARI_BIND", "127.0.0.1")
HOST = os.environ.get("VAIKHARI_HOST") or (
    socket.getfqdn() if BIND == "0.0.0.0" else BIND)
# Loopback needs no token: reaching it already means local access. Anything
# else does, because the message text is genuinely sensitive.
NO_AUTH = os.environ.get("VAIKHARI_NO_AUTH") == "1"
SAMPLE_RATE = 24000
DEFAULT_VOICE = os.environ.get("VAIKHARI_VOICE", "af_heart")
DEVICE = os.environ.get("VAIKHARI_DEVICE", "cuda")
# This is a notifier, not an archive. A dismissed message is done with; keep
# just enough of them to replay one you cleared by mistake, and delete the
# rest outright — audio and text together. The byte cap is the backstop for
# the rare very long message, since audio is 48 KB/s and text is nothing.
KEEP_DISMISSED = int(os.environ.get("VAIKHARI_KEEP", "50"))
KEEP_BYTES = int(os.environ.get("VAIKHARI_KEEP_BYTES", str(100 * 1024 * 1024)))
GAP = float(os.environ.get("VAIKHARI_GAP", "3.0"))   # silence between utterances
# A hook-generated message is suppressed if the same session said something
# deliberate this recently. Claude summarising its own work and then a hook
# announcing the same conclusion is the same news twice.
AUTO_WINDOW = float(os.environ.get("VAIKHARI_AUTO_WINDOW", "120"))
# An identical hook message repeated inside this window is dropped. Claude Code
# re-emits "is waiting for your input" while it sits idle; hearing it five
# times tells you nothing the first one did not.
AUTO_DEDUP = float(os.environ.get("VAIKHARI_AUTO_DEDUP", "600"))
TRACE = os.environ.get("VAIKHARI_TRACE") == "1"   # log every playback start/end

# All 28 English voices, ordered by two criteria at once: the model card's
# quality grade (best first, so a handful of sessions all get good voices) and
# alternating accent/gender (so consecutive sessions are easy to tell apart
# rather than being three similar American females). Grades in comments.
VOICE_POOL = [
    "af_heart",     # A
    "bm_george",    # C
    "af_bella",     # A-
    "am_michael",   # C+
    "bf_emma",      # B-
    "am_fenrir",    # C+
    "af_nicole",    # B-
    "bm_fable",     # C
    "af_aoede",     # C+
    "am_puck",      # C+
    "bf_isabella",  # C
    "af_kore",      # C+
    "bm_lewis",     # D+
    "af_sarah",     # C+
    "am_onyx",      # D
    "af_nova",      # C
    "bf_lily",      # D
    "am_eric",      # D
    "af_sky",       # C-
    "bm_daniel",    # D
    "am_liam",      # D
    "af_alloy",     # C
    "bf_alice",     # D
    "am_echo",      # D
    "af_jessica",   # D
    "af_river",     # D
    "am_adam",      # F+
    "am_santa",     # D-
]

GRADES = {
    "af_heart": "A", "af_bella": "A-", "af_nicole": "B-", "bf_emma": "B-",
    "af_aoede": "C+", "af_kore": "C+", "af_sarah": "C+", "am_fenrir": "C+",
    "am_michael": "C+", "am_puck": "C+", "af_alloy": "C", "af_nova": "C",
    "bf_isabella": "C", "bm_fable": "C", "bm_george": "C", "af_sky": "C-",
    "bm_lewis": "D+", "af_jessica": "D", "af_river": "D", "am_echo": "D",
    "am_eric": "D", "am_liam": "D", "am_onyx": "D", "bf_alice": "D",
    "bf_lily": "D", "bm_daniel": "D", "am_santa": "D-", "am_adam": "F+",
}

_pipelines = {}
_jobs = queue.Queue()
_current = None
_current_lock = threading.Lock()
_db_lock = threading.RLock()
_db = None
_last_drain_end = 0.0
_drain_pending = 0
_last_play_end = 0.0
_last_manual = {}
_last_auto = {}
_floor_until = 0.0


def gap():
    """Hold the floor between utterances. Enforced as a minimum interval since
    the last playback ended, not a blanket sleep, so a quiet system still
    speaks immediately and only back-to-back messages get spaced.

    Also waits out a browser holding the floor. The inbox player decodes in the
    tab, so it is the one audio path into these speakers that the queue cannot
    see; polled rather than slept through, because the hold can be extended or
    released while we are already waiting."""
    while True:
        remaining = max(GAP - (time.time() - _last_play_end),
                        _floor_until - time.time())
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


def trace(msg):
    """Playback tracing, off by default. Overlapping audio is the one bug you
    cannot diagnose after the fact, so leave the instrument in the drawer."""
    if TRACE:
        log(msg)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- storage

def db():
    global _db
    if _db is None:
        os.makedirs(STATE, exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS utterances(
                id      TEXT PRIMARY KEY,
                ts      REAL NOT NULL,
                session TEXT NOT NULL,
                voice   TEXT NOT NULL,
                text    TEXT NOT NULL,
                dur     REAL NOT NULL,
                played  INTEGER NOT NULL DEFAULT 0,
                audio   BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_ts ON utterances(ts DESC);
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voices(
                session TEXT PRIMARY KEY, voice TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                sid     TEXT PRIMARY KEY,
                label   TEXT NOT NULL,
                project TEXT NOT NULL,
                cwd     TEXT NOT NULL,
                voice   TEXT NOT NULL,
                started REAL NOT NULL,
                ended   REAL);
            CREATE INDEX IF NOT EXISTS idx_live ON sessions(ended);
        """)
        cols = {r[1] for r in _db.execute("PRAGMA table_info(voices)")}
        if "pinned" not in cols:              # migration for pre-pinning dbs
            _db.execute("ALTER TABLE voices ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        cols = {r[1] for r in _db.execute("PRAGMA table_info(utterances)")}
        if "dismissed" not in cols:           # migration for pre-inbox dbs
            _db.execute("ALTER TABLE utterances "
                        "ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            _db.execute("UPDATE utterances SET dismissed=1")
        _db.commit()
    return _db


def pin_project(project, voice, pinned=True):
    with _db_lock:
        db().execute(
            "INSERT INTO voices(session,voice,pinned) VALUES(?,?,?) "
            "ON CONFLICT(session) DO UPDATE SET voice=excluded.voice, "
            "pinned=excluded.pinned", (project, voice, 1 if pinned else 0))
        db().execute("UPDATE sessions SET voice=? WHERE project=? AND ended IS NULL",
                     (voice, project))
        db().commit()
    return {"project": project, "voice": voice, "pinned": pinned}


def unpin_project(project):
    """Clear the pin only. Live sessions keep the voice they are already using
    for the rest of their life — reshuffling mid-session would mean the thing
    you just learned to recognise changes under you. New sessions in this
    project go back to automatic allocation."""
    with _db_lock:
        db().execute("UPDATE voices SET pinned=0 WHERE session=?", (project,))
        db().commit()
    return {"project": project, "pinned": False}


def pinned_voice(project):
    with _db_lock:
        row = db().execute(
            "SELECT voice FROM voices WHERE session=? AND pinned=1", (project,)).fetchone()
    return row[0] if row else None


def list_pins():
    with _db_lock:
        return [{"project": r[0], "voice": r[1]} for r in db().execute(
            "SELECT session,voice FROM voices WHERE pinned=1 ORDER BY session")]


def setting(key, default=None, value=None):
    with _db_lock:
        if value is not None:
            db().execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            db().commit()
            return value
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def ui_token():
    """Stable per-install token, minted on first use and kept in settings."""
    tok = setting("ui_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        setting("ui_token", value=tok)
    return tok


def is_muted():
    return setting("muted", "0") == "1"


def pending_count():
    """The inbox is everything you have not dismissed. `played` is a separate
    axis — whether it has ever been spoken aloud — so a message you heard but
    did not act on still counts as waiting."""
    with _db_lock:
        return db().execute(
            "SELECT COUNT(*) FROM utterances WHERE dismissed=0").fetchone()[0]


def dismiss(ids=None):
    with _db_lock:
        if ids is None:
            n = db().execute("UPDATE utterances SET dismissed=1 "
                             "WHERE dismissed=0").rowcount
        else:
            n = db().executemany(
                "UPDATE utterances SET dismissed=1 WHERE id=? AND dismissed=0",
                [(i,) for i in ids]).rowcount
        db().commit()
    return n


def dismiss_session(label):
    """Clear a session's inbox because you went back to that session.

    Only messages that were actually spoken: one queued while muted was never
    heard, so returning to the session is no reason to drop it silently."""
    with _db_lock:
        n = db().execute(
            "UPDATE utterances SET dismissed=1 "
            "WHERE session=? AND dismissed=0 AND played=1", (label,)).rowcount
        db().commit()
    return n


def delete(uid):
    with _db_lock:
        n = db().execute("DELETE FROM utterances WHERE id=?", (uid,)).rowcount
        db().commit()
    return n


def prefs():
    return {"repeat_enabled": setting("repeat_enabled", "1") == "1",
            "repeat_minutes": int(setting("repeat_minutes", "10"))}


def set_prefs(d):
    if "repeat_enabled" in d:
        setting("repeat_enabled", value="1" if d["repeat_enabled"] else "0")
    if "repeat_minutes" in d:
        setting("repeat_minutes", value=str(max(1, min(240, int(d["repeat_minutes"])))))
    return prefs()


def voice_for_project(project):
    """A project's voice is sticky: assigned once, then read back from the
    voices table forever. New projects take the next voice no other project
    already owns, so every project sounds different without anyone choosing.

    Uniqueness is against every project ever seen, not just live ones. That
    costs a pool slot per project but means a voice always means the same
    repo — which is the whole point of hearing which session spoke."""
    with _db_lock:
        row = db().execute(
            "SELECT voice FROM voices WHERE session=?", (project,)).fetchone()
        if row:
            return row[0]
        taken = {r[0] for r in db().execute("SELECT voice FROM voices")}
        pick = next((v for v in VOICE_POOL if v not in taken),
                    VOICE_POOL[len(taken) % len(VOICE_POOL)])
        db().execute("INSERT INTO voices(session,voice) VALUES(?,?)", (project, pick))
        db().commit()
    return pick


def register_session(sid, cwd, label=None):
    """Record a live session and give it its project's voice.

    The voice is a cached preference, not a fresh allocation: see
    voice_for_project. Two sessions in the same repo therefore sound the same
    — they are the same project, and the label distinguishes them in the UI.
    Across repos, a voice always means one repo."""
    project = os.path.basename((cwd or "").rstrip("/")) or "claude"
    label = label or f"{project}.{sid[:4]}" if sid else project
    # An explicit pin is your stated preference and outranks the cache.
    pick = pinned_voice(project) or voice_for_project(project)
    with _db_lock:
        db().execute(
            "INSERT INTO sessions(sid,label,project,cwd,voice,started,ended) "
            "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(sid) DO UPDATE SET "
            "label=excluded.label, cwd=excluded.cwd, voice=excluded.voice, ended=NULL",
            (sid, label, project, cwd or "", pick, time.time()))
        db().commit()
    log(f"session {label} ({project}) -> {pick}")
    return {"voice": pick, "label": label, "project": project}


def end_session(key):
    """Accepts a session id, or a cwd — /close knows its directory but not
    the session id the hook registered under."""
    with _db_lock:
        cur = db().execute(
            "UPDATE sessions SET ended=? WHERE sid=? AND ended IS NULL",
            (time.time(), key))
        if cur.rowcount == 0:
            cur = db().execute(
                "UPDATE sessions SET ended=? WHERE cwd=? AND ended IS NULL",
                (time.time(), key.rstrip("/")))
        db().commit()
        return cur.rowcount


def list_sessions():
    with _db_lock:
        rows = db().execute(
            "SELECT sid,label,project,cwd,voice,started,ended FROM sessions "
            "ORDER BY ended IS NOT NULL, started DESC LIMIT 40").fetchall()
    keys = ("sid", "label", "project", "cwd", "voice", "started", "ended")
    return [dict(zip(keys, r)) for r in rows]


def resolve_session(key, cwd=None):
    """`say -S X`: X may be a registered session id, or a free-form label for
    ad-hoc CLI use. Returns (display label, voice).

    If a session id speaks without having registered — it predates the hook,
    or SessionStart never fired — and it told us its cwd, register it now.
    That keeps voices consistent instead of re-deriving a different one per
    utterance."""
    if not key:
        # No session id, but we know the directory: attribute to the project
        # so a shell `say` in a repo matches that repo's voice.
        if cwd:
            project = os.path.basename(cwd.rstrip("/")) or "cli"
            return project, voice_for_project(project)
        return "cli", DEFAULT_VOICE
    with _db_lock:
        row = db().execute(
            "SELECT label,voice FROM sessions WHERE sid=? ORDER BY started DESC LIMIT 1",
            (key,)).fetchone()
    if row:
        return row[0], row[1]
    if cwd:
        info = register_session(key, cwd)
        return info["label"], info["voice"]

    project = key.split(".")[0]
    return key, voice_for_project(project)


def wav_bytes(pcm):
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def store(entry, pcm):
    with _db_lock:
        db().execute(
            "INSERT INTO utterances(id,ts,session,voice,text,dur,played,audio) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (entry["id"], entry["ts"], entry["session"], entry["voice"],
             entry["text"], entry["dur"], entry["played"], wav_bytes(pcm)))
        # Prune dismissed history only; the inbox is not backlog.
        prune()


def prune():
    """Keep the newest KEEP_DISMISSED dismissed messages, under KEEP_BYTES of
    audio. Everything else goes. The inbox is exempt: an undismissed message
    is not backlog, at any age or size."""
    with _db_lock:
        db().execute(
            "DELETE FROM utterances WHERE dismissed=1 AND id NOT IN "
            "(SELECT id FROM utterances WHERE dismissed=1 "
            " ORDER BY ts DESC LIMIT ?)", (KEEP_DISMISSED,))
        total = db().execute(
            "SELECT COALESCE(SUM(LENGTH(audio)),0) FROM utterances").fetchone()[0]
        if total > KEEP_BYTES:
            for uid, n in db().execute(
                    "SELECT id,LENGTH(audio) FROM utterances WHERE dismissed=1 "
                    "ORDER BY ts ASC"):
                db().execute("DELETE FROM utterances WHERE id=?", (uid,))
                total -= n
                if total <= KEEP_BYTES:
                    break
        # Leftovers from when expiry blanked the blob instead of deleting.
        db().execute("DELETE FROM utterances WHERE dismissed=1 AND LENGTH(audio)=0")
        db().commit()


def mark_played(ids):
    with _db_lock:
        db().executemany("UPDATE utterances SET played=1 WHERE id=?",
                         [(i,) for i in ids])
        db().commit()


def history(limit=120):
    with _db_lock:
        rows = db().execute(
            "SELECT id,ts,session,voice,text,dur,played,dismissed FROM utterances "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    keys = ("id", "ts", "session", "voice", "text", "dur", "played", "dismissed")
    return [dict(zip(keys, r)) for r in rows]


def audio_of(uid):
    with _db_lock:
        row = db().execute(
            "SELECT audio FROM utterances WHERE id=?", (uid,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- synthesis

def pipeline_for(voice):
    lang = voice[0] if voice[:1] in ("a", "b") else "a"
    if lang not in _pipelines:
        from kokoro import KPipeline
        _pipelines[lang] = KPipeline(
            lang_code=lang, repo_id="hexgrad/Kokoro-82M", device=DEVICE)
    return _pipelines[lang]


def play_pcm(pcm):
    """Blocking playback of a complete buffer, interruptible via stop."""
    global _current, _last_play_end
    gap()
    proc = subprocess.Popen(
        ["pw-play", "--format=s16", f"--rate={SAMPLE_RATE}", "--channels=1", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    trace(f"play start pid={proc.pid} thread={threading.current_thread().name}")
    with _current_lock:
        _current = proc
    try:
        proc.stdin.write(pcm)
        proc.stdin.close()
        proc.wait()
    except (BrokenPipeError, OSError):
        pass                                   # interrupted by --stop
    finally:
        with _current_lock:
            if _current is proc:
                _current = None
        trace(f"play end pid={proc.pid}")
        _last_play_end = time.time()


def speak(job):
    """Synthesize, streaming to the speakers as chunks land so playback starts
    on the first sentence, while accumulating full PCM for the archive."""
    global _current, _last_play_end
    import numpy as np

    voice, text = job["voice"], job["text"]
    # A preview is an explicit click on "let me hear this voice", so it plays
    # through mute and is never archived.
    ephemeral = job.get("ephemeral")
    out_path = job.get("out")
    muted = is_muted() and not ephemeral
    live = not out_path and not muted
    proc, parts = None, []
    try:
        for _, _, audio in pipeline_for(voice)(text, voice=voice, speed=job["speed"]):
            pcm = (np.clip(np.asarray(audio, dtype="float32"), -1, 1) * 32767).astype("<i2")
            parts.append(pcm)
            if not live:
                continue
            if proc is None:
                gap()
                proc = subprocess.Popen(
                    ["pw-play", "--format=s16", f"--rate={SAMPLE_RATE}",
                     "--channels=1", "-"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                trace(f"speak start pid={proc.pid} "
                      f"thread={threading.current_thread().name} {text[:30]!r}")
                with _current_lock:
                    _current = proc
            proc.stdin.write(pcm.tobytes())
    except BrokenPipeError:
        pass
    finally:
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait()
            except (BrokenPipeError, OSError):
                pass
            with _current_lock:
                if _current is proc:
                    _current = None
            trace(f"speak end pid={proc.pid}")
            _last_play_end = time.time()

    if not parts:
        return
    raw = np.concatenate(parts).tobytes()
    if out_path:
        with open(out_path, "wb") as f:
            f.write(wav_bytes(raw))
        return
    if ephemeral:
        return
    store({
        "id": f"{int(time.time()*1000)}-{os.urandom(3).hex()}",
        "ts": time.time(),
        "session": job.get("session") or "cli",
        "voice": voice,
        "text": text,
        "dur": round(len(raw) / 2 / SAMPLE_RATE, 2),
        "played": 0 if muted else 1,
    }, raw)


def drain_inbox():
    """Speak everything undismissed, oldest first. No cap: a message repeats
    until you dismiss it, and dismissing is how you make it stop."""
    with _db_lock:
        rows = db().execute(
            "SELECT id,audio FROM utterances WHERE dismissed=0 "
            "ORDER BY ts ASC").fetchall()
    for uid, blob in rows:
        if not blob:
            continue                           # audio expired; text still kept
        play_pcm(blob[44:])                    # strip the 44-byte WAV header
        mark_played([uid])


def queue_drain():
    """Every drain goes through here so the repeater can see one is already
    queued or speaking. A drain runs for as long as it takes to say everything,
    which is easily longer than the poll interval."""
    global _drain_pending
    _drain_pending += 1
    submit({"kind": "drain"})


def maintenance():
    """Hourly: apply the same retention as a new message would, and reclaim
    pages. Needed because pruning otherwise only happens when something new
    arrives, and a quiet machine would sit on whatever it last held."""
    while True:
        time.sleep(3600)
        try:
            before = db().execute("SELECT COUNT(*) FROM utterances").fetchone()[0]
            prune()
            with _db_lock:
                db().execute(
                    "DELETE FROM sessions WHERE ended IS NOT NULL "
                    "AND sid NOT IN (SELECT DISTINCT session FROM utterances)")
                db().commit()
                after = db().execute(
                    "SELECT COUNT(*) FROM utterances").fetchone()[0]
                if after < before:
                    db().execute("VACUUM")
            if after < before:
                log(f"maintenance: dropped {before - after}, vacuumed")
        except Exception as exc:
            log(f"maintenance: {type(exc).__name__}: {exc}")


def repeater():
    """Drain the inbox every N minutes, timed from the end of the last drain.

    Deliberately coarse. Timing it from each message's arrival instead meant a
    busy inbox never repeated at all, because new arrivals kept pushing the
    clock forward. Paused while muted: mute means make no noise, and a
    reminder is noise."""
    while True:
        time.sleep(15)
        try:
            p = prefs()
            if not p["repeat_enabled"] or is_muted() or not pending_count():
                continue
            if _drain_pending:                 # already queued or speaking
                continue
            if time.time() - _last_drain_end < p["repeat_minutes"] * 60:
                continue
            log(f"repeating {pending_count()} undismissed")
            queue_drain()
        except Exception as exc:
            log(f"repeater: {type(exc).__name__}: {exc}")


def is_speaking():
    with _current_lock:
        return _current is not None and _current.poll() is None


def stop_current():
    with _current_lock:
        if _current is not None and _current.poll() is None:
            _current.kill()


def hold_floor(seconds):
    """A browser tab claims the speakers for `seconds`, or releases them at 0.

    The inbox player is a plain <audio> element: it never reaches this process,
    so the job queue cannot serialise it. Clicking play is deliberate, so it
    wins over a notification already speaking. Bounded, and the tab sends the
    clip's remaining time, so a tab that dies mid-clip frees the floor anyway."""
    global _floor_until
    if seconds:
        _floor_until = time.time() + min(float(seconds), 300)
        stop_current()
    else:
        _floor_until = 0.0
    return round(max(0.0, _floor_until - time.time()), 1)


def worker():
    """Single consumer: overlapping notifications queue up instead of talking
    over each other."""
    global _last_drain_end, _drain_pending
    while True:
        job, done = _jobs.get()
        try:
            if job.get("kind") == "drain":
                try:
                    drain_inbox()
                finally:
                    # Stamp at the end, so the next repeat is N minutes after
                    # this one stopped talking rather than after it started.
                    _last_drain_end = time.time()
                    _drain_pending = max(0, _drain_pending - 1)
            elif job.get("kind") == "replay":
                blob = audio_of(job["id"])
                if blob:
                    play_pcm(blob[44:])
                    mark_played([job["id"]])
            else:
                speak(job)
        except Exception as exc:
            log(f"job error: {type(exc).__name__}: {exc}")
        finally:
            done.set()
            _jobs.task_done()


def submit(job, wait=False):
    done = threading.Event()
    _jobs.put((job, done))
    if wait:
        done.wait(timeout=600)
    return done


# ---------------------------------------------------------------- socket

def handle(conn):
    with conn:
        conn.settimeout(600)
        f = conn.makefile("rwb")
        line = f.readline()
        if not line:
            return

        def reply(obj):
            f.write((json.dumps(obj) + "\n").encode())
            f.flush()

        try:
            req = json.loads(line)
        except ValueError as exc:
            return reply({"ok": False, "error": str(exc)})

        if req.get("stop"):
            while not _jobs.empty():
                try:
                    _, d = _jobs.get_nowait(); d.set(); _jobs.task_done()
                except queue.Empty:
                    break
            stop_current()
            return reply({"ok": True})

        if "mute" in req:
            setting("muted", value="1" if req["mute"] else "0")
            if req["mute"]:
                stop_current()
            return reply({"ok": True, "muted": is_muted()})

        if req.get("play_pending"):
            queue_drain()
            return reply({"ok": True})

        if req.get("replay"):
            submit({"kind": "replay", "id": req["replay"]}, wait=req.get("wait"))
            return reply({"ok": True})

        if req.get("register"):
            r = req["register"]
            info = register_session(r.get("sid") or "", r.get("cwd") or "",
                                    r.get("label"))
            return reply({"ok": True, **info})

        if req.get("unregister"):
            return reply({"ok": True, "freed": end_session(req["unregister"])})

        if req.get("sessions"):
            return reply({"ok": True, "sessions": list_sessions()})

        if req.get("pin"):
            p = req["pin"]
            project = p.get("project") or os.path.basename(
                (p.get("cwd") or "").rstrip("/")) or "claude"
            if p.get("pinned") is False:
                return reply({"ok": True, **unpin_project(project)})
            return reply({"ok": True, **pin_project(project, p["voice"])})

        if req.get("pins"):
            return reply({"ok": True, "pins": list_pins()})

        if req.get("preview"):
            voice = req["preview"]
            submit({"text": req.get("text") or "This is how this session sounds.",
                    "session": "", "voice": voice, "speed": 1.0,
                    "out": None, "ephemeral": True}, wait=req.get("wait"))
            return reply({"ok": True})

        if req.get("dismiss_session"):
            label, _ = resolve_session(req["dismiss_session"], req.get("cwd"))
            n = dismiss_session(label)
            if n:
                log(f"{label} revisited; dismissed {n}")
            return reply({"ok": True, "dismissed": n, "session": label})

        if req.get("dismiss"):
            n = dismiss(None if req["dismiss"] is True else [req["dismiss"]])
            return reply({"ok": True, "dismissed": n})

        if req.get("prefs") is not None:
            p = set_prefs(req["prefs"]) if req["prefs"] else prefs()
            return reply({"ok": True, **p})

        if req.get("status"):
            return reply({"ok": True, "muted": is_muted(),
                          "pending": pending_count(),
                          "ui": f"http://{HOST}:{UI_PORT}", **prefs()})

        label, voice = resolve_session(req.get("session") or "", req.get("cwd"))

        # Tracked at enqueue, not from the utterances table: rows are written
        # when playback *ends*, so a manual message still being spoken would
        # not be visible yet and the hook right behind it would slip through.
        if req.get("auto"):
            now = time.time()
            since = now - _last_manual.get(label, 0)
            if since < AUTO_WINDOW:
                log(f"suppressed auto for {label} ({since:.0f}s after a manual)")
                return reply({"ok": True, "suppressed": True})
            prev_text, prev_ts = _last_auto.get(label, (None, 0))
            if prev_text == req.get("text", "") and now - prev_ts < AUTO_DEDUP:
                log(f"suppressed repeat auto for {label} "
                    f"({now - prev_ts:.0f}s since the same message)")
                return reply({"ok": True, "suppressed": True})
            _last_auto[label] = (req.get("text", ""), now)
        else:
            _last_manual[label] = time.time()

        # Always name the project aloud. The voice identifies it too, but only
        # once you have learned the voices, and a message you cannot place is
        # not much better than no message. Done here rather than in the hooks
        # so a bare `say "done"` gets it as well.
        text = req.get("text", "")
        project = label.split(".")[0]
        if (project and project != "cli"
                and not text.lower().startswith(project.lower())):
            text = f"{project}: {text}"

        submit({
            "text": text,
            "session": label,
            "voice": req.get("voice") or voice,
            "speed": float(req.get("speed") or 1.0),
            "out": req.get("out"),
        }, wait=req.get("wait"))
        reply({"ok": True, "muted": is_muted(), "voice": voice})


# ---------------------------------------------------------------- web UI

HERE = os.path.dirname(os.path.realpath(__file__))
UI_HTML = os.path.join(HERE, "ui.html")
AVATAR_DIR = os.path.join(HERE, "avatars")


class UI(BaseHTTPRequestHandler):
    """Serves the review UI. Requests from anywhere but loopback must carry the
    token, as a ?token= query (which sets a cookie and redirects, so a phone is
    enrolled by opening one link), a cookie, or a bearer header."""
    # HTTP/1.1 for keep-alive: mobile media clients open a connection per
    # range and 1.0 makes them reconnect for every chunk. Safe here because
    # every response sets an accurate Content-Length.
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorised(self):
        if NO_AUTH or self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        want = ui_token()
        got = ""
        if "token=" in self.path:
            got = self.path.split("token=", 1)[1].split("&")[0]
        if not got:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                got = auth[7:]
        if not got:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "vaikhari":
                    got = v
        return bool(got) and secrets.compare_digest(got, want)

    def _gate(self):
        """Returns True if the request was handled here (rejected or
        redirected) and the caller should stop."""
        if self._authorised():
            # Enrol: swap ?token= for a cookie so the URL can be dropped.
            if "token=" in self.path and self.command == "GET":
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"vaikhari={ui_token()}; Max-Age=31536000; Path=/; "
                    f"SameSite=Lax; HttpOnly")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            return False
        self._send(401, "text/plain; charset=utf-8",
                   b"Code Vaikhari: token required.\n"
                   b"Open the URL printed in the daemon log.\n")
        return True

    def do_GET(self):
        if self._gate():
            return
        if self.path in ("/", "/index.html"):
            with open(UI_HTML) as fh:          # read per request, so editing
                page = fh.read()               # ui.html needs no restart
            page = page.replace("__VOICES__", json.dumps(
                [[v, GRADES.get(v, "")] for v in VOICE_POOL]))
            page = page.replace("__HOME__", json.dumps(os.path.expanduser("~")))
            # Tell the page which voices have artwork; the rest fall back to
            # a generated monogram, so a checkout without avatars/ still works.
            try:
                have = sorted(f[:-5] for f in os.listdir(AVATAR_DIR)
                              if f.endswith(".webp"))
            except OSError:
                have = []
            page = page.replace("__AVATARS__", json.dumps(have))
            return self._send(200, "text/html; charset=utf-8", page.encode())
        if self.path == "/api/history":
            return self._send(200, "application/json", json.dumps(history()).encode())
        if self.path == "/api/status":
            return self._send(200, "application/json", json.dumps(
                {"muted": is_muted(), "pending": pending_count(),
                 "speaking": is_speaking(), **prefs()}).encode())
        if self.path == "/api/sessions":
            return self._send(200, "application/json",
                              json.dumps(list_sessions()).encode())
        if self.path == "/logo.svg":
            with open(os.path.join(HERE, "logo.svg"), "rb") as fh:
                return self._send(200, "image/svg+xml", fh.read())
        if self.path.startswith("/avatars/"):
            name = os.path.basename(self.path[len("/avatars/"):])
            path = os.path.join(AVATAR_DIR, name)
            if name.endswith(".webp") and os.path.isfile(path):
                with open(path, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/webp")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self._send(404, "text/plain", b"not found")
        if self.path.startswith("/audio/"):
            uid = os.path.basename(self.path[len("/audio/"):]).removesuffix(".wav")
            blob = audio_of(uid)
            if blob:
                return self._send_audio(blob)
        self._send(404, "text/plain", b"not found")

    def _send_audio(self, blob):
        """Honour Range requests. Mobile Safari asks for bytes=0- before it
        will play anything, and refuses a plain 200 for media."""
        rng = self.headers.get("Range", "")
        total = len(blob)
        start, end = 0, total - 1
        partial = False
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else total - 1
                elif b:                        # suffix form: bytes=-500
                    start = max(0, total - int(b))
                partial = True
            except ValueError:
                partial = False
            if start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            end = min(end, total - 1)
        body = blob[start:end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self._gate():
            return
        if self.path == "/api/stop":
            stop_current()
            return self._send(200, "application/json", b'{"ok":true}')
        if self.path == "/api/drain":
            queue_drain()
            return self._send(200, "application/json", b'{"ok":true}')
        if self.path == "/api/floor":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            return self._send(200, "application/json", json.dumps(
                {"held": hold_floor(body.get("seconds") or 0)}).encode())
        if self.path == "/api/mute":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            setting("muted", value="1" if body.get("mute") else "0")
            if body.get("mute"):
                stop_current()
            return self._send(200, "application/json",
                              json.dumps({"muted": is_muted()}).encode())
        if self.path == "/api/preview":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            submit({"text": body.get("text") or "This is how this session sounds.",
                    "session": "", "voice": body.get("voice") or DEFAULT_VOICE,
                    "speed": 1.0, "out": None, "ephemeral": True})
            return self._send(200, "application/json", b'{"ok":true}')
        if self.path == "/api/pin":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            return self._send(200, "application/json", json.dumps(
                pin_project(body["project"], body["voice"])).encode())
        if self.path == "/api/dismiss-all":
            return self._send(200, "application/json",
                              json.dumps({"dismissed": dismiss()}).encode())
        if self.path.startswith("/api/dismiss/"):
            return self._send(200, "application/json", json.dumps(
                {"dismissed": dismiss([os.path.basename(self.path[13:])])}).encode())
        if self.path.startswith("/api/delete/"):
            return self._send(200, "application/json", json.dumps(
                {"deleted": delete(os.path.basename(self.path[12:]))}).encode())
        if self.path == "/api/prefs":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            return self._send(200, "application/json",
                              json.dumps(set_prefs(body)).encode())
        if self.path.startswith("/api/heard/"):
            mark_played([os.path.basename(self.path[len("/api/heard/"):])])
            return self._send(200, "application/json", b'{"ok":true}')
        self._send(404, "text/plain", b"not found")


def serve_ui():
    try:
        srv = ThreadingHTTPServer((BIND, UI_PORT), UI)
    except OSError as exc:
        log(f"UI disabled ({exc})")
        return
    if BIND in ("127.0.0.1", "::1", "localhost"):
        log(f"UI on http://{HOST}:{UI_PORT}")
    elif NO_AUTH:
        log(f"UI on http://{HOST}:{UI_PORT}  *** NO AUTH — anyone on this "
            f"network can read and control it ***")
    else:
        # Printed every start, so the enrolment link is never hunted for.
        log(f"UI on http://{HOST}:{UI_PORT} (loopback exempt from the token)")
        log(f"  enrol a device once: "
            f"http://{HOST}:{UI_PORT}/?token={ui_token()}")
    srv.serve_forever()


# ---------------------------------------------------------------- main

def main():
    os.makedirs(STATE, exist_ok=True)

    # Exactly one daemon, whoever starts it. `say` auto-starts one when it
    # cannot connect, and a systemd restart leaves a ~14s window with no
    # socket, so a hook firing in that window used to spawn a second daemon.
    # It would then unlink and steal the socket while the first kept its own
    # repeater running — two processes speaking over each other. Taken before
    # the model loads, so the loser costs nothing.
    lock = open(os.path.join(STATE, "daemon.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another daemon holds the lock; exiting")
        return 0
    globals()["_lock"] = lock          # hold it open for the process lifetime

    db()
    log(f"loading kokoro on {DEVICE} ...")
    pipeline_for(DEFAULT_VOICE)
    try:                                       # pay JIT/autotune cost up front
        list(pipeline_for(DEFAULT_VOICE)("Ready.", voice=DEFAULT_VOICE))
    except Exception as exc:
        log(f"warmup failed (continuing): {exc}")

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    srv.listen(16)
    signal.signal(signal.SIGTERM, lambda *_: (stop_current(), sys.exit(0)))
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=serve_ui, daemon=True).start()
    # Sweep zombies: a session whose SessionEnd hook never fired (crash, kill
    # -9) would otherwise hold its voice forever and eventually exhaust the
    # pool. It keeps its voice if it speaks again — resolve_session looks up
    # by sid regardless of ended — only the live slot is released.
    with _db_lock:
        n = db().execute(
            "UPDATE sessions SET ended=? WHERE ended IS NULL AND started < ?",
            (time.time(), time.time() - 24 * 3600)).rowcount
        db().commit()
    if n:
        log(f"released {n} stale session(s) older than 24h")

    global _last_drain_end
    _last_drain_end = time.time()   # don't nag the instant the daemon restarts
    threading.Thread(target=repeater, daemon=True).start()
    threading.Thread(target=maintenance, daemon=True).start()
    p = prefs()
    log(f"ready on {SOCKET_PATH} (muted={is_muted()}, inbox={pending_count()}, "
        f"repeat={'every %dm' % p['repeat_minutes'] if p['repeat_enabled'] else 'off'})")

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        # Do not leave a player behind: an orphaned pw-play keeps talking over
        # whatever starts next, which is exactly the overlap this is meant to
        # prevent.
        stop_current()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    sys.exit(main())
