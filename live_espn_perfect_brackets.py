# live_espn_perfect_brackets.py

from __future__ import annotations

import asyncio
import csv
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import uvicorn

# -----------------------------
# Config
# -----------------------------
BRACKET_URL = "https://www.espn.com/perfect-bracket/"
# Unofficial ESPN scoreboard JSON used by many public examples.
SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball/scoreboard?groups=100&seasontype=3"
)

POLL_SECONDS = 30
POST_FINAL_DELAY_SECONDS = 90

LIVE_CSV_FILE = Path("espn_perfect_brackets_log.csv")
GAME_CSV_FILE = Path("espn_perfect_brackets_by_game.csv")

PATTERNS = [
    re.compile(r"(?i)([\d, ]+)\s+perfect brackets remaining"),
    re.compile(r"(?i)how many perfect brackets are left\??\s*([\d, ]+)"),
]

latest_value: Optional[int] = None
latest_timestamp: Optional[str] = None

# game_id -> metadata for games already written to GAME_CSV_FILE
recorded_games: dict[str, dict] = {}
# game_id -> pending final metadata waiting for bracket count to settle
pending_finals: dict[str, dict] = {}


# -----------------------------
# Helpers
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_number(s: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d]", "", s)
    if not cleaned:
        return None
    return int(cleaned)


def ensure_live_csv() -> None:
    if not LIVE_CSV_FILE.exists():
        with LIVE_CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_utc", "perfect_brackets_remaining"])


def ensure_game_csv() -> None:
    if not GAME_CSV_FILE.exists():
        with GAME_CSV_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "game_number",
                "date",
                "update_time_et",
                "matchup",
                "winner",
                "score",
                "perfect_brackets_remaining",
                "scope",
                "source",
            ])


def append_live_csv(timestamp: str, value: int) -> None:
    ensure_live_csv()
    with LIVE_CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, value])


def load_existing_game_rows() -> None:
    ensure_game_csv()
    with GAME_CSV_FILE.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            matchup = row.get("matchup", "")
            date = row.get("date", "")
            key = f"{date}|{matchup}"
            recorded_games[key] = row


def next_game_number() -> int:
    ensure_game_csv()
    with GAME_CSV_FILE.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return len(rows) + 1


def append_game_csv(
    date_str: str,
    update_time_et: str,
    matchup: str,
    winner: str,
    score: str,
    perfect_brackets_remaining: int,
) -> None:
    ensure_game_csv()
    game_number = next_game_number()
    with GAME_CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            game_number,
            date_str,
            update_time_et,
            matchup,
            winner,
            score,
            perfect_brackets_remaining,
            "ESPN Tournament Challenge perfect-bracket tracker",
            "ESPN perfect-bracket tracker + ESPN scoreboard",
        ])


def extract_count_from_text(body_text: str) -> int:
    for pattern in PATTERNS:
        match = pattern.search(body_text)
        if match:
            value = normalize_number(match.group(1))
            if value is not None:
                return value

    for line in body_text.splitlines():
        if "perfect" in line.lower():
            nums = re.findall(r"\d[\d, ]*", line)
            for num in nums:
                value = normalize_number(num)
                if value is not None:
                    return value

    raise ValueError("Could not find the live perfect-bracket count on the page.")


async def extract_count_from_page(page) -> int:
    await page.wait_for_timeout(3000)
    body_text = await page.locator("body").inner_text()
    return extract_count_from_text(body_text)


