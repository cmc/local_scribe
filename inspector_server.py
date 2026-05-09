"""local_scribe Inspector — a tiny loopback web app for the data Char
already collects, plus our config + Char audit.

Tabs (single page, vanilla JS):

* Sessions  — list every Char session on disk, play audio, view the
  diarised transcript, view per-template summaries.
* Config    — ground-truth ``~/.config/local_scribe/config.json``
  with form-bound editing (PUT round-trips through validation +
  timestamped backup).
* Char      — runs ``char_audit.audit()`` and surfaces the OK/WARN/INFO
  list with a one-click ``configure-char`` button.

By default binds to ``127.0.0.1:8001`` and skips auth — same loopback
trust model as the rest of the stack. Set ``inspector.auth_token`` in
config.json to enable bearer-token auth (only required if you ever
rebind to a non-loopback address; the validator refuses that
combination otherwise).

Sized to avoid build steps: no React, no bundler. The HTML / CSS / JS
are inlined below as constants and served as a single page. That keeps
the privacy surface tiny — no external CDNs, no XHR to anything but
``self``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

import char_audit
from config import (
    DEFAULT_CONFIG_PATH,
    Config,
    load_config,
    save_config,
    to_dict,
    validate,
)


logger = logging.getLogger("local_scribe.inspector")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _require_auth(request: Request, cfg: Config) -> None:
    """Loopback + optional bearer-token auth.

    By default ``inspector.auth_token`` is null and we don't enforce
    auth -- inspector.bind=127.0.0.1 already keeps non-local clients
    out. If a token is set, every /api/* request must carry it.
    """
    if not cfg.inspector_auth_token:
        return
    header = request.headers.get("authorization", "")
    expected = f"Bearer {cfg.inspector_auth_token}"
    # secrets.compare_digest avoids leaking the token length via timing
    if not (header and secrets.compare_digest(header, expected)):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _list_sessions(sessions_dir: Path) -> list[dict[str, Any]]:
    """Scan Char's per-session directories and return a JSON-friendly
    summary suitable for the Sessions tab card grid."""
    out: list[dict[str, Any]] = []
    if not sessions_dir.is_dir():
        return out
    for entry in sorted(sessions_dir.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "_meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text() or "{}")
            except json.JSONDecodeError:
                meta = {}
        audio_path = entry / "audio.mp3"
        transcript_path = entry / "transcript.json"
        notes = sorted(
            p.name for p in entry.glob("*.md") if not p.name.startswith("_")
        )
        out.append({
            "id": meta.get("id") or entry.name,
            "title": meta.get("title") or "(untitled)",
            "created_at": meta.get("created_at"),
            "participants": meta.get("participants") or [],
            "audio_bytes": audio_path.stat().st_size if audio_path.is_file() else 0,
            "has_audio": audio_path.is_file(),
            "has_transcript": transcript_path.is_file(),
            "notes": notes,
        })
    # Newest first -- Char's UI does the same.
    out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return out


def _session_dir(cfg: Config, session_id: str) -> Path:
    """Resolve + validate a session directory. Refuses traversal."""
    if not session_id or "/" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="invalid session id")
    path = cfg.char_sessions_dir / session_id
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="session not found")
    return path


def _flatten_transcript(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse Char's ``transcript.json`` into a UI-friendly shape:
    every word with its speaker, plus a denormalised plain-text view
    grouped by speaker. Char's stored format keeps words in nested
    ``transcripts[].words`` arrays with separate speaker_hints, so we
    do the join here once on the server.
    """
    transcripts = raw.get("transcripts") or []
    speakers_by_word: dict[str, str] = {}
    for t in transcripts:
        for hint in (t.get("speaker_hints") or []):
            for wid in (hint.get("word_id") or []):
                speakers_by_word[wid] = hint.get("value") or ""

    words: list[dict[str, Any]] = []
    for t in transcripts:
        for w in (t.get("words") or []):
            wid = w.get("id") or ""
            words.append({
                "id": wid,
                "text": w.get("text") or "",
                "start": w.get("start"),
                "end": w.get("end"),
                "speaker": speakers_by_word.get(wid, ""),
            })

    # Group consecutive words by speaker into paragraphs.
    paragraphs: list[dict[str, Any]] = []
    cur_speaker: str | None = None
    cur_text: list[str] = []
    cur_start: float | None = None
    for w in words:
        sp = w["speaker"] or "?"
        if sp != cur_speaker:
            if cur_text:
                paragraphs.append({
                    "speaker": cur_speaker or "",
                    "text": " ".join(cur_text),
                    "start": cur_start,
                })
            cur_speaker = sp
            cur_text = [w["text"]]
            cur_start = w["start"]
        else:
            cur_text.append(w["text"])
    if cur_text:
        paragraphs.append({
            "speaker": cur_speaker or "",
            "text": " ".join(cur_text),
            "start": cur_start,
        })
    return {"word_count": len(words), "paragraphs": paragraphs}


def create_app(cfg: Config | None = None) -> FastAPI:
    """Factory used by uvicorn entry-point + tests. Re-loading is the
    caller's job (each request reads the cached ``cfg`` for fast paths,
    plus a fresh ``load_config()`` for /api/config GET so an out-of-band
    edit shows up immediately)."""
    cfg = cfg or load_config()
    app = FastAPI(title="local_scribe inspector", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        # The HTML page itself is unauthenticated (so the browser can
        # load it before sending the token in headers); /api/* is gated.
        if request.url.path.startswith("/api/"):
            try:
                _require_auth(request, cfg)
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code,
                )
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "local_scribe.inspector"}

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        sessions = _list_sessions(cfg.char_sessions_dir)
        return {
            "sessions_dir": str(cfg.char_sessions_dir),
            "sessions": sessions,
            "count": len(sessions),
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        path = _session_dir(cfg, session_id)
        meta_path = path / "_meta.json"
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text() or "{}")
            except json.JSONDecodeError:
                meta = {}
        transcript: dict[str, Any] | None = None
        tpath = path / "transcript.json"
        if tpath.is_file():
            try:
                transcript = _flatten_transcript(
                    json.loads(tpath.read_text() or "{}")
                )
            except json.JSONDecodeError:
                transcript = None
        notes: list[dict[str, Any]] = []
        for p in sorted(path.glob("*.md")):
            if p.name.startswith("_"):
                continue
            notes.append({
                "filename": p.name,
                "size": p.stat().st_size,
                "content": p.read_text(),
            })
        audio_path = path / "audio.mp3"
        return {
            "id": session_id,
            "meta": meta,
            "audio_bytes": audio_path.stat().st_size if audio_path.is_file() else 0,
            "has_audio": audio_path.is_file(),
            "transcript": transcript,
            "notes": notes,
        }

    @app.get("/api/sessions/{session_id}/audio")
    async def session_audio(session_id: str) -> FileResponse:
        path = _session_dir(cfg, session_id) / "audio.mp3"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="audio not found")
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=f"{session_id}.mp3",
        )

    @app.get("/api/sessions/{session_id}/transcript.txt", response_class=PlainTextResponse)
    async def session_transcript_txt(session_id: str) -> PlainTextResponse:
        path = _session_dir(cfg, session_id) / "transcript.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="transcript not found")
        try:
            payload = _flatten_transcript(json.loads(path.read_text() or "{}"))
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="transcript JSON malformed")
        lines = []
        for p in payload.get("paragraphs", []):
            sp = p.get("speaker") or "Speaker"
            lines.append(f"{sp}: {p.get('text', '')}")
        return PlainTextResponse("\n\n".join(lines))

    # ---------- Config ------------------------------------------------

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        # Re-load so out-of-band edits (e.g. a manual `vim config.json`)
        # show up immediately.
        fresh = load_config()
        return {
            "config_path": str(DEFAULT_CONFIG_PATH),
            "config": to_dict(fresh),
        }

    @app.put("/api/config")
    async def put_config(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        errors = validate(body)
        if errors:
            return JSONResponse(
                {"ok": False, "errors": errors}, status_code=400,
            )
        path = save_config(body)
        return {
            "ok": True,
            "config_path": str(path),
            "note": (
                "wrote config; restart the ASR server "
                "(`./run.sh stop && ./run.sh start`) to pick up changes "
                "to asr.* / llm.*"
            ),
        }

    # ---------- Char audit -------------------------------------------

    @app.get("/api/char/audit")
    async def char_audit_endpoint() -> dict[str, Any]:
        return char_audit.audit(load_config()).to_dict()

    @app.post("/api/char/configure")
    async def char_configure(request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        backup_key = bool(body.get("backup_existing_key", True))
        result = char_audit.configure_char(
            load_config(), backup_existing_key=backup_key,
        )
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    # ---------- Service health checks --------------------------------

    @app.get("/api/asr/health")
    async def asr_health() -> dict[str, Any]:
        url = f"http://{cfg.asr_bind}:{cfg.asr_port}/health"
        try:
            r = requests.get(url, timeout=2.0)
            return {"ok": r.ok, "status": r.status_code, "url": url, "body": r.text[:200]}
        except requests.RequestException as exc:
            return {"ok": False, "url": url, "error": str(exc)}

    @app.get("/api/llm/health")
    async def llm_health() -> dict[str, Any]:
        # Hit LM Studio's models endpoint; any 200 means the server's
        # alive. Body lists which model is currently loaded.
        url = cfg.llm_models_url
        try:
            r = requests.get(url, timeout=2.0)
            payload: dict[str, Any] = {"ok": r.ok, "status": r.status_code, "url": url}
            try:
                data = r.json()
                if isinstance(data, dict) and "data" in data:
                    payload["models"] = [m.get("id") for m in data.get("data") or []]
                    payload["expected_model"] = cfg.llm_model
                    payload["expected_model_loaded"] = any(
                        cfg.llm_model in (m.get("id") or "") for m in data.get("data") or []
                        if (m.get("state") or "").lower() in ("loaded", "loading", "ready")
                    )
            except (ValueError, json.JSONDecodeError):
                pass
            return payload
        except requests.RequestException as exc:
            return {"ok": False, "url": url, "error": str(exc)}

    return app


# Module-level app for ``uvicorn inspector_server:app``. Tests use the
# factory directly so they can inject a temp-dir Config.
app = create_app()


# ===================================================================
# Frontend: single-page HTML/CSS/JS, no build step.
# ===================================================================

_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>local_scribe inspector</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {
  --bg: #0e1116;
  --fg: #e6edf3;
  --fg-dim: #8b949e;
  --accent: #58a6ff;
  --ok: #3fb950;
  --warn: #d29922;
  --err: #f85149;
  --info: #8b949e;
  --card: #161b22;
  --border: #30363d;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #ffffff; --fg: #1f2328; --fg-dim: #59636e;
    --accent: #0969da; --ok: #1a7f37; --warn: #9a6700;
    --err: #d1242f; --info: #59636e;
    --card: #f6f8fa; --border: #d0d7de;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI",
       Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg);
}
header {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg); border-bottom: 1px solid var(--border);
  padding: 0.6rem 1rem; display: flex; align-items: center; gap: 1rem;
  flex-wrap: wrap;
}
header h1 { margin: 0; font-size: 1rem; font-weight: 600; }
header h1 small { color: var(--fg-dim); font-weight: 400; margin-left: 0.5rem; }
nav { display: flex; gap: 0.25rem; }
nav button {
  background: transparent; color: var(--fg); border: 1px solid transparent;
  padding: 0.3rem 0.7rem; border-radius: 6px; cursor: pointer; font: inherit;
}
nav button.active { background: var(--card); border-color: var(--border); }
nav button:hover { background: var(--card); }
.pills { display: flex; gap: 0.4rem; margin-left: auto; }
.pill {
  font-size: 0.8rem; padding: 0.15rem 0.55rem; border-radius: 999px;
  background: var(--card); border: 1px solid var(--border); color: var(--fg-dim);
  white-space: nowrap;
}
.pill.ok { color: var(--ok); border-color: var(--ok); }
.pill.warn { color: var(--warn); border-color: var(--warn); }
.pill.err { color: var(--err); border-color: var(--err); }
main { padding: 1rem; max-width: 1100px; margin: 0 auto; }
section { display: none; }
section.active { display: block; }
h2 { font-size: 1rem; margin-top: 0; }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 1rem; margin-bottom: 0.75rem;
}
.card h3 { margin: 0 0 0.4rem; font-size: 0.95rem; }
.card .meta { color: var(--fg-dim); font-size: 0.8rem; }
.row { display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; }
button.btn, a.btn {
  background: var(--accent); color: white; border: 0;
  padding: 0.35rem 0.8rem; border-radius: 6px; cursor: pointer;
  font: inherit; text-decoration: none;
}
button.btn.ghost {
  background: transparent; color: var(--fg); border: 1px solid var(--border);
}
button.btn.danger { background: var(--err); }
input, select, textarea {
  background: var(--card); color: var(--fg); border: 1px solid var(--border);
  border-radius: 6px; padding: 0.3rem 0.5rem; font: inherit; width: 100%;
}
textarea { min-height: 6rem; font-family: ui-monospace, Menlo, monospace; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--fg-dim); font-weight: 500; font-size: 0.85rem; }
.badge {
  display: inline-block; padding: 0.05rem 0.45rem; border-radius: 999px;
  font-size: 0.75rem; border: 1px solid var(--border);
}
.badge.ok { color: var(--ok); border-color: var(--ok); }
.badge.warn { color: var(--warn); border-color: var(--warn); }
.badge.info { color: var(--info); border-color: var(--info); }
.badge.miss { color: var(--err); border-color: var(--err); }
.transcript .speaker { color: var(--accent); font-weight: 600; }
.transcript p { margin: 0.4rem 0; }
.kv { display: grid; grid-template-columns: 12rem 1fr; gap: 0.3rem 1rem; }
.kv label { color: var(--fg-dim); align-self: center; }
.session-detail { padding-top: 1rem; border-top: 1px dashed var(--border); margin-top: 1rem; }
.note-box {
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
  padding: 0.6rem; max-height: 22rem; overflow: auto; white-space: pre-wrap;
  font-family: ui-monospace, Menlo, monospace; font-size: 0.8rem;
}
.empty { color: var(--fg-dim); padding: 1rem; text-align: center; }
.error-block { color: var(--err); padding: 0.5rem; }
audio { width: 100%; }
code { background: var(--card); padding: 0.05rem 0.3rem; border-radius: 4px; }
</style>
</head>
<body>
<header>
  <h1>local_scribe <small>inspector</small></h1>
  <nav>
    <button data-tab="sessions" class="active">Sessions</button>
    <button data-tab="config">Config</button>
    <button data-tab="char">Char audit</button>
    <button data-tab="about">About</button>
  </nav>
  <div class="pills" id="status-pills"></div>
</header>
<main>

<section id="tab-sessions" class="active">
  <h2>Sessions</h2>
  <div id="sessions-list" class="empty">loading…</div>
  <div id="session-detail" class="session-detail" style="display:none"></div>
</section>

<section id="tab-config">
  <h2>Config</h2>
  <p class="meta">
    Edits write to
    <code id="config-path">~/.config/local_scribe/config.json</code>.
    Each save backs up the previous file as <code>config.json.bak.&lt;ts&gt;</code>.
    ASR / LLM changes apply on the next ASR-server restart.
  </p>
  <form id="config-form"></form>
  <div class="row" style="margin-top:1rem">
    <button type="button" class="btn" id="config-save">Save</button>
    <button type="button" class="btn ghost" id="config-reset">Reload from disk</button>
    <span id="config-msg" class="meta"></span>
  </div>
</section>

<section id="tab-char">
  <h2>Char audit</h2>
  <p class="meta">
    Verifies Char isn't pointed at any non-local provider. Read the
    methodology in <code>CHAR_REVIEW.md</code>.
  </p>
  <div class="row" style="margin-bottom:0.6rem">
    <button class="btn" id="char-rerun">Re-run audit</button>
    <button class="btn ghost" id="char-fix">Run configure-char</button>
  </div>
  <div id="char-summary" class="meta"></div>
  <table id="char-checks">
    <thead><tr><th>key</th><th>status</th><th>current</th><th>expected</th><th>note</th></tr></thead>
    <tbody></tbody>
  </table>
  <h3>Backups</h3>
  <ul id="char-backups" class="meta"></ul>
</section>

<section id="tab-about">
  <h2>About</h2>
  <p>This is the <code>local_scribe</code> inspector — a small loopback
  web UI over the data Char already collects.</p>
  <ul>
    <li>Privacy posture and exact endpoints: see <code>CHAR_REVIEW.md</code> in the repo.</li>
    <li>Config schema: see <code>config.py</code>.</li>
    <li>Source: <a href="https://github.com/cmc/local_scribe" target="_blank" rel="noopener">github.com/cmc/local_scribe</a></li>
  </ul>
</section>

</main>

<script>
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, opts) {
  const res = await fetch(path, opts);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    const err = new Error((data && data.detail) || res.statusText);
    err.status = res.status; err.data = data; throw err;
  }
  return data;
}

function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B','KB','MB','GB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

function fmtTs(t) {
  if (!t) return '';
  try { return new Date(t).toLocaleString(); } catch (e) { return t; }
}

function fmtDuration(secs) {
  if (secs == null) return '';
  const m = Math.floor(secs / 60), s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2,'0')}`;
}

// --- tabs --------------------------------------------------------
$$('nav button').forEach(b => b.addEventListener('click', () => {
  $$('nav button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $$('section').forEach(s => s.classList.remove('active'));
  $('#tab-' + b.dataset.tab).classList.add('active');
  if (b.dataset.tab === 'config') loadConfig();
  if (b.dataset.tab === 'char') loadCharAudit();
}));

// --- top-bar status pills ---------------------------------------
async function refreshStatus() {
  const pills = $('#status-pills'); pills.innerHTML = '';
  const add = (label, status, title) => {
    const el = document.createElement('span');
    el.className = 'pill ' + status;
    el.textContent = label;
    if (title) el.title = title;
    pills.appendChild(el);
  };
  try {
    const a = await api('/api/asr/health');
    add('ASR ' + (a.ok ? 'up' : 'down'), a.ok ? 'ok' : 'err', a.url);
  } catch { add('ASR ?', 'err'); }
  try {
    const l = await api('/api/llm/health');
    add('LM Studio ' + (l.ok ? 'up' : 'down'), l.ok ? 'ok' : 'err', l.url);
  } catch { add('LM Studio ?', 'err'); }
  try {
    const c = await api('/api/char/audit');
    const bad = (c.summary.warn || 0) + (c.summary.miss || 0);
    add('Char ' + (bad ? bad + ' warn' : 'clean'), bad ? 'warn' : 'ok');
  } catch { add('Char ?', 'err'); }
}

// --- Sessions tab ------------------------------------------------
async function loadSessions() {
  const list = $('#sessions-list');
  list.innerHTML = 'loading…';
  try {
    const data = await api('/api/sessions');
    if (!data.sessions.length) {
      list.innerHTML = '<div class="empty">No sessions found in <code>'
        + data.sessions_dir + '</code>.</div>';
      return;
    }
    list.innerHTML = '';
    list.classList.remove('empty');
    data.sessions.forEach(s => {
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `
        <h3>${escapeHtml(s.title)}</h3>
        <div class="meta">${fmtTs(s.created_at)}
          · ${s.has_audio ? fmtBytes(s.audio_bytes) + ' audio' : 'no audio'}
          · ${s.has_transcript ? 'transcript' : 'no transcript'}
          · ${s.notes.length} note${s.notes.length === 1 ? '' : 's'}</div>
        <div class="row" style="margin-top:0.5rem">
          <button class="btn" data-id="${s.id}">Open</button>
          <a class="btn ghost" href="/api/sessions/${s.id}/audio" download>Download audio</a>
          <a class="btn ghost" href="/api/sessions/${s.id}/transcript.txt"
             target="_blank" rel="noopener">Transcript .txt</a>
          <span class="meta" style="margin-left:auto">${escapeHtml(s.id)}</span>
        </div>`;
      card.querySelector('button.btn').addEventListener('click',
        () => openSession(s.id));
      list.appendChild(card);
    });
  } catch (e) {
    list.innerHTML = '<div class="error-block">load failed: ' + escapeHtml(e.message) + '</div>';
  }
}

async function openSession(id) {
  const detail = $('#session-detail');
  detail.style.display = 'block';
  detail.innerHTML = 'loading…';
  try {
    const s = await api('/api/sessions/' + encodeURIComponent(id));
    let html = `<h3>${escapeHtml(s.meta.title || '(untitled)')}</h3>
      <div class="meta">${escapeHtml(s.id)} · ${fmtTs(s.meta.created_at)}</div>`;
    if (s.has_audio) {
      html += `<audio controls preload="metadata"
        src="/api/sessions/${id}/audio"></audio>`;
    }
    if (s.transcript && s.transcript.paragraphs && s.transcript.paragraphs.length) {
      html += '<h4>Transcript</h4><div class="transcript">';
      s.transcript.paragraphs.forEach(p => {
        html += `<p><span class="speaker">${escapeHtml(p.speaker || 'Speaker')}:</span>
                 ${escapeHtml(p.text)}
                 <span class="meta"> ${fmtDuration(p.start)}</span></p>`;
      });
      html += '</div>';
    } else {
      html += '<p class="meta">no transcript yet</p>';
    }
    if (s.notes.length) {
      html += '<h4>Notes</h4>';
      s.notes.forEach(n => {
        html += `<details><summary>${escapeHtml(n.filename)}
                 (${fmtBytes(n.size)})</summary>
                 <div class="note-box">${escapeHtml(n.content)}</div></details>`;
      });
    }
    html += `<div class="row" style="margin-top:0.6rem">
             <button class="btn ghost" id="close-session">Close</button></div>`;
    detail.innerHTML = html;
    $('#close-session').addEventListener('click', () => detail.style.display = 'none');
    detail.scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e) {
    detail.innerHTML = '<div class="error-block">'
      + escapeHtml(e.message) + '</div>';
  }
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// --- Config tab --------------------------------------------------
let _configCache = null;

const CONFIG_FIELDS = [
  // [section, key, label, type, hint?]
  ['asr',       'bind',                       'asr.bind',        'text', '127.0.0.1 = loopback only'],
  ['asr',       'port',                       'asr.port',        'number'],
  ['asr',       'backend',                    'asr.backend',     'select', ['parakeet','whisper']],
  ['asr',       'parakeet_model',             'asr.parakeet_model', 'text'],
  ['asr',       'whisper_model',              'asr.whisper_model','text'],
  ['asr',       'stream_heartbeat_seconds',   'asr.stream_heartbeat_seconds','number'],
  ['asr.diarization', 'enabled',              'asr.diarization.enabled', 'checkbox'],
  ['asr.diarization', 'max_seconds',          'asr.diarization.max_seconds','number'],
  ['asr.diarization', 'max_speakers',         'asr.diarization.max_speakers','number'],
  ['asr.diarization', 'num_speakers',         'asr.diarization.num_speakers','number_or_null'],
  ['asr.diarization', 'cluster_threshold',    'asr.diarization.cluster_threshold','number_or_null'],
  ['llm',       'host',                       'llm.host',        'text', 'set to a LAN address to run LM Studio on another Mac'],
  ['llm',       'port',                       'llm.port',        'number'],
  ['llm',       'model',                      'llm.model',       'text'],
  ['llm',       'max_tokens',                 'llm.max_tokens',  'number'],
  ['llm',       'temperature',                'llm.temperature', 'number'],
  ['inspector', 'bind',                       'inspector.bind',  'text'],
  ['inspector', 'port',                       'inspector.port',  'number'],
  ['inspector', 'auth_token',                 'inspector.auth_token','password_or_null'],
  ['char',      'data_dir',                   'char.data_dir',   'text_or_null'],
  ['char',      'expected_stt_provider',      'char.expected_stt_provider','text'],
  ['char',      'expected_stt_model',         'char.expected_stt_model','text'],
];

function getDotted(obj, dotted) {
  return dotted.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setDotted(obj, dotted, val) {
  const parts = dotted.split('.'); let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    cur[parts[i]] = cur[parts[i]] || {}; cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = val;
}

async function loadConfig() {
  const form = $('#config-form');
  form.innerHTML = 'loading…';
  try {
    const data = await api('/api/config');
    _configCache = data.config;
    $('#config-path').textContent = data.config_path;
    form.innerHTML = '';
    const container = document.createElement('div'); container.className = 'kv';
    CONFIG_FIELDS.forEach(([section, key, label, type, extra]) => {
      const dotted = (section === 'asr.diarization' ? 'asr.diarization.' + key
                      : section + '.' + key);
      const cur = getDotted(_configCache, dotted);
      const lbl = document.createElement('label'); lbl.textContent = label;
      lbl.htmlFor = 'cfg-' + dotted;
      const wrap = document.createElement('div');
      let input;
      if (type === 'select') {
        input = document.createElement('select');
        (extra || []).forEach(o => {
          const opt = document.createElement('option');
          opt.value = o; opt.textContent = o;
          if (o === cur) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (type === 'checkbox') {
        input = document.createElement('input');
        input.type = 'checkbox'; input.checked = !!cur;
      } else {
        input = document.createElement('input');
        input.type = (type === 'password_or_null') ? 'password'
                   : (type === 'number' || type === 'number_or_null') ? 'number' : 'text';
        if (cur != null) input.value = cur;
      }
      input.id = 'cfg-' + dotted;
      input.dataset.dotted = dotted;
      input.dataset.kind = type;
      wrap.appendChild(input);
      // For non-select types the 5th tuple slot is an inline hint.
      if (type !== 'select' && typeof extra === 'string') {
        const sm = document.createElement('div');
        sm.className = 'meta'; sm.textContent = extra;
        wrap.appendChild(sm);
      }
      container.appendChild(lbl); container.appendChild(wrap);
    });
    form.appendChild(container);
  } catch (e) {
    form.innerHTML = '<div class="error-block">'
      + escapeHtml(e.message) + '</div>';
  }
}

async function saveConfig() {
  const out = JSON.parse(JSON.stringify(_configCache || {}));
  $$('#config-form input,#config-form select').forEach(el => {
    const dotted = el.dataset.dotted, kind = el.dataset.kind;
    let val;
    if (kind === 'checkbox') val = el.checked;
    else if (kind === 'number') val = el.value === '' ? 0 : Number(el.value);
    else if (kind === 'number_or_null') val = el.value === '' ? null : Number(el.value);
    else if (kind === 'text_or_null' || kind === 'password_or_null')
      val = el.value === '' ? null : el.value;
    else val = el.value;
    setDotted(out, dotted, val);
  });
  const msg = $('#config-msg'); msg.textContent = 'saving…';
  try {
    const res = await api('/api/config', {
      method: 'PUT',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(out),
    });
    msg.textContent = 'saved · ' + (res.note || res.config_path);
  } catch (e) {
    if (e.data && e.data.errors) {
      msg.textContent = 'rejected: ' + e.data.errors.join('; ');
    } else {
      msg.textContent = 'failed: ' + e.message;
    }
  }
}

$('#config-save')?.addEventListener('click', saveConfig);
$('#config-reset')?.addEventListener('click', loadConfig);

// --- Char audit tab ----------------------------------------------
async function loadCharAudit() {
  const tbody = $('#char-checks tbody'); tbody.innerHTML = '<tr><td colspan="5">loading…</td></tr>';
  try {
    const r = await api('/api/char/audit');
    tbody.innerHTML = '';
    r.checks.forEach(c => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><code>${escapeHtml(c.key)}</code></td>
        <td><span class="badge ${c.status}">${c.status}</span></td>
        <td><code>${escapeHtml(JSON.stringify(c.current))}</code></td>
        <td>${c.expected == null ? '' : '<code>' + escapeHtml(JSON.stringify(c.expected)) + '</code>'}</td>
        <td class="meta">${escapeHtml(c.note || '')}</td>`;
      tbody.appendChild(tr);
    });
    $('#char-summary').textContent =
      `${r.summary.ok} ok · ${r.summary.warn} warn · ${r.summary.info} info · ${r.summary.miss} miss`
      + ` · settings: ${r.settings_path}`;
    const ul = $('#char-backups'); ul.innerHTML = '';
    (r.backups || []).slice(-10).reverse().forEach(b => {
      const li = document.createElement('li'); li.textContent = b; ul.appendChild(li);
    });
    if (!r.backups || !r.backups.length) {
      ul.innerHTML = '<li>no backups yet</li>';
    }
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" class="error-block">'
      + escapeHtml(e.message) + '</td></tr>';
  }
}

$('#char-rerun')?.addEventListener('click', loadCharAudit);
$('#char-fix')?.addEventListener('click', async () => {
  if (!confirm('Quit Char if it\'s running, rewrite settings.json + store.json?')) return;
  try {
    await api('/api/char/configure', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({backup_existing_key: true}),
    });
    await loadCharAudit();
  } catch (e) {
    alert('configure-char failed: ' + e.message);
  }
});

// --- bootstrap ---------------------------------------------------
loadSessions();
refreshStatus();
setInterval(refreshStatus, 15000);
</script>
</body>
</html>
"""
