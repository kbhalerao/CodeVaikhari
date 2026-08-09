#!/usr/bin/env python
"""Warm Kokoro TTS daemon, inbox, and local review UI.

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
import json
import os
import queue
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

SOCKET_PATH = os.environ.get("KOKORO_SOCKET") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "kokoro-say.sock"
)
STATE = os.path.expanduser("~/.local/state/kokoro-say")
DB_PATH = os.path.join(STATE, "say.db")
UI_PORT = int(os.environ.get("KOKORO_UI_PORT", "8765"))
SAMPLE_RATE = 24000
DEFAULT_VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
DEVICE = os.environ.get("KOKORO_DEVICE", "cuda")
KEEP_PLAYED = 250          # already-heard utterances retained; inbox is never pruned

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
_db_lock = threading.Lock()
_db = None


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


def is_muted():
    return setting("muted", "0") == "1"


def pending_count():
    with _db_lock:
        return db().execute(
            "SELECT COUNT(*) FROM utterances WHERE played=0").fetchone()[0]


def register_session(sid, cwd, label=None):
    """Claim a voice for a live session.

    Uniqueness only has to hold among sessions that are currently running —
    that is what you actually need to tell two speakers apart. So a voice is
    released on session end and can be reused. A project keeps its previous
    voice when that voice is free, which keeps things recognisable across
    restarts without permanently burning a slot per project ever seen."""
    project = os.path.basename((cwd or "").rstrip("/")) or "claude"
    label = label or f"{project}.{sid[:4]}" if sid else project

    # An explicit pin is the user's stated preference and outranks everything,
    # including collision avoidance: if you pin two projects to one voice,
    # that is your call to make.
    pin = pinned_voice(project)

    with _db_lock:
        row = db().execute("SELECT voice FROM sessions WHERE sid=? AND ended IS NULL",
                           (sid,)).fetchone()
        if row and not pin:
            return {"voice": row[0], "label": label, "project": project}
        if pin:
            db().execute(
                "INSERT INTO sessions(sid,label,project,cwd,voice,started,ended) "
                "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(sid) DO UPDATE SET "
                "label=excluded.label, cwd=excluded.cwd, voice=excluded.voice, "
                "ended=NULL", (sid, label, project, cwd or "", pin, time.time()))
            db().commit()
            return {"voice": pin, "label": label, "project": project, "pinned": True}

        taken = {r[0] for r in db().execute(
            "SELECT voice FROM sessions WHERE ended IS NULL")}
        prev = db().execute(
            "SELECT voice FROM voices WHERE session=?", (project,)).fetchone()
        pick = prev[0] if prev and prev[0] not in taken else None
        if pick is None:
            pick = next((v for v in VOICE_POOL if v not in taken), None)
        if pick is None:                       # >11 live sessions; wrap round
            pick = VOICE_POOL[len(taken) % len(VOICE_POOL)]

        db().execute(
            "INSERT INTO sessions(sid,label,project,cwd,voice,started,ended) "
            "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(sid) DO UPDATE SET "
            "label=excluded.label, cwd=excluded.cwd, voice=excluded.voice, ended=NULL",
            (sid, label, project, cwd or "", pick, time.time()))
        db().execute(
            "INSERT INTO voices(session,voice) VALUES(?,?) "
            "ON CONFLICT(session) DO UPDATE SET voice=excluded.voice", (project, pick))
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
    with _db_lock:
        row = db().execute(
            "SELECT voice FROM voices WHERE session=?", (project,)).fetchone()
        # Never hand out a voice a live session is already using, even on the
        # ad-hoc path — two speakers sounding identical defeats the point.
        live = {r[0] for r in db().execute(
            "SELECT voice FROM sessions WHERE ended IS NULL")}
        if row and row[0] not in live:
            return key, row[0]
        taken = live | {r[0] for r in db().execute("SELECT voice FROM voices")}
        pick = next((v for v in VOICE_POOL if v not in taken),
                    next((v for v in VOICE_POOL if v not in live), VOICE_POOL[0]))
        db().execute("INSERT INTO voices(session,voice) VALUES(?,?) "
                     "ON CONFLICT(session) DO UPDATE SET voice=excluded.voice",
                     (project, pick))
        db().commit()
    return key, pick


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
        # Prune heard history only. Anything still unplayed is inbox, not backlog.
        db().execute(
            "DELETE FROM utterances WHERE played=1 AND id NOT IN "
            "(SELECT id FROM utterances WHERE played=1 ORDER BY ts DESC LIMIT ?)",
            (KEEP_PLAYED,))
        db().commit()


def mark_played(ids):
    with _db_lock:
        db().executemany("UPDATE utterances SET played=1 WHERE id=?",
                         [(i,) for i in ids])
        db().commit()


def history(limit=120):
    with _db_lock:
        rows = db().execute(
            "SELECT id,ts,session,voice,text,dur,played FROM utterances "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    keys = ("id", "ts", "session", "voice", "text", "dur", "played")
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
    global _current
    proc = subprocess.Popen(
        ["pw-play", "--format=s16", f"--rate={SAMPLE_RATE}", "--channels=1", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def speak(job):
    """Synthesize, streaming to the speakers as chunks land so playback starts
    on the first sentence, while accumulating full PCM for the archive."""
    global _current
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
                proc = subprocess.Popen(
                    ["pw-play", "--format=s16", f"--rate={SAMPLE_RATE}",
                     "--channels=1", "-"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
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
    """Speak everything unplayed, oldest first, marking as it goes so an
    interruption doesn't replay what you already heard."""
    with _db_lock:
        rows = db().execute(
            "SELECT id,audio FROM utterances WHERE played=0 ORDER BY ts ASC").fetchall()
    for uid, blob in rows:
        play_pcm(blob[44:])                    # strip the 44-byte WAV header
        mark_played([uid])


