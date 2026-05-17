import json

single_cell_source = """
import sys
import subprocess

# 1. Install required packages automatically
def install_dependencies():
    packages = ["playwright", "nest-asyncio", "faker"]
    for pkg in packages:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

    # Install chromium for playwright
    print("Ensuring Playwright Chromium is installed...")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])

install_dependencies()

# 2. Imports and Setup
import asyncio
import re
import nest_asyncio
from faker import Faker
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

nest_asyncio.apply()

# 3. Core Meeting Logic
async def log(msg: str):
    print(msg)

async def find_first(page, selectors, timeout=8000):
    for s in selectors:
        try:
            loc = page.locator(s)
            await loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            continue
    return None

async def join_meeting(tag: str, page, meeting_url: str, passcode: str,
                       name: str, wait_seconds: int,
                       joined_event: asyncio.Event,
                       winner_page: dict, winner_lock: asyncio.Lock,
                       admitted_pages: list, admitted_lock: asyncio.Lock):
    try:
        await log(f"{tag} 🌐 Navigating to meeting URL ...")
        await page.goto(meeting_url, timeout=120000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        await page.evaluate('''
            () => document.querySelectorAll('[id*="onetrust"],[class*="onetrust"]')
                          .forEach(e => e.remove())
        ''')

        try:
            cancel_btn = page.locator("button:has-text('Cancel')")
            await cancel_btn.wait_for(state="visible", timeout=5000)
            await cancel_btn.click()
            await log(f"{tag} ✅ Dismissed popup.")
        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(1000)

        try:
            join_browser_btn = page.locator("button:has-text('Join from browser')")
            await join_browser_btn.wait_for(state="visible", timeout=8000)
            await join_browser_btn.click()
        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(4000)

        pass_input = await find_first(page, [
            "#input-for-pwd", 'input[id="input-for-pwd"]', 'input[placeholder*="passcode"]',
            'input[type="password"]'
        ], timeout=5000)
        if pass_input:
            await pass_input.click(force=True)
            await pass_input.fill(passcode)
            await log(f"{tag} 🔑 Passcode entered.")

        name_input = await find_first(page, [
            "#input-for-name", "#inputname", 'input[placeholder*="Your Name"]',
        ], timeout=8000)
        if name_input:
            await name_input.click(force=True)
            await name_input.fill("")
            await name_input.fill(name)
            await log(f"{tag} 📝 Name set to '{name}'.")

        await page.wait_for_timeout(500)

        try:
            cam_btn = await find_first(page, [
                "button#preview-video-control-button",
                "button[aria-label='Stop Video']",
                "button[aria-label='Turn off video']",
                "button[aria-label='Start Video']",
                "button:has-text('Stop Video')",
            ], timeout=3000)
            if cam_btn:
                aria_label = await cam_btn.get_attribute("aria-label") or ""
                btn_text   = await cam_btn.text_content() or ""
                if "Stop Video" in aria_label or "Turn off video" in aria_label or "Stop Video" in btn_text:
                    await cam_btn.click(force=True)
                    await log(f"{tag} 📷 Camera off (pre-join).")
                elif "Start Video" in aria_label or "Start Video" in btn_text:
                    await log(f"{tag} 📷 Camera already off (pre-join).")
        except Exception:
            pass

        try:
            mute_btn = await find_first(page, [
                "button#preview-audio-control-button",
                "button[aria-label='Mute']",
                "button[aria-label='Unmute']",
            ], timeout=3000)
            if mute_btn:
                aria_label = await mute_btn.get_attribute("aria-label") or ""
                if "Mute" in aria_label and "Unmute" not in aria_label:
                    await mute_btn.click(force=True)
                    await log(f"{tag} 🎤 Muted (pre-join).")
                elif "Unmute" in aria_label:
                    await log(f"{tag} 🎤 Already muted (pre-join).")
        except Exception:
            pass

        await page.wait_for_timeout(300)

        join_btn = await find_first(page, [
            'button.preview-join-button',
            'button[aria-label="Join"]',
            'button:has-text("Join")',
        ], timeout=6000)
        if join_btn:
            await join_btn.click(force=True)
            await log(f"{tag} ✅ Clicked Join!")

        await page.wait_for_timeout(4000)

        try:
            video_btn = await find_first(page, [
                "button[aria-label='Stop Video']",
                "button[aria-label='Turn off video']",
                "button[aria-label='Start Video']",
            ], timeout=5000)
            if video_btn:
                aria_label = await video_btn.get_attribute("aria-label") or ""
                if "Stop Video" in aria_label or "Turn off video" in aria_label:
                    await video_btn.click(force=True)
                    await log(f"{tag} 📷 Camera turned OFF (in-meeting).")
        except Exception:
            pass

        try:
            mic_btn = await find_first(page, [
                "button[aria-label='Mute']",
                "button[aria-label='Unmute']",
                "button[aria-label='Join Audio']",
            ], timeout=3000)
            if mic_btn:
                aria_label = await mic_btn.get_attribute("aria-label") or ""
                if "Unmute" in aria_label:
                    await log(f"{tag} 🎤 Microphone is already muted (in-meeting).")
                elif "Join Audio" in aria_label:
                    await mic_btn.click(force=True)
                    await log(f"{tag} 🎤 Joined audio (in-meeting).")
                    await page.wait_for_timeout(2000)
                    mute_btn = await find_first(page, ["button[aria-label='Mute']", "button[aria-label='Unmute']"], timeout=3000)
                    if mute_btn and "Mute" in (await mute_btn.get_attribute("aria-label") or ""):
                        await mute_btn.click(force=True)
                elif "Mute" in aria_label:
                    await mic_btn.click(force=True)
        except Exception:
            pass

        await log(f"{tag} ⏳ Waiting to be admitted ...")
        waited = 0
        while waited < 600:
            if joined_event.is_set():
                return

            in_meeting = False
            for selector in [".video-avatar__avatar", "button[aria-label='Leave']", "#wc-footer"]:
                try:
                    if await page.locator(selector).count() > 0:
                        in_meeting = True
                        break
                except Exception:
                    pass

            if in_meeting:
                async with admitted_lock:
                    if page not in admitted_pages:
                        admitted_pages.append(page)
                async with winner_lock:
                    if not joined_event.is_set():
                        joined_event.set()
                        winner_page["page"] = page
                        await log(f"🏆 {tag} is the WINNER — '{name}'.")
                return
            await page.wait_for_timeout(10000)
            waited += 10
    except Exception as e:
        if not joined_event.is_set():
            await log(f"{tag} ❌ Error: {e}")

# 4. Interactive Configuration
meeting_id = input("Enter Meeting ID (numbers only): ").strip()
passcode = input("Enter Passcode: ").strip()

while True:
    try:
        num_tabs = int(input("Enter number of bots (10 to 100): ").strip())
        if 10 <= num_tabs <= 100:
            break
        else:
            print("Please enter a number between 10 and 100.")
    except ValueError:
        print("Invalid input. Please enter an integer.")

while True:
    try:
        stay_minutes = int(input("Enter duration to stay in meeting (in minutes): ").strip())
        if stay_minutes > 0:
            break
        else:
            print("Duration must be greater than 0.")
    except ValueError:
        print("Invalid input. Please enter an integer.")

MEETING_URL = f"https://zoom.us/wc/{meeting_id}/join?pwd={passcode}"

faker_in = Faker('en_IN')
TAB_NAMES = [faker_in.name() for _ in range(num_tabs)]
HEADLESS = True

print(f"\\nPrepared to launch {num_tabs} bots with names like: {TAB_NAMES[:3]}...")
print(f"Joining meeting {meeting_id} for {stay_minutes} minutes.\\n")

# 5. Coordinator Loop
async def run_bots():
    joined_event = asyncio.Event()
    session_done_event = asyncio.Event()
    winner_lock = asyncio.Lock()
    winner_page = {"page": None}
    admitted_pages = []
    admitted_lock = asyncio.Lock()

    LAUNCH_ARGS = [
        "--no-sandbox", "--disable-setuid-sandbox", "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream", "--disable-gpu", "--window-size=1280,720"
    ]

    async def launch_and_join(index, name):
        tag = f"[Bot {index + 1}]"
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS, args=LAUNCH_ARGS)
            context = await browser.new_context(permissions=["camera", "microphone"])
            page = await context.new_page()
            try:
                await join_meeting(
                    tag, page, MEETING_URL, passcode, name, stay_minutes * 60,
                    joined_event, winner_page, winner_lock, admitted_pages, admitted_lock
                )
                if joined_event.is_set() and winner_page.get("page") is page:
                    await session_done_event.wait()
            finally:
                if not joined_event.is_set() or winner_page.get("page") is not page:
                    await browser.close()

    tasks = [asyncio.create_task(launch_and_join(i, TAB_NAMES[i])) for i in range(num_tabs)]

    try:
        await asyncio.wait_for(joined_event.wait(), timeout=660)
    except asyncio.TimeoutError:
        print("❌ No tab admitted.")
        for t in tasks: t.cancel()
        return

    print(f"🟢 In meeting! Staying for {stay_minutes} minute(s).")

    stay_seconds = stay_minutes * 60
    for remaining in range(stay_seconds, 0, -30):
        mins, secs = divmod(remaining, 60)
        print(f"⏱️  {mins:02d}:{secs:02d} remaining ...")
        await asyncio.sleep(min(30, remaining))

    print("👋 Time is up! Leaving the meeting ...")
    try:
        wp = winner_page["page"]
        leave = wp.locator("button[aria-label='Leave'], button[aria-label='End'], button:has-text('Leave')")
        await leave.first.wait_for(state="visible", timeout=5000)
        await leave.first.click()
        await wp.wait_for_timeout(1500)
        confirm = wp.locator("button:has-text('Leave Meeting'), button:has-text('Leave for Everyone')")
        await confirm.first.click()
        print("✅ Left the meeting.")
    except Exception as e:
        print(f"⚠️ Leave error: {e}")

    session_done_event.set()
    await asyncio.sleep(2)
    for t in tasks: t.cancel()

# Start
await run_bots()
"""

notebook = {
    "cells": [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# Zoom Bot (All-in-One)\n",
                "This single cell will automatically install all dependencies, prompt you for the Meeting ID/Passcode/Details, and run the bots."
            ]
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [line + "\n" for line in single_cell_source.split('\n')]
        }
    ],
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3"
        },
        "language_info": {
            "name": "python",
            "version": "3.10"
        }
    },
    "nbformat": 4,
    "nbformat_minor": 4
}

with open("zoom_bot.ipynb", "w") as f:
    json.dump(notebook, f, indent=2)
