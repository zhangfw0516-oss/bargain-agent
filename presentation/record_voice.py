"""Record the project lead's lines in your own voice, then build the narrated video.

    venv\\Scripts\\python.exe presentation\\record_voice.py

Opens a local page (http://127.0.0.1:8770/) that shows one line at a time. Record each
line with the microphone; recordings are saved to presentation/lead-video/voice/NN.webm
and never leave this computer. "Build video" runs lead_video.py --own-voice, which times
the slides and captions to your recordings.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lead_video  # noqa: E402

PORT = 8770
ORIGIN = f"http://127.0.0.1:{PORT}"
VOICE_DIR = lead_video.OUT / "voice"
MAX_BYTES = 20 * 1024 * 1024
BUILD = {"state": "idle", "log": ""}

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Recorder</title><style>
:root { --ink:#18212b; --muted:#5b6673; --line:#dfe3e8; --accent:#1d5f8c; --soft:#e4eef6; --rec:#c0362c; --ok:#2f7d4f; --paper:#f6f7f9; --card:#fff; }
@media (prefers-color-scheme: dark) { :root { --ink:#e6ebf0; --muted:#9aa6b2; --line:#2a333d; --accent:#7db7e3; --soft:#1b2d3c; --rec:#f08c76; --ok:#6fcf97; --paper:#11161c; --card:#181f27; color-scheme: dark; } }
* { box-sizing:border-box; } body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
.wrap { max-width:1100px; margin:0 auto; padding:24px 20px 48px; display:grid; grid-template-columns:260px minmax(0,1fr); gap:20px; }
@media (max-width:760px) { .wrap { grid-template-columns:1fr; } }
h1 { margin:0 0 4px; font-size:24px; } .muted { color:var(--muted); }
.list { display:grid; gap:6px; align-content:start; }
.list button { text-align:left; border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:8px; padding:7px 10px; cursor:pointer; font:inherit; display:flex; gap:8px; }
.list button.cur { border-color:var(--accent); background:var(--soft); }
.list .dot { width:18px; flex:none; text-align:center; } .list .done .dot { color:var(--ok); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:22px; display:grid; gap:16px; }
.line { font-size:24px; line-height:1.55; min-height:180px; }
.row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.row button { font:600 15px inherit; font-family:inherit; border-radius:10px; padding:10px 18px; border:1px solid var(--line); background:var(--card); color:var(--ink); cursor:pointer; }
.row button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
.row button.rec { background:var(--rec); color:#fff; border-color:var(--rec); }
.row button:disabled { opacity:.45; cursor:default; }
.meter { height:8px; flex:1; min-width:120px; background:var(--soft); border-radius:4px; overflow:hidden; } .meter div { height:100%; width:0; background:var(--ok); }
.timer { font-variant-numeric:tabular-nums; min-width:60px; }
pre { white-space:pre-wrap; background:var(--soft); border-radius:8px; padding:10px; font-size:13px; max-height:160px; overflow:auto; margin:0; }
</style></head><body><div class="wrap">
<div><h1>录你的台词</h1><p class="muted">共 <span id="count"></span> 句。录完的会打勾，可以随时点回去重录。</p><div class="list" id="list"></div></div>
<div class="card">
  <div class="muted" id="pos"></div>
  <div class="line" id="line"></div>
  <div class="row">
    <button class="rec" id="recBtn">● 录音</button><button id="stopBtn" disabled>■ 停止</button><button id="playBtn" disabled>▶ 回放</button>
    <button class="primary" id="nextBtn">下一句 →</button><span class="timer" id="timer">0.0 s</span><div class="meter"><div id="level"></div></div>
  </div>
  <p class="muted" id="hint">提示：戴耳机麦克风效果最好；在安静的地方录；每句录完点"停止"会自动保存。快捷键：空格开始/停止。</p>
  <div class="row"><button class="primary" id="buildBtn" disabled>生成视频</button><span class="muted" id="buildState"></span></div>
  <pre id="buildLog" hidden></pre>
</div></div>
<script>
const LINES = __LINES__;
let cur = 0, recorder = null, chunks = [], started = 0, tick = null, done = new Set(__DONE__), stream = null, analyser = null;
const $ = id => document.getElementById(id);
function render() {
  $('count').textContent = LINES.length;
  const list = $('list'); list.textContent = '';
  LINES.forEach((t, i) => {
    const b = document.createElement('button');
    b.className = (i === cur ? 'cur ' : '') + (done.has(i) ? 'done' : '');
    b.innerHTML = '<span class="dot">' + (done.has(i) ? '✓' : (i + 1)) + '</span><span></span>';
    b.lastChild.textContent = t.slice(0, 40) + (t.length > 40 ? '…' : '');
    b.onclick = () => { if (!recorder) { cur = i; render(); } };
    list.appendChild(b);
  });
  $('pos').textContent = '第 ' + (cur + 1) + ' / ' + LINES.length + ' 句';
  $('line').textContent = LINES[cur];
  $('playBtn').disabled = !done.has(cur) || !!recorder;
  $('nextBtn').disabled = cur >= LINES.length - 1 || !!recorder;
  $('buildBtn').disabled = done.size < LINES.length || !!recorder;
  $('buildState').textContent = done.size < LINES.length ? '还差 ' + (LINES.length - done.size) + ' 句' : '全部录好了，可以生成视频';
}
async function startRec() {
  try {
    stream = stream || await navigator.mediaDevices.getUserMedia({audio: {noiseSuppression: true, echoCancellation: true}});
  } catch (e) { $('hint').textContent = '没有拿到麦克风权限：请在地址栏左侧允许使用麦克风，然后再点录音。'; return; }
  if (!analyser) {
    const ctx = new AudioContext(); analyser = ctx.createAnalyser(); analyser.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(analyser);
  }
  chunks = []; recorder = new MediaRecorder(stream, {mimeType: 'audio/webm;codecs=opus'});
  recorder.ondataavailable = e => chunks.push(e.data);
  recorder.onstop = save;
  recorder.start(); started = performance.now();
  tick = setInterval(() => {
    $('timer').textContent = ((performance.now() - started) / 1000).toFixed(1) + ' s';
    const data = new Uint8Array(analyser.fftSize); analyser.getByteTimeDomainData(data);
    let peak = 0; for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
    $('level').style.width = Math.min(100, peak / 1.28) + '%';
  }, 100);
  $('recBtn').disabled = true; $('stopBtn').disabled = false; render();
}
function stopRec() { if (recorder) recorder.stop(); clearInterval(tick); $('level').style.width = '0'; }
async function save() {
  const blob = new Blob(chunks, {type: 'audio/webm'});
  recorder = null; $('recBtn').disabled = false; $('stopBtn').disabled = true;
  const r = await fetch('/save/' + cur, {method: 'POST', headers: {'Content-Type': 'audio/webm'}, body: blob});
  if (r.ok) { done.add(cur); $('hint').textContent = '第 ' + (cur + 1) + ' 句已保存。可以回放听一下，满意就点"下一句"。'; }
  else { $('hint').textContent = '保存失败：' + await r.text(); }
  render();
}
$('recBtn').onclick = startRec; $('stopBtn').onclick = stopRec;
$('playBtn').onclick = () => new Audio('/clip/' + cur + '?t=' + Date.now()).play();
$('nextBtn').onclick = () => { cur = Math.min(LINES.length - 1, cur + 1); render(); };
document.addEventListener('keydown', e => { if (e.code === 'Space' && e.target === document.body) { e.preventDefault(); recorder ? stopRec() : startRec(); } });
$('buildBtn').onclick = async () => {
  await fetch('/build', {method: 'POST'}); $('buildLog').hidden = false;
  const poll = async () => {
    const s = await (await fetch('/build-status')).json();
    $('buildState').textContent = {running: '正在生成视频（大约 5 分钟），请不要关闭黑色窗口…', done: '视频已生成：' + s.out, error: '生成失败，见下面的输出'}[s.state] || '';
    $('buildLog').textContent = s.log;
    if (s.state === 'running') setTimeout(poll, 3000);
  };
  poll();
};
render();
</script></body></html>"""