def stop_current():
    with _current_lock:
        if _current is not None and _current.poll() is None:
            _current.kill()


def worker():
    """Single consumer: overlapping notifications queue up instead of talking
    over each other."""
    while True:
        job, done = _jobs.get()
        try:
            if job.get("kind") == "drain":
                drain_inbox()
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
            submit({"kind": "drain"}, wait=req.get("wait"))
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

        if req.get("status"):
            return reply({"ok": True, "muted": is_muted(),
                          "pending": pending_count(),
                          "ui": f"http://127.0.0.1:{UI_PORT}"})

        label, voice = resolve_session(req.get("session") or "", req.get("cwd"))
        submit({
            "text": req.get("text", ""),
            "session": label,
            "voice": req.get("voice") or voice,
            "speed": float(req.get("speed") or 1.0),
            "out": req.get("out"),
        }, wait=req.get("wait"))
        reply({"ok": True, "muted": is_muted(), "voice": voice})


# ---------------------------------------------------------------- web UI

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>say</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='13' font-size='14'>%F0%9F%94%8A</text></svg>">
<style>
:root{--bg:#faf9f7;--fg:#1a1a1a;--dim:#6b6b6b;--card:#fff;--line:#e5e3df;
      --accent:#2563eb;--warn:#b45309;--warnbg:#fef3c7}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#16161a;--fg:#e8e6e3;--dim:#8f8d89;--card:#1e1e24;--line:#2e2e36;
  --accent:#7aa2f7;--warn:#fbbf24;--warnbg:#3a2f12}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
 padding:12px 20px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;z-index:5}
h1{font-size:16px;margin:0 6px 0 0;font-weight:600;letter-spacing:-.01em}
.spacer{flex:1}
button,select{font:inherit;font-size:13px;background:var(--card);color:var(--fg);
 border:1px solid var(--line);border-radius:7px;padding:5px 11px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.on{background:var(--warnbg);border-color:var(--warn);color:var(--warn);font-weight:600}
#banner{display:none;background:var(--warnbg);color:var(--warn);padding:8px 20px;
 font-size:13px;font-weight:500;border-bottom:1px solid var(--line)}
#banner.show{display:block}
main{padding:16px 20px 60px;max-width:900px;margin:0 auto}
.row{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--sc);
 border-radius:9px;padding:11px 14px;margin-bottom:9px;position:relative}
.row.unheard{box-shadow:inset 3px 0 0 0 var(--warn)}
.meta{display:flex;gap:9px;align-items:center;font-size:12px;color:var(--dim);
 margin-bottom:5px;flex-wrap:wrap}
.badge{background:var(--sc);color:#fff;padding:1px 8px;border-radius:20px;
 font-weight:600;font-size:11px}
.new{background:var(--warn);color:#000;padding:1px 7px;border-radius:20px;
 font-size:10px;font-weight:700;letter-spacing:.04em}
.text{white-space:pre-wrap;word-break:break-word}
audio{height:30px;margin-top:8px;width:100%;max-width:420px;display:block}
.empty{color:var(--dim);text-align:center;padding:60px 20px}
#sessions{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
.sess{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--sc);
 border-radius:9px;padding:8px 11px;min-width:190px}
.sess.dead{opacity:.45}
.sess .top{display:flex;align-items:center;gap:7px;margin-bottom:3px}
.dot{width:7px;height:7px;border-radius:50%;background:#22c55e;flex:none}
.sess.dead .dot{background:var(--dim)}
.sess .nm{font-weight:600;font-size:13px}
.sess .cwd{font-size:11px;color:var(--dim);word-break:break-all;margin-bottom:6px}
.sess .vc{font-size:11px;color:var(--dim)}
.play{font-size:11px;padding:3px 9px;border-radius:6px}
.vsel{font-size:11px;padding:2px 5px;max-width:132px}
</style></head><body>
<header>
  <h1>say</h1>
  <button id="mute">…</button>
  <button id="drain">play inbox</button>
  <button id="stop">stop</button>
  <select id="filter"><option value="">all sessions</option></select>
  <span class="spacer"></span>
  <label style="font-size:12px;color:var(--dim)">
    <input type="checkbox" id="auto" checked> live</label>
</header>
<div id="banner"></div>
<main>
  <section id="sessions"></section>
  <div id="list" class="empty">nothing spoken yet</div>
</main>
<script>
const VOICES=__VOICES__;
const listEl=document.getElementById('list'),filtEl=document.getElementById('filter'),
      muteEl=document.getElementById('mute'),banner=document.getElementById('banner');
let sessions=new Set(),lastSig='';
// Stable hue per session, so a badge colour means the same project every time.
function hue(s){let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))>>>0;return h%360}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function when(ts){const d=new Date(ts*1000),n=new Date();
 const t=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
 return d.toDateString()===n.toDateString()?t
   :d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+t}
