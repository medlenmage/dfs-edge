"""Internal: boot the demo server in a thread and screenshot each tab."""

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("ODDS_API_KEY", "demo-mode-fake-key")
os.environ.setdefault("DB_PATH", "data/preview.db")

import preview  # noqa: E402
import test_pipeline as fx  # noqa: E402

fx.patch()
from app.clients import weather  # noqa: E402

weather.get_game_weather = preview._weather

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

preview.mount_ui(app, ROOT / "frontend" / "dist")

config = uvicorn.Config(app, host="127.0.0.1", port=8011, log_level="error")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
time.sleep(4)

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

TABS = ["Stacks", "Hitters", "Games", "AI analysis"]

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    for scheme, suffix in (("light", ""), ("dark", "-dark")):
        page = browser.new_page(
            viewport={"width": 1340, "height": 1000},
            device_scale_factor=2,
            color_scheme=scheme,
        )
        page.goto("http://127.0.0.1:8011/", wait_until="networkidle")
        page.wait_for_timeout(2500)
        for tab in TABS:
            page.get_by_role("button", name=tab, exact=True).click()
            page.wait_for_timeout(700)
            name = tab.lower().replace(" ", "-")
            page.screenshot(path=str(OUT / f"{name}{suffix}.png"), full_page=True)
            print("wrote", OUT / f"{name}{suffix}.png")
        page.close()
    browser.close()

print("done")