def recorded() -> list[int]:
    return sorted(int(p.stem) for p in VOICE_DIR.glob("*.webm") if p.stem.isdigit())


def build() -> None:
    BUILD.update(state="running", log="")
    proc = subprocess.run([sys.executable, str(HERE / "lead_video.py"), "--own-voice"], capture_output=True, text=True,
                          cwd=str(HERE.parent))
    BUILD["log"] = (proc.stdout + proc.stderr)[-3000:]
    BUILD["state"] = "done" if proc.returncode == 0 else "error"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, kind: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            lines = lead_video.lead_beats()
            page = PAGE.replace("__LINES__", json.dumps(lines, ensure_ascii=False)).replace("__DONE__", json.dumps(recorded()))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path.startswith("/clip/") and path[6:].isdigit():
            clip = VOICE_DIR / f"{int(path[6:]):02d}.webm"
            self._send(200, clip.read_bytes(), "audio/webm") if clip.exists() else self._send(404, b"not recorded")
        elif path == "/build-status":
            out = next(iter(sorted(lead_video.OUT.glob("fengwei-part-own-voice.mp4"))), None)
            self._send(200, json.dumps({**BUILD, "out": str(out) if out else ""}).encode(), "application/json")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        if self.headers.get("Origin") not in (None, ORIGIN):  # only this page may write
            self._send(403, b"forbidden")
            return
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BYTES:
            self._send(413, b"recording too large")
            return
        body = self.rfile.read(length) if length else b""
        if path.startswith("/save/") and path[6:].isdigit():
            n = int(path[6:])
            if not 0 <= n < len(lead_video.lead_beats()) or not body:
                self._send(400, b"bad line or empty recording")
                return
            VOICE_DIR.mkdir(parents=True, exist_ok=True)
            (VOICE_DIR / f"{n:02d}.webm").write_bytes(body)
            self._send(200, b"saved")
        elif path == "/build":
            if BUILD["state"] != "running":
                threading.Thread(target=build, daemon=True).start()
            self._send(200, b"started")
        else:
            self._send(404, b"not found")


def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Voice recorder: {ORIGIN}/  (recordings: {VOICE_DIR})")
    print("Keep this window open. Close it when you are done.")
    threading.Timer(0.8, webbrowser.open, args=(ORIGIN + "/",)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