async function drawSessions(){
  const ss=await (await fetch('/api/sessions')).json();
  document.getElementById('sessions').innerHTML=ss.map(s=>`
    <div class="sess ${s.ended?'dead':''}" style="--sc:hsl(${hue(s.project)} 62% 48%)">
      <div class="top"><span class="dot"></span><span class="nm">${esc(s.label)}</span></div>
      <div class="cwd">${esc(s.cwd||'')}</div>
      <div class="top">
        <select class="vsel" onchange="pin('${esc(s.project)}',this.value)">
          ${VOICES.map(v=>`<option value="${v[0]}" ${v[0]===s.voice?'selected':''}
             >${v[0]} (${v[2]})</option>`).join('')}
        </select>
        <button class="play" onclick="preview(this.previousElementSibling.value,'${esc(s.project)}')">hear</button>
      </div>
    </div>`).join('');
}
// Choosing from the dropdown pins the project, so the choice survives restarts
// and is never reshuffled by automatic allocation.
async function pin(project,voice){
  await fetch('/api/pin',{method:'POST',body:JSON.stringify({project:project,voice:voice})});
  await preview(voice,project); lastSig='';
}
async function preview(voice,project){
  await fetch('/api/preview',{method:'POST',
    body:JSON.stringify({voice:voice,text:`This is the ${project} session speaking.`})});
}
async function refresh(){
  drawSessions();
  const st=await (await fetch('/api/status')).json();
  muteEl.textContent=st.muted?'muted':'unmuted';
  muteEl.className=st.muted?'on':'';
  banner.className=st.pending?'show':'';
  banner.textContent=st.pending?`${st.pending} message${st.pending>1?'s':''} waiting in the inbox`:'';
  document.title=st.pending?`(${st.pending}) say`:'say';
  const items=await (await fetch('/api/history')).json();
  const sig=items.map(i=>i.id+i.played).join(',');
  if(sig===lastSig)return; lastSig=sig;
  for(const i of items) if(!sessions.has(i.session)){sessions.add(i.session);
    const o=document.createElement('option');o.value=o.textContent=i.session;filtEl.appendChild(o)}
  const want=filtEl.value, shown=items.filter(i=>!want||i.session===want);
  if(!shown.length){listEl.className='empty';listEl.textContent='nothing spoken yet';return}
  listEl.className='';
  listEl.innerHTML=shown.map(i=>`
   <div class="row ${i.played?'':'unheard'}" style="--sc:hsl(${hue(i.session)} 62% 48%)">
     <div class="meta"><span class="badge">${esc(i.session)}</span>
       <span>${when(i.ts)}</span><span>${i.voice}</span><span>${i.dur}s</span>
       ${i.played?'':'<span class="new">UNHEARD</span>'}</div>
     <div class="text">${esc(i.text)}</div>
     <audio controls preload="none" src="/audio/${i.id}.wav"
            onplay="fetch('/api/heard/${i.id}',{method:'POST'})"></audio>
   </div>`).join('');
}
muteEl.onclick=async()=>{const m=muteEl.textContent==='muted';
  await fetch('/api/mute',{method:'POST',body:JSON.stringify({mute:!m})});lastSig='';refresh()};
document.getElementById('drain').onclick=()=>fetch('/api/drain',{method:'POST'});
document.getElementById('stop').onclick=()=>fetch('/api/stop',{method:'POST'});
filtEl.onchange=()=>{lastSig='';refresh()};
setInterval(()=>{if(document.getElementById('auto').checked)refresh()},2000);
refresh();
</script></body></html>"""


class UI(BaseHTTPRequestHandler):
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

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("__VOICES__", json.dumps(
                [[v, "", GRADES.get(v, "")] for v in VOICE_POOL]))
            return self._send(200, "text/html; charset=utf-8", page.encode())
        if self.path == "/api/history":
            return self._send(200, "application/json", json.dumps(history()).encode())
        if self.path == "/api/status":
            return self._send(200, "application/json", json.dumps(
                {"muted": is_muted(), "pending": pending_count()}).encode())
        if self.path == "/api/sessions":
            return self._send(200, "application/json",
                              json.dumps(list_sessions()).encode())
        if self.path.startswith("/audio/"):
            uid = os.path.basename(self.path[len("/audio/"):]).removesuffix(".wav")
            blob = audio_of(uid)
            if blob:
                return self._send(200, "audio/wav", blob)
        self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/api/stop":
            stop_current()
            return self._send(200, "application/json", b'{"ok":true}')
        if self.path == "/api/drain":
            submit({"kind": "drain"})
            return self._send(200, "application/json", b'{"ok":true}')
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
        if self.path.startswith("/api/heard/"):
            mark_played([os.path.basename(self.path[len("/api/heard/"):])])
            return self._send(200, "application/json", b'{"ok":true}')
        self._send(404, "text/plain", b"not found")


def serve_ui():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", UI_PORT), UI)
    except OSError as exc:
        log(f"UI disabled ({exc})")
        return
    log(f"UI on http://127.0.0.1:{UI_PORT}")
    srv.serve_forever()


# ---------------------------------------------------------------- main

def main():
    os.makedirs(STATE, exist_ok=True)
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
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=serve_ui, daemon=True).start()
    log(f"ready on {SOCKET_PATH} (muted={is_muted()}, pending={pending_count()})")

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    sys.exit(main())
