#!/usr/bin/env python3
"""
Connectivity and configuration check.

Run this FIRST, before you try to start the app:

    cd backend
    .venv/bin/python ../scripts/doctor.py

It tells you exactly which pieces are working and which need a key,
without you having to read a stack trace.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def fail(msg: str, hint: str = "") -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")
    if hint:
        print(f"        {DIM}{hint}{RESET}")


def skip(msg: str, hint: str = "") -> None:
    print(f"  {YELLOW}SKIP{RESET}  {msg}")
    if hint:
        print(f"        {DIM}{hint}{RESET}")


async def main() -> int:
    print("\nDFS Edge - system check\n" + "=" * 48)
    failures = 0

    # ---- 1. Python packages -------------------------------------------
    print("\nPython packages")
    for pkg in ("fastapi", "uvicorn", "httpx", "anthropic", "dotenv"):
        try:
            __import__(pkg)
            ok(pkg)
        except ImportError:
            fail(pkg, "run: pip install -r backend/requirements.txt")
            failures += 1

    # ---- 2. Configuration ---------------------------------------------
    print("\nConfiguration")
    from app.config import PROJECT_ROOT, get_settings

    settings = get_settings()

    if (PROJECT_ROOT / ".env").exists():
        ok(".env file found")
    else:
        skip(".env file not found", f"run: cp .env.example .env   (in {PROJECT_ROOT})")

    if settings.has_claude:
        ok(f"ANTHROPIC_API_KEY set (model: {settings.anthropic_model})")
    else:
        skip("ANTHROPIC_API_KEY not set", "AI analysis will be disabled")

    if settings.has_odds:
        ok(f"ODDS_API_KEY set (books: {', '.join(settings.odds_bookmakers)})")
    else:
        skip("ODDS_API_KEY not set", "betting lines will be disabled")

    try:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        from app import cache

        cache.put("doctor:test", {"hello": "world"}, ttl=60)
        assert cache.get("doctor:test") == {"hello": "world"}
        cache.clear("doctor:")
        ok(f"cache database writable ({settings.db_path})")
    except Exception as exc:  # noqa: BLE001
        fail(f"cache database not writable: {exc}")
        failures += 1

    # ---- 3. Live data sources -----------------------------------------
    print("\nData sources")
    today = date.today().isoformat()

    from app.clients import mlb, odds, weather
    from app.clients.http import close_client

    try:
        games = await mlb.get_schedule(today, force=True)
        ok(f"MLB Stats API reachable - {len(games)} game(s) on {today}")
        if games:
            g = games[0]
            teams = g.get("teams", {})
            print(
                f"        {DIM}e.g. {teams.get('away',{}).get('team',{}).get('name')}"
                f" @ {teams.get('home',{}).get('team',{}).get('name')}{RESET}"
            )
    except Exception as exc:  # noqa: BLE001
        fail(f"MLB Stats API: {exc}", "check your internet connection")
        failures += 1

    try:
        wx = await weather.get_game_weather(41.9484, -87.6553, f"{today}T20:00:00Z")
        if wx and wx.get("temp_f") is not None:
            ok(f"Open-Meteo reachable - Wrigley {round(wx['temp_f'])}F")
        else:
            fail("Open-Meteo returned no forecast")
            failures += 1
    except Exception as exc:  # noqa: BLE001
        fail(f"Open-Meteo: {exc}")
        failures += 1

    if settings.has_odds:
        try:
            lines = await odds.get_game_lines("mlb", force=True)
            ok(f"The Odds API reachable - {len(lines)} game line(s)")
            usage = odds.get_usage()
            if usage:
                print(
                    f"        {DIM}credits remaining: {usage.get('remaining')}"
                    f" (this call cost {usage.get('last_call_cost')}){RESET}"
                )
        except Exception as exc:  # noqa: BLE001
            fail(f"The Odds API: {exc}", "check ODDS_API_KEY in .env")
            failures += 1
    else:
        skip("The Odds API (no key)")

    if settings.has_claude:
        try:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            msg = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=20,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            )
            reply = "".join(
                b.text for b in msg.content if getattr(b, "type", "") == "text"
            ).strip()
            ok(f"Anthropic API reachable - replied '{reply}'")
        except Exception as exc:  # noqa: BLE001
            fail(
                f"Anthropic API: {exc}",
                "check ANTHROPIC_API_KEY and ANTHROPIC_MODEL in .env",
            )
            failures += 1
    else:
        skip("Anthropic API (no key)")

    await close_client()

    # ---- Summary -------------------------------------------------------
    print("\n" + "=" * 48)
    if failures == 0:
        print(f"{GREEN}Everything that's configured is working.{RESET}")
        print("\nStart the app with:")
        print("  cd backend && .venv/bin/uvicorn app.main:app --reload")
        print("  cd frontend && npm run dev")
    else:
        print(f"{RED}{failures} check(s) failed.{RESET} Fix those before starting the app.")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