async def fetch_count(browser) -> int:
    page = await browser.new_page(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 1200},
    )
    try:
        await page.goto(BRACKET_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        return await extract_count_from_page(page)
    finally:
        await page.close()


def parse_event(event: dict) -> Optional[dict]:
    comps = event.get("competitions", [])
    if not comps:
        return None

    comp = comps[0]
    competitors = comp.get("competitors", [])
    if len(competitors) != 2:
        return None

    # Sort so home/away ordering does not matter for storage
    competitors_sorted = sorted(competitors, key=lambda c: c.get("homeAway", ""))

    team1 = competitors_sorted[0].get("team", {}).get("displayName", "Team 1")
    team2 = competitors_sorted[1].get("team", {}).get("displayName", "Team 2")
    score1 = competitors_sorted[0].get("score", "")
    score2 = competitors_sorted[1].get("score", "")

    winner = ""
    for c in competitors:
        if c.get("winner") is True:
            winner = c.get("team", {}).get("displayName", "")
            break

    status = comp.get("status", {})
    type_info = status.get("type", {})
    status_name = type_info.get("name", "")
    status_desc = type_info.get("description", "")
    completed = bool(type_info.get("completed", False))

    # ESPN often uses STATUS_FINAL or completed=True
    is_final = completed or "final" in status_name.lower() or "final" in status_desc.lower()

    date_raw = event.get("date", "")
    # Keep UTC date string simple in file; if you want exact ET conversion later, I can add it.
    date_str = date_raw[:10] if date_raw else ""

    matchup = f"{team1} vs. {team2}"
    score = f"{score1}-{score2}"

    return {
        "event_id": str(event.get("id", "")),
        "date": date_str,
        "matchup": matchup,
        "winner": winner,
        "score": score,
        "is_final": is_final,
        "status": status_desc or status_name,
    }


async def fetch_scoreboard_events(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(SCOREBOARD_URL, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    events = data.get("events", [])
    parsed = []
    for event in events:
        item = parse_event(event)
        if item:
            parsed.append(item)
    return parsed


async def scoreboard_loop() -> None:
    global latest_value, latest_timestamp

    async with httpx.AsyncClient() as client:
        while True:
            try:
                events = await fetch_scoreboard_events(client)
                now = datetime.now(timezone.utc)

                for event in events:
                    key = f"{event['date']}|{event['matchup']}"

                    if event["is_final"] and key not in recorded_games and key not in pending_finals:
                        pending_finals[key] = {
                            **event,
                            "detected_at": now,
                        }

                # Promote pending finals to recorded rows after delay and when a bracket count exists
                to_record = []
                for key, event in pending_finals.items():
                    elapsed = (now - event["detected_at"]).total_seconds()
                    if elapsed >= POST_FINAL_DELAY_SECONDS and latest_value is not None:
                        to_record.append((key, event))

                for key, event in sorted(to_record, key=lambda x: (x[1]["date"], x[1]["matchup"])):
                    append_game_csv(
                        date_str=event["date"],
                        update_time_et=utc_now_iso(),
                        matchup=event["matchup"],
                        winner=event["winner"],
                        score=event["score"],
                        perfect_brackets_remaining=latest_value,
                    )
                    recorded_games[key] = event
                    del pending_finals[key]
                    print(
                        f"[{utc_now_iso()}] Logged final game: {event['matchup']} | "
                        f"{event['winner']} | {event['score']} | brackets left: {latest_value:,}"
                    )

            except Exception as e:
                print(f"[{utc_now_iso()}] scoreboard error: {e}")

            await asyncio.sleep(POLL_SECONDS)


async def poll_bracket_loop() -> None:
    global latest_value, latest_timestamp

    ensure_live_csv()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            last_value = None
            while True:
                timestamp = utc_now_iso()
                try:
                    value = await fetch_count(browser)
                    latest_value = value
                    latest_timestamp = timestamp

                    if value != last_value:
                        append_live_csv(timestamp, value)
                        print(f"[{timestamp}] Perfect brackets remaining: {value:,}")
                        last_value = value
                    else:
                        print(f"[{timestamp}] No change: {value:,}")

                except Exception as e:
                    print(f"[{timestamp}] bracket polling error: {e}")

                await asyncio.sleep(POLL_SECONDS)
        finally:
            await browser.close()


# -----------------------------
# FastAPI app
# -----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_existing_game_rows()
    asyncio.create_task(poll_bracket_loop())
    asyncio.create_task(scoreboard_loop())
    yield

app = FastAPI(lifespan=lifespan)


@app.get("/api/current")
async def api_current():
    return JSONResponse(
        {
            "timestamp_utc": latest_timestamp,
            "perfect_brackets_remaining": latest_value,
            "tracker_source": BRACKET_URL,
            "scoreboard_source": SCOREBOARD_URL,
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>ESPN Perfect Brackets Live</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 40px; }
    .value { font-size: 56px; font-weight: bold; margin: 20px 0; }
    .meta { color: #555; margin-bottom: 10px; }
  </style>
</head>
<body>
  <h1>ESPN Perfect Brackets Live</h1>
  <div class="value" id="value">Loading...</div>
  <div class="meta" id="meta"></div>
  <div class="meta">Game rows are being written to <code>espn_perfect_brackets_by_game.csv</code></div>

  <script>
    async function refresh() {
      const res = await fetch('/api/current');
      const data = await res.json();

      document.getElementById('value').textContent =
        data.perfect_brackets_remaining == null
          ? 'Unavailable'
          : Number(data.perfect_brackets_remaining).toLocaleString();

      document.getElementById('meta').textContent =
        data.timestamp_utc
          ? `Last updated: ${data.timestamp_utc}`
          : 'Waiting for first successful scrape...';
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)