"""Narrated video of the project lead's part of the demo (opening, design, results, delivery).

    venv\\Scripts\\python.exe presentation\\lead_video.py [--voice en-NZ-MitchellNeural]

Reads Fengwei's lines from build_presentation.SEGMENTS, voices each beat with Microsoft
neural TTS (edge-tts, online), drives slides + the live demo app in the installed Edge
with Playwright, records the screen and muxes everything into
presentation/lead-video/fengwei-part.mp4. Needs: pip install edge-tts playwright imageio-ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "lead-video"
PORT = 8766
VOICE = "en-US-AndrewNeural"
GAP = 0.7  # seconds of silence after each beat

sys.path.insert(0, str(HERE))
import build_presentation as bp  # noqa: E402

# One visual cue per beat, in the order of Fengwei's beats.
CUES = [
    "title", "app", "app-type", "app",                       # opening
    "arch", "arch-before", "arch-data", "arch-agents", "arch-rule",  # design
    "eval", "eval-price", "eval-model",                      # results
    "eng", "app",                                            # delivery, hand-over
]

SLIDES = """<!doctype html><html><head><meta charset="utf-8"><style>
:root { --ink:#18212b; --muted:#5b6673; --line:#dfe3e8; --accent:#1d5f8c; --soft:#e4eef6; --ok:#2f7d4f; --bad:#b2432f; --paper:#f6f7f9; }
* { box-sizing:border-box; } html,body { margin:0; width:1280px; height:720px; overflow:hidden; background:var(--paper); color:var(--ink);
  font-family:"Segoe UI","IBM Plex Sans",system-ui,sans-serif; }
.scene { position:absolute; inset:0 0 86px 0; padding:44px 64px; display:none; flex-direction:column; gap:22px; }
.scene.on { display:flex; }
h1 { margin:0; font-size:44px; } h2 { margin:0; font-size:32px; } .muted { color:var(--muted); }
.kicker { text-transform:uppercase; letter-spacing:.08em; font-size:14px; font-weight:700; color:var(--accent); }
#app { padding:0; } #app iframe { width:100%; height:100%; border:0; background:white; }
.team { display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-top:20px; }
.team div { background:white; border:1px solid var(--line); border-radius:12px; padding:14px; font-size:17px; }
.team b { display:block; font-size:19px; } .team span { color:var(--muted); font-size:14px; }
.flow { display:flex; align-items:center; gap:10px; flex-wrap:nowrap; }
.box { background:white; border:2px solid var(--line); border-radius:12px; padding:12px 14px; font-size:17px; font-weight:600; text-align:center;
  min-width:120px; transition:all .35s; }
.box small { display:block; font-weight:400; color:var(--muted); font-size:13px; margin-top:3px; }
.box.agent { border-color:#b9cfe0; }
.box.hl { border-color:var(--accent); background:var(--soft); transform:translateY(-4px); box-shadow:0 8px 20px rgba(29,95,140,.18); }
.arrow { color:var(--muted); font-size:22px; }
.retry { margin-left:330px; color:var(--muted); font-size:15px; border:2px dashed var(--line); border-top:0; border-radius:0 0 12px 12px;
  width:430px; padding:6px 10px; text-align:center; transition:all .35s; }
.retry.hl { border-color:var(--accent); color:var(--accent); }
.before { display:flex; gap:16px; align-items:center; font-size:18px; }
.card { background:white; border:1px solid var(--line); border-radius:12px; padding:14px 18px; transition:all .35s; }
.card.hl { border-color:var(--accent); box-shadow:0 8px 20px rgba(29,95,140,.18); }
.card.off { opacity:.35; }
.strike { text-decoration:line-through; color:var(--bad); }
.rule { font-size:24px; font-weight:700; text-align:center; padding:14px; border-radius:12px; background:white; border:2px solid var(--line); transition:all .35s; }
.rule.hl { border-color:var(--ok); color:var(--ok); background:#eaf6ef; }
table { border-collapse:collapse; width:100%; background:white; border-radius:12px; overflow:hidden; font-size:20px; }
th,td { text-align:left; padding:12px 16px; border-bottom:1px solid var(--line); } th { color:var(--muted); font-size:14px; text-transform:uppercase; letter-spacing:.05em; }
td.num { font-variant-numeric:tabular-nums; font-weight:700; white-space:nowrap; }
tr { transition:background .35s; } tr.hl td { background:var(--soft); }
.fixes { display:flex; gap:10px; flex-wrap:wrap; font-size:16px; } .fixes span { background:white; border:1px solid var(--line); border-radius:999px; padding:6px 12px; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:24px; }
.cols ul { margin:10px 0 0; padding-left:22px; font-size:21px; line-height:1.6; }
.file { font-family:Consolas,monospace; background:var(--soft); padding:2px 8px; border-radius:6px; font-size:18px; }
#caption { position:absolute; left:0; right:0; bottom:0; height:86px; background:#18212b; color:white; display:flex; align-items:center;
  justify-content:center; padding:0 60px; font-size:21px; line-height:1.35; text-align:center; }
</style></head><body>
<section class="scene" id="title"><span class="kicker">159.333 Computer Science Project · Massey University</span>
<h1>Bargain Agent</h1><p class="muted" style="font-size:24px;margin:0">Describe what you want. Agents find it, compare New Zealand shops and watch the price.</p>
<div class="team"><div><b>Fengwei Zhang</b><span>Project Lead</span></div><div><b>Gang Zhao</b><span>Data Engineer</span></div>
<div><b>Tianshuo Gao</b><span>Interface Developer</span></div><div><b>Xiao Zhang</b><span>Agent Developer &amp; Test</span></div>
<div><b>Wenhan Zhang</b><span>Agent Developer &amp; Test</span></div></div></section>
<section class="scene" id="app"><iframe id="appframe" title="Bargain Agent app"></iframe></section>
<section class="scene" id="arch"><span class="kicker">How it is built</span><h2>Data from Google Shopping NZ · eight AI agents · code makes the decisions</h2>
<div class="before"><div class="card" id="c-before"><span class="strike">Proposal: scrape each shop (BeautifulSoup, Selenium)</span> <span class="muted">blocked · one fragile parser per site</span></div>
<span class="arrow">→</span><div class="card" id="c-data"><b>Now:</b> search APIs (Serper, SerpApi backup) return every merchant's listing in one call</div></div>
<div class="flow"><div class="box">Description</div><span class="arrow">→</span><div class="box agent" id="b-u">Understand<small>agent</small></div><span class="arrow">→</span>
<div class="box agent" id="b-r">Rewrite<small>agent</small></div><span class="arrow">→</span><div class="box" id="b-s">Search<small>Google Shopping NZ</small></div><span class="arrow">→</span>
<div class="box agent" id="b-sel">Select<small>agent</small></div><span class="arrow">→</span><div class="box agent" id="b-d">Describe<small>agent</small></div></div>
<div class="retry" id="retry">nothing suitable → try new search terms once</div>
<div class="flow"><div class="box">Same product at<small>other merchants</small></div><span class="arrow">→</span><div class="box">SQLite<small>up to 5 merchants</small></div><span class="arrow">→</span>
<div class="box">Scheduler<small>one search per check</small></div><span class="arrow">→</span><div class="box agent" id="b-v">Offer verifier<small>agent · veto only</small></div><span class="arrow">→</span>
<div class="box" id="b-mail">Alert email<small>trusted shop · once per drop</small></div></div>
<div class="rule" id="rule">Agents choose, code decides</div></section>
<section class="scene" id="eval"><span class="kicker">Evaluation with real data</span><h2>How accurate is it?</h2>
<table><thead><tr><th>What we measured</th><th>Data</th><th>Result</th></tr></thead><tbody>
<tr id="r1"><td>Real price accuracy, first run</td><td>15 products, 5 NZ shops</td><td class="num">9/15 · 60%</td></tr>
<tr id="r2"><td>Real price accuracy, after fixes</td><td>14 products</td><td class="num">14/14 · 100%</td></tr>
<tr id="r3"><td>Price agent on shop pages</td><td>13 pages, real model</td><td class="num">13/13 · false price 0</td></tr>
<tr id="r4"><td>Instruction parsing</td><td>16 commands, real model</td><td class="num">15/16 · 98.1% of fields</td></tr>
<tr id="r5"><td>Alert triggering</td><td>10 scenarios, 19 steps</td><td class="num">precision 1.0 · recall 1.0</td></tr></tbody></table>
<div class="fixes" id="fixes"><span class="muted" style="border:0;background:none">Misses we fixed:</span><span>superscript cents</span><span>Afterpay instalment</span><span>RRP instead of sale price</span><span>price before GST</span></div></section>
<section class="scene" id="eng"><span class="kicker">Engineering and delivery</span><h2>Safe to run, easy to share</h2>
<div class="cols"><div class="card"><b style="font-size:24px">Cost and safety</b><ul><li>Daily call limit per provider</li><li>At most 5 merchants per product</li>
<li>One search per check</li><li>Server only on your own computer, CSRF and origin checks</li></ul></div>
<div class="card"><b style="font-size:24px">Delivery</b><ul><li>Unzip <span class="file">BargainAgent-windows.zip</span></li><li>Double-click <span class="file">BargainAgent.exe</span>, opens in the browser</li>
<li>No Python needed</li><li>Source version installs itself on first run</li></ul></div></div></section>
<div id="caption"></div>
<script>
function show(id) { document.querySelectorAll('.scene').forEach(s => s.classList.toggle('on', s.id === id)); }
function hl(ids) { document.querySelectorAll('.hl').forEach(e => e.classList.remove('hl')); (ids || []).forEach(i => { const e = document.getElementById(i); if (e) e.classList.add('hl'); }); }
function caption(t) { document.getElementById('caption').textContent = t; }
</script></body></html>"""


def ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path) -> float:
    err = subprocess.run([ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def caption_chunks(text: str, limit: int = 150) -> list[str]:
    """Sentences grouped into captions that fit two lines of the caption bar."""
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if chunks and len(chunks[-1]) + 1 + len(sentence) <= limit:
            chunks[-1] += " " + sentence
        elif len(sentence) > limit:  # long sentence: split at commas
            part = ""
            for piece in re.split(r"(?<=,)\s+", sentence):
                if part and len(part) + 1 + len(piece) > limit:
                    chunks.append(part)
                    part = piece
                else:
                    part = f"{part} {piece}".strip()
            chunks.append(part)
        else:
            chunks.append(sentence)
    return chunks


async def run_captions(page, text: str, length: float) -> None:
    chunks = caption_chunks(text)
    total = sum(len(c) for c in chunks)
    for chunk in chunks:
        await page.evaluate("t => caption(t)", chunk)
        await asyncio.sleep(length * len(chunk) / total)


def lead_beats() -> list[str]:
    return [say for seg in bp.SEGMENTS if seg[0] == "fw" for say, _ in seg[4]]


async def synthesize(beats: list[str], voice: str) -> list[Path]:
    import edge_tts

    audio = OUT / "audio" / voice
    audio.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, text in enumerate(beats):
        path = audio / f"{n:02d}.mp3"
        stamp = audio / f"{n:02d}.txt"
        if not (path.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == voice + "\n" + text):
            await edge_tts.Communicate(text, voice, rate="-4%").save(str(path))
            stamp.write_text(voice + "\n" + text, encoding="utf-8")
        paths.append(path)
    return paths


def start_server() -> subprocess.Popen:
    if not (ROOT / "demo.db").exists():
        subprocess.run([sys.executable, "-m", "demo.seed_demo"], cwd=ROOT, check=True)
    proc = subprocess.Popen([sys.executable, "webapp.py", "--port", str(PORT), "--db-path", "demo.db", "--demo"],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return proc
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("demo server did not start")


async def record(beats: list[str], clips: list[Path]) -> tuple[Path, list[float]]:
    from playwright.async_api import async_playwright

    slides = OUT / "slides.html"
    slides.write_text(SLIDES, encoding="utf-8")
    raw = OUT / "raw"
    for old in raw.glob("*.webm") if raw.exists() else []:
        try:
            old.unlink()
        except OSError:  # still open in a player; the new recording gets its own name
            pass
    starts: list[float] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720},
                                            record_video_dir=str(raw), record_video_size={"width": 1280, "height": 720})
        page = await context.new_page()
        t_video = time.monotonic()
        await page.goto(slides.as_uri())
        await page.evaluate(f"document.getElementById('appframe').src = 'http://127.0.0.1:{PORT}/'")
        await page.wait_for_timeout(2500)  # let the app load inside the frame
        app = page.frame_locator("#appframe")
        for text, cue, clip in zip(beats, CUES, clips):
            scene, _, detail = cue.partition("-")
            await page.evaluate("id => show(id)", scene)
            highlight = {"before": ["c-before"], "data": ["c-data", "b-s"], "rule": ["b-v", "b-mail", "rule"],
                         "price": ["r1", "r2", "fixes"], "model": ["r3", "r4", "r5"]}.get(detail, [])
            await page.evaluate("ids => hl(ids)", highlight)
            length = duration(clip)
            starts.append(time.monotonic() - t_video)
            captions = asyncio.create_task(run_captions(page, text, length))
            began = time.monotonic()
            if detail == "agents":  # light up each step of the Find pipeline as it is named
                steps = [("Understand agent", ["b-u"]), ("Rewrite agent", ["b-r"]), ("Select agent", ["b-s", "b-sel"]),
                         ("Describe agent", ["b-d"]), ("nothing suitable", ["retry", "b-sel", "b-r"])]
                for words, ids in steps:
                    at = length * text.index(words) / len(text)
                    await asyncio.sleep(max(0.0, at - (time.monotonic() - began)))
                    await page.evaluate("ids => hl(ids)", ids)
            if detail == "type":  # show a Chinese description being typed while it is mentioned
                box = app.locator("#findDescription")
                await box.fill("")
                await box.type("跑步用的无线耳机，黑色，300 纽币以内", delay=120)
            remaining = length + GAP - (time.monotonic() - began)
            await page.wait_for_timeout(max(0, int(remaining * 1000)))
            await captions
        if scene == "app":
            await app.locator("#findDescription").fill("")
        await page.wait_for_timeout(800)
        video_path = await page.video.path()
        await context.close()
        await browser.close()
    return Path(video_path), starts


def mux(video: Path, clips: list[Path], starts: list[float], label: str) -> Path:
    out = OUT / f"fengwei-part-{label}.mp4"
    inputs = ["-i", str(video)]
    for clip in clips:
        inputs += ["-i", str(clip)]
    delays = ";".join(f"[{n + 1}:a]adelay={int(t * 1000)}|{int(t * 1000)}[a{n}]" for n, t in enumerate(starts))
    mix = "".join(f"[a{n}]" for n in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0[aout]"
    subprocess.run([ffmpeg(), "-y", *inputs, "-filter_complex", f"{delays};{mix}", "-map", "0:v", "-map", "[aout]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "22", "-c:a", "aac", "-b:a", "160k",
                    "-shortest", str(out)], check=True, capture_output=True)
    return out


def own_voice(beats: list[str]) -> list[Path]:
    """The presenter's recordings (presentation/record_voice.py), trimmed and levelled."""
    folder = OUT / "voice"
    clips = []
    for n in range(len(beats)):
        found = sorted(p for p in folder.glob(f"{n:02d}.*") if p.suffix in (".webm", ".ogg", ".wav", ".m4a", ".mp3")
                       and not p.name.endswith(".clean.wav"))
        if not found:
            raise SystemExit(f"Line {n + 1} has no recording yet; run presentation/record_voice.py.")
        clean = folder / f"{n:02d}.clean.wav"
        trim = "silenceremove=start_periods=1:start_threshold=-45dB"
        chain = f"{trim},areverse,{trim},areverse"
        if "--keep-pauses" not in sys.argv:  # pauses over 0.6 s become 0.35 s; speech itself is untouched
            chain += ",silenceremove=stop_periods=-1:stop_duration=0.6:stop_threshold=-32dB:stop_silence=0.35"
        subprocess.run([ffmpeg(), "-y", "-i", str(found[-1]), "-af", f"{chain},loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-ac", "1", "-ar", "48000", str(clean)], check=True, capture_output=True)
        clips.append(clean)
    return clips


def main() -> int:
    voice = sys.argv[sys.argv.index("--voice") + 1] if "--voice" in sys.argv else VOICE
    beats = lead_beats()
    if len(beats) != len(CUES):
        raise SystemExit(f"{len(beats)} lines but {len(CUES)} cues; update CUES.")
    OUT.mkdir(exist_ok=True)
    if "--own-voice" in sys.argv:
        voice, clips = "own-voice", own_voice(beats)
    else:
        clips = asyncio.run(synthesize(beats, voice))
    total = sum(duration(c) for c in clips) + GAP * len(clips)
    print(f"voice {voice}: {len(clips)} lines, about {total / 60:.1f} min")
    server = start_server()
    try:
        video, starts = asyncio.run(record(beats, clips))
    finally:
        server.terminate()
    out = mux(video, clips, starts, voice)
    (OUT / "timing.json").write_text(json.dumps({"voice": voice, "starts": starts}, indent=2), encoding="utf-8")
    print(f"video: {out} ({out.stat().st_size / 1e6:.1f} MB, {duration(out) / 60:.1f} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
