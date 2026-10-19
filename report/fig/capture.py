"""Capture report figures: three diagrams and three real-app screenshots (read-only)."""

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

FIG = Path(__file__).parent
ROOT = Path(__file__).resolve().parents[2]
PORT = 8767


def start_app() -> subprocess.Popen:
    proc = subprocess.Popen([str(ROOT / "venv/Scripts/python.exe"), "webapp.py", "--port", str(PORT)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return proc
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("app did not start")


async def main() -> None:
    only = set(sys.argv[1:])
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        if not only or "diagrams" in only:
            page = await browser.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=2)
            await page.goto((FIG / "diagrams.html").as_uri())
            for name in ("architecture", "pipeline", "evaluation"):
                await page.locator(f"#fig-{name}").screenshot(path=str(FIG / f"{name}.png"))
            await page.close()
        if not only or "app" in only:
            proc = start_app()
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=2)
                await page.goto(f"http://127.0.0.1:{PORT}/")
                await page.wait_for_timeout(1500)
                await page.evaluate("showTab('find'); document.getElementById('findDescription').value = 'Noise cancelling headphones under NZD 400'; doFind();")
                await page.wait_for_function("document.getElementById('findStatus').textContent.includes('recommendations')", timeout=150000)
                await page.wait_for_timeout(800)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.screenshot(path=str(FIG / "find.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 860})
                await page.locator("#findRecommendations button", has_text="Compare & monitor").first.click()
                await page.wait_for_function("document.getElementById('previewSection').classList.contains('open')", timeout=90000)
                await page.wait_for_timeout(1500)
                await page.screenshot(path=str(FIG / "preview.png"))
                await page.evaluate("hidePreview(); showTab('monitors')")
                await page.wait_for_timeout(800)
                await page.evaluate("openDetail(4)")
                await page.wait_for_timeout(2000)
                await page.screenshot(path=str(FIG / "detail.png"))
            finally:
                proc.terminate()
        await browser.close()
    print("done")


asyncio.run(main())
