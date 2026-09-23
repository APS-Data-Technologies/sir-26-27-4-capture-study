"""
Clock-in kiosk prototype — UI layer.

Two screens:
    /           employee kiosk (PIN entry, Google sign-in, ID card scan)
    /dashboard  employer view (summary tiles, staff table, registration,
                linking school ID cards)

Run:
    pip install flask
    python app.py
then open http://localhost:5000  (or http://<this-machine-ip>:5000 from the tablet)

This file is the *interface* sector — it is the "pinterface" that
clock_system.py expects: it collects a PIN, calls into clock_system, and
displays whatever comes back. The clock-in/clock-out rules all live in
clock_system.py. Nothing in this file decides whether an entry is valid.

The flows it draws
------------------
RETURNING employee
    types their 4-6 digit PIN -> popup shows the name on file
    -> "Yes, that's me" flips the clock lever
    -> "No, try again" clears and returns to the numpad.
    Or they tap Sign in with Google and skip the PIN entirely.
    Or they scan their school ID card: that clocks them in or out at once.

NEW employee
    types the temporary code they were issued -> popup asks them to
    choose their own 4-6 digit PIN -> a PIN that's already taken shows
    an error and they pick again.

Barcode scanners
----------------
A USB or Bluetooth scanner acts as a keyboard: it "types" the barcode very
fast and then presses Enter. The kiosk tells a scan apart from a person
typing by that speed (see SCAN_MAX_GAP_MS in the kiosk script), so no
driver or setup is needed; plug the scanner in and scan. Every scan goes
through one function, handleScan(code, source), so a camera scanner can be
added later by calling that same function.
"""

import os
import time
from datetime import datetime

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    url_for,
)

import clock_system as cs

app = Flask(__name__)


# =====================================================================
# INTEGRATION SEAMS — other sectors replace the bodies of these.
# Everything below this block is layout and input handling only.
# =====================================================================

def log_event(event_type, detail=None):
    """Instrumentation sector: replace this body.

    Timing, keystroke counts, error/retry rates, CSV export — all of that
    hangs off here. For now it just prints so the screens are demoable.
    """
    print(f"[event] {event_type} {detail or ''}")


# --- Google sign-in stand-in -----------------------------------------
# Real Google OAuth needs a Google Cloud client ID + secret and an HTTPS
# redirect URI. Until those exist, this fake account picker stands in so
# the linking flow can be built and demoed. To go live, replace
# google_accounts() and the /api/google/signin route with a real OAuth
# round trip; everything downstream (link, lookup, clock) is unchanged.
GOOGLE_IS_STUBBED = True


def google_accounts():
    """Accounts the fake picker offers. Replace with real OAuth."""
    return [
        {"email": "sample.person@school.org", "name": "Sample Person"},
        {"email": "test.employee@school.org", "name": "Test Employee"},
        {"email": "new.hire@school.org", "name": "New Hire"},
    ]


# clock_system tracks WHO is in or out, but not WHEN. The dashboard wants
# a time per person, so the kiosk records one here as punches come in.
# Keyed by PIN -> {"time": "7:52 AM", "method": "PIN"}.
# The instrumentation sector can replace this with real punch history.
last_punch = {}


# A scan clocks in or out instantly, so a card scanned twice in a row (a
# double beep, or someone unsure the first one took) would clock them in
# and straight back out. Within this window a repeat scan of the same card
# just shows the earlier result again instead of flipping the lever.
SCAN_COOLDOWN_SECONDS = 10

# PIN -> (time.monotonic() of the scan, the response it got)
recent_scans = {}


def load_punches():
    """Rows for the dashboard table, built from the live clock_system store."""
    rows = []
    for pin, record in sorted(cs.employee_records.items(), key=lambda kv: kv[1]["name"]):
        punch = last_punch.get(pin, {})
        rows.append({
            "name": record["name"],
            "id": pin,
            "status": "in" if record["status"] == "clocked_in" else "out",
            "temporary": record["temporary"],
            "google": record.get("google_email") or "",
            "badge": record.get("badge") or "",
            "time": punch.get("time", "—"),
            "method": punch.get("method", "—"),
        })
    return rows


# Synthetic roster only — no real staff records (see README ground rules).
def seed_demo_staff():
    """Two staff who've set their own PIN, one still on an issued code.

    Each gets a made-up card number so the scan flow can be tried without
    real ID cards; link a real card from the dashboard to test hardware.
    """
    ready = cs.register_employee("Sample Person")
    cs.set_custom_pin(ready, "1234")
    cs.link_badge("1234", "100234")
    print("[seed] Sample Person -> PIN 1234, card 100234 (ready to clock in)")

    ready2 = cs.register_employee("Test Employee")
    cs.set_custom_pin(ready2, "567890")
    cs.link_badge("567890", "100567")
    print("[seed] Test Employee -> PIN 567890, card 100567 (ready to clock in)")

    issued = cs.register_employee("New Hire")
    cs.link_badge(issued, "100999")
    print(f"[seed] New Hire -> temporary code {issued}, card 100999 "
          f"(will be asked to pick a PIN)")


SITE_LABEL = "Main Office"


def record_punch(pin, method):
    """Note the time and capture method for the dashboard's last-punch column."""
    last_punch[pin] = {"time": datetime.now().strftime("%-I:%M %p"), "method": method}


# What the kiosk says when clock_system refuses to flip the lever.
CLOCK_FAILURE_MESSAGES = {
    cs.DOES_NOT_EXIST: "That code isn't registered. Check it and try again.",
    cs.NEEDS_PIN_SETUP: "Finish setting up first: enter the code you were given on the keypad.",
}


def clock_response(pin, method):
    """Flip the lever via clock_system and shape the kiosk's reply (a dict)."""
    result = cs.process_clock_entry(pin)

    if result in (cs.CLOCKED_IN, cs.CLOCKED_OUT):
        record_punch(cs.clean_pin(pin), method)
        return {
            "ok": True,
            "name": cs.employee_records[cs.clean_pin(pin)]["name"],
            "action": result,
            "time": datetime.now().strftime("%-I:%M:%S %p"),
        }

    return {"ok": False, "message": CLOCK_FAILURE_MESSAGES.get(result, result)}


# =====================================================================
# ROUTES
# =====================================================================

@app.route("/")
def kiosk():
    return render_template_string(
        KIOSK_HTML,
        site=SITE_LABEL,
        min_len=cs.MIN_PIN_LENGTH,
        max_len=cs.MAX_PIN_LENGTH,
        google_stubbed=GOOGLE_IS_STUBBED,
    )


@app.route("/dashboard")
@app.route("/admin")
def dashboard():
    punches = load_punches()
    counts = {
        "on": sum(1 for p in punches if p["status"] == "in"),
        "out": sum(1 for p in punches if p["status"] == "out"),
        "pending": sum(1 for p in punches if p["temporary"]),
        "all": len(punches),
    }
    return render_template_string(
        DASHBOARD_HTML,
        site=SITE_LABEL,
        punches=punches,
        counts=counts,
        today=datetime.now().strftime("%A, %B %-d, %Y"),
        new_pin=request.args.get("pin"),
        new_name=request.args.get("name"),
        notice=request.args.get("notice"),
        notice_kind=request.args.get("kind", "ok"),
    )


@app.route("/register", methods=["POST"])
def register():
    """Employer registers a new employee; clock_system issues the code."""
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect(url_for("dashboard"))

    pin = cs.register_employee(name)
    log_event("register", {"name": name, "code": pin})
    return redirect(url_for("dashboard", pin=pin, name=name))


@app.route("/badge/link", methods=["POST"])
def badge_link():
    """Employer ties a school ID card to an employee (scan it into the box)."""
    pin = cs.clean_pin(request.form.get("pin"))
    badge = request.form.get("badge")

    result = cs.link_badge(pin, badge)
    log_event("badge_link", {"pin": pin, "result": result})

    if result == cs.BADGE_LINKED:
        name = cs.employee_records[pin]["name"]
        return redirect(url_for("dashboard", notice=f"ID card linked to {name}.", kind="ok"))

    messages = {
        cs.DOES_NOT_EXIST: "Pick an employee first.",
        cs.BADGE_BAD_FORMAT: "Nothing was scanned. Click the card box, then scan the card.",
        cs.BADGE_ALREADY_LINKED: "That card is already linked to someone else. Unlink it there first.",
    }
    return redirect(url_for("dashboard", notice=messages.get(result, result), kind="error"))


@app.route("/badge/unlink", methods=["POST"])
def badge_unlink():
    """Employer removes a card, e.g. when it's lost or replaced."""
    pin = cs.clean_pin(request.form.get("pin"))
    record = cs.lookup(pin)
    cs.unlink_badge(pin)
    log_event("badge_unlink", {"pin": pin})

    if record is None:
        return redirect(url_for("dashboard"))
    return redirect(url_for("dashboard", notice=f"ID card removed from {record['name']}.", kind="ok"))


# --- kiosk API -------------------------------------------------------

@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    """Step 1: a PIN was entered. Which popup should the kiosk show?"""
    pin = cs.clean_pin((request.get_json(silent=True) or {}).get("pin"))
    record = cs.lookup(pin)

    if record is None:
        log_event("lookup", {"pin": pin, "result": cs.DOES_NOT_EXIST})
        return jsonify({
            "state": "unknown",
            "message": "That code isn't registered. Check it and try again.",
        })

    if record["temporary"]:
        log_event("lookup", {"pin": pin, "result": cs.NEEDS_PIN_SETUP})
        return jsonify({"state": "new", "name": record["name"]})

    log_event("lookup", {"pin": pin, "result": "returning"})
    return jsonify({
        "state": "returning",
        "name": record["name"],
        "clocked_in": record["status"] == "clocked_in",
    })


@app.route("/api/clock", methods=["POST"])
def api_clock():
    """Step 2: they confirmed the name on the popup. Flip the lever."""
    pin = cs.clean_pin((request.get_json(silent=True) or {}).get("pin"))
    log_event("confirm", {"pin": pin})
    return jsonify(clock_response(pin, "PIN"))


@app.route("/api/set-pin", methods=["POST"])
def api_set_pin():
    """New employee replaces their issued code with a PIN of their own."""
    payload = request.get_json(silent=True) or {}
    current = cs.clean_pin(payload.get("current_pin"))
    chosen = cs.clean_pin(payload.get("new_pin"))

    result = cs.set_custom_pin(current, chosen)
    log_event("set_pin", {"from": current, "result": result})

    if result == cs.PIN_SET:
        return jsonify({"ok": True, "name": cs.employee_records[chosen]["name"]})

    messages = {
        cs.PIN_TAKEN: "That PIN is already taken. Choose a different one.",
        cs.PIN_BAD_FORMAT: f"Your PIN must be {cs.MIN_PIN_LENGTH}-{cs.MAX_PIN_LENGTH} digits.",
        cs.DOES_NOT_EXIST: "That code isn't registered. Start over.",
    }
    return jsonify({"ok": False, "message": messages.get(result, result)})


# --- Google sign-in (stubbed) ----------------------------------------

@app.route("/api/google/accounts", methods=["GET"])
def api_google_accounts():
    """The fake account picker's contents. Replace with real OAuth."""
    return jsonify({"accounts": google_accounts()})


@app.route("/api/google/signin", methods=["POST"])
def api_google_signin():
    """An account was picked. Is it already linked to an employee?"""
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip()
    pin = cs.find_by_google_email(email)

    if pin is None:
        log_event("google_signin", {"email": email, "result": "needs link"})
        return jsonify({"state": "needs_link", "email": email})

    record = cs.employee_records[pin]
    log_event("google_signin", {"email": email, "result": "linked"})
    return jsonify({
        "state": "linked",
        "email": email,
        "pin": pin,
        "name": record["name"],
        "clocked_in": record["status"] == "clocked_in",
    })


@app.route("/api/google/link", methods=["POST"])
def api_google_link():
    """The one-time link: PIN once, Google from then on."""
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip()
    pin = cs.clean_pin(payload.get("pin"))

    record = cs.lookup(pin)
    if record is not None and record["temporary"]:
        return jsonify({
            "ok": False,
            "message": "Set up your own PIN at the kiosk first, then link Google.",
        })

    result = cs.link_google_account(pin, email)
    log_event("google_link", {"email": email, "pin": pin, "result": result})

    if result == cs.GOOGLE_LINKED:
        return jsonify({"ok": True, "pin": pin, "name": cs.employee_records[pin]["name"]})

    messages = {
        cs.DOES_NOT_EXIST: "That PIN isn't registered. Check it and try again.",
        cs.GOOGLE_ALREADY_LINKED: "That Google account is already linked to someone else.",
    }
    return jsonify({"ok": False, "message": messages.get(result, result)})


@app.route("/api/google/clock", methods=["POST"])
def api_google_clock():
    """Confirmed from a Google sign-in. Flip the lever."""
    pin = cs.clean_pin((request.get_json(silent=True) or {}).get("pin"))
    log_event("confirm", {"pin": pin, "via": "google"})
    return jsonify(clock_response(pin, "Google"))


# --- ID card scan ----------------------------------------------------

@app.route("/api/badge/scan", methods=["POST"])
def api_badge_scan():
    """A card was scanned. Clock in or out straight away, no confirm step."""
    payload = request.get_json(silent=True) or {}
    badge = cs.normalize_badge(payload.get("badge"))
    source = payload.get("source", "scanner")   # "scanner" now; "camera" later

    pin = cs.find_by_badge(badge)
    if pin is None:
        log_event("badge_scan", {"badge": badge, "source": source, "result": "unknown badge"})
        return jsonify({
            "ok": False,
            "message": "This ID card isn't linked to anyone yet. See your site lead.",
        })

    # Same card again within the cooldown: show the earlier result instead
    # of clocking them straight back out.
    now = time.monotonic()
    previous = recent_scans.get(pin)
    if previous and now - previous[0] < SCAN_COOLDOWN_SECONDS:
        log_event("badge_scan", {"badge": badge, "source": source, "result": "repeat ignored"})
        return jsonify({**previous[1], "repeat": True})

    response = clock_response(pin, "Badge")
    log_event("badge_scan", {
        "badge": badge,
        "source": source,
        "result": response.get("action") or response.get("message"),
    })

    if response["ok"]:
        recent_scans[pin] = (now, response)
    return jsonify(response)


@app.route("/api/log", methods=["POST"])
def api_log():
    payload = request.get_json(silent=True) or {}
    log_event(payload.get("type", "unknown"), payload.get("detail"))
    return jsonify({"ok": True})


# =====================================================================
# SHARED STYLE
# =====================================================================

BASE_CSS = """
  :root {
    --bg: #0f1420;
    --card: #1a2130;
    --field: #131a27;
    --key-face: #2a3547;
    --key-press: #3a4760;
    --line: #333f55;
    --text: #eef2f8;
    --muted: #93a0b5;
    --accent: #4d8dff;
    --ok: #3ecf8e;
    --warn: #f5c451;
    --off: #7a879d;
    --err: #ff6b6b;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .sub { font-size: 14px; color: var(--muted); margin: 0; }
"""


# =====================================================================
# KIOSK PAGE — what the employee sees
# =====================================================================

KIOSK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Clock In / Out — Kiosk</title>
<style>
""" + BASE_CSS + """
  html, body { height: 100%; user-select: none; }
  body { overflow: auto; }

  .screen { height: 100%; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .card {
    width: min(560px, 100%);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 22px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }

  .head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  .site { font-size: 15px; color: var(--muted); letter-spacing: .04em; text-transform: uppercase; }
  .clock { font-size: 28px; font-variant-numeric: tabular-nums; font-weight: 600; }
  .title { font-size: 22px; font-weight: 600; margin: 0; }

  /* ---------- PIN field ---------- */
  .field {
    background: var(--field);
    border: 2px solid var(--accent);
    border-radius: 14px;
    padding: 12px 16px;
    min-height: 82px;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .field-label {
    font-size: 12px;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 6px;
  }
  /* The caret trails the entered digits: at the left edge of the box when
     nothing is typed, then advancing one place with every digit. */
  .field-value { display: flex; align-items: center; min-height: 34px; }
  .caret {
    flex: 0 0 auto;
    width: 2px;
    height: 30px;
    background: var(--accent);
    animation: blink 1.1s steps(1) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .dots { display: flex; gap: 12px; align-items: center; }
  /* space before the caret only once there's a digit to sit behind */
  .dots:not(:empty) { margin-right: 12px; }
  .dot { width: 16px; height: 16px; border-radius: 50%; background: var(--text); }

  /* ---------- keys ---------- */
  .numpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .key {
    min-height: 66px;
    border: 1px solid var(--line);
    background: var(--key-face);
    color: var(--text);
    border-radius: 12px;
    font-size: 26px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    transition: background .08s, transform .08s;
  }
  .key:active { background: var(--key-press); transform: translateY(1px); }
  .key.back { font-size: 24px; }
  .key.util { font-size: 17px; color: var(--muted); }

  .btn {
    min-height: 60px;
    border-radius: 12px;
    border: 1px solid var(--line);
    background: transparent;
    color: var(--muted);
    font-size: 17px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    padding: 0 18px;
  }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.ghost { color: var(--text); }
  .btn:disabled { opacity: .4; cursor: default; }
  .btn:active:not(:disabled) { transform: translateY(1px); }
  .actions { display: flex; gap: 10px; }
  .actions .btn { flex: 1; }
  .actions .btn.primary { flex: 2; }

  /* ---------- Google ---------- */
  .divider { display: flex; align-items: center; gap: 12px; color: var(--muted); font-size: 13px; }
  .divider::before, .divider::after { content: ""; flex: 1; height: 1px; background: var(--line); }

  .google-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    width: 100%;
    min-height: 58px;
    border-radius: 12px;
    border: 1px solid var(--line);
    background: #fff;
    color: #1f1f1f;
    font-size: 16px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
  }
  .google-btn:active { transform: translateY(1px); }
  .g-mark { font-size: 19px; font-weight: 700; }
  .g-note { text-align: center; font-size: 12px; color: var(--muted); }

  /* ---------- ID card scan hint ---------- */
  .scan-hint {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    border: 1px dashed var(--line);
    border-radius: 12px;
    color: var(--muted);
    font-size: 14px;
    transition: border-color .2s, color .2s, background-color .2s;
  }
  .scan-hint svg { flex: 0 0 auto; }
  /* lights up for a moment when a scan comes in */
  .scan-hint.hit {
    border-style: solid;
    border-color: var(--accent);
    color: var(--text);
    background-color: rgba(77,141,255,.1);
  }

  /* ---------- banner ---------- */
  .banner {
    border-radius: 12px;
    padding: 12px 16px;
    font-size: 16px;
    font-weight: 600;
    text-align: center;
    display: none;
  }
  .banner.show { display: block; }
  .banner.error { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.4); }

  .shake { animation: shake .3s; }
  @keyframes shake { 25% { transform: translateX(-7px); } 75% { transform: translateX(7px); } }

  /* ---------- popups ---------- */
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(7, 10, 17, .78);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
    z-index: 10;
  }
  .overlay.show { display: flex; }
  /* a `display` rule outranks the plain `hidden` attribute, so say it again */
  [hidden] { display: none !important; }
  .popup {
    width: min(480px, 100%);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 20px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    text-align: center;
    animation: rise .18s ease-out;
  }
  @keyframes rise { from { transform: translateY(12px); opacity: 0; } }
  .popup h2 { font-size: 20px; margin: 0; font-weight: 600; }
  .popup .who { font-size: 34px; font-weight: 700; line-height: 1.15; }
  .popup .state { font-size: 14px; color: var(--muted); }
  .popup .numpad { margin-top: 4px; }
  .popup .field { border-color: var(--line); }
  .popup .field-value { justify-content: center; }

  .avatar {
    width: 74px; height: 74px;
    border-radius: 50%;
    background: rgba(77,141,255,.14);
    border: 2px solid var(--accent);
    color: var(--accent);
    font-size: 30px;
    font-weight: 700;
    line-height: 70px;
    margin: 0 auto;
  }

  .account-list { display: flex; flex-direction: column; gap: 8px; }
  .account {
    display: flex;
    align-items: center;
    gap: 12px;
    text-align: left;
    background: var(--field);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 14px;
    color: var(--text);
    font-family: inherit;
    font-size: 15px;
    cursor: pointer;
  }
  .account:active { background: var(--key-press); }
  .account .avatar { width: 40px; height: 40px; font-size: 16px; line-height: 36px; margin: 0; }
  .account-name { font-weight: 600; }
  .account-email { font-size: 13px; color: var(--muted); }

  /* ---------- confirmation ---------- */
  .check {
    width: 92px; height: 92px; border-radius: 50%;
    background: rgba(62,207,142,.13);
    border: 2px solid var(--ok);
    color: var(--ok);
    font-size: 48px; line-height: 88px;
    margin: 0 auto;
  }
  #confirmPopup.out .check { background: rgba(77,141,255,.13); border-color: var(--accent); color: var(--accent); }
  #confirmPopup.out .confirm-action { color: var(--accent); }
  .confirm-action {
    font-size: 19px;
    font-weight: 700;
    letter-spacing: .06em;
    text-transform: uppercase;
    color: var(--ok);
  }
  .confirm-time { font-size: 19px; color: var(--muted); font-variant-numeric: tabular-nums; }

  /* ---------- fit the screen ---------- */
  .screen { min-height: 100%; height: auto; padding: clamp(10px, 2.5vh, 20px); }
  .card { gap: clamp(8px, 1.8vh, 18px); padding: clamp(14px, 2.6vh, 24px); }
  .col-info, .col-pad, .col-alt { display: flex; flex-direction: column; gap: inherit; }
  .key { min-height: clamp(44px, 7.5vh, 66px); }
  .btn { min-height: clamp(44px, 6.5vh, 60px); }
  .field { min-height: clamp(56px, 8.5vh, 82px); }
  .google-btn { min-height: clamp(44px, 6vh, 58px); }
  .admin-link { align-self: center; font-size: 12px; color: var(--muted); text-decoration: none; opacity: .7; }
  .admin-link:hover { opacity: 1; text-decoration: underline; }

  /* Landscape (laptop, tablet on its side): info + Google on the left,
     PIN pad on the right, so nothing has to stack past the screen edge. */
  @media (orientation: landscape) and (min-width: 820px) {
    .card {
      width: min(980px, 100%);
      display: grid;
      grid-template-columns: 1fr 1.1fr;
      grid-template-areas: "info pad" "alt pad";
      grid-template-rows: auto 1fr;
      column-gap: clamp(20px, 3vw, 36px);
    }
    .col-info { grid-area: info; }
    .col-pad  { grid-area: pad; justify-content: center; }
    .col-alt  { grid-area: alt; justify-content: flex-end; }
    .key { min-height: clamp(44px, 11vh, 76px); }
  }

  @media (max-height: 680px) {
    .key { min-height: 52px; font-size: 22px; }
    .field { min-height: 68px; }
    .google-btn { min-height: 50px; }
  }
</style>
</head>
<body>

<div class="screen">
  <div class="card" id="entry">
   <div class="col-info">
    <div class="head">
      <span class="site">{{ site }}</span>
      <span class="clock" id="clock">--:--</span>
    </div>

    <div>
      <h1 class="title">Clock In / Out</h1>
      <p class="sub">Enter your {{ min_len }}-{{ max_len }} digit PIN, or scan your ID card. The same PIN or card clocks you out.</p>
    </div>

    <div class="scan-hint" id="scanHint">
      <svg width="34" height="22" viewBox="0 0 34 22" aria-hidden="true">
        <g fill="currentColor">
          <rect x="0" y="0" width="2" height="22"/><rect x="4" y="0" width="1" height="22"/>
          <rect x="7" y="0" width="3" height="22"/><rect x="12" y="0" width="1" height="22"/>
          <rect x="15" y="0" width="2" height="22"/><rect x="19" y="0" width="1" height="22"/>
          <rect x="22" y="0" width="3" height="22"/><rect x="27" y="0" width="1" height="22"/>
          <rect x="30" y="0" width="2" height="22"/><rect x="33" y="0" width="1" height="22"/>
        </g>
      </svg>
      <span id="scanHintText">Have your school ID? Just scan it, any time.</span>
    </div>

    <div class="banner" id="banner"></div>
   </div>

   <div class="col-pad">
    <div class="field">
      <div class="field-label">PIN</div>
      <div class="field-value">
        <span class="dots" id="pinDots"></span>
        <span class="caret"></span>
      </div>
    </div>

    <div class="numpad" id="numpad"></div>

    <div class="actions">
      <button class="btn" id="btnClear">Clear</button>
      <button class="btn primary" id="btnEnter" disabled>Enter</button>
    </div>
   </div>

   <div class="col-alt">
    <div class="divider">or</div>

    <button class="google-btn" id="btnGoogle">
      <span class="g-mark">G</span> Sign in with Google
    </button>
    {% if google_stubbed %}
    <p class="g-note">Demo account picker — real Google OAuth not wired up yet.</p>
    {% endif %}
    <a class="admin-link" href="/admin">Admin</a>
   </div>
  </div>
</div>

<!-- ============ POPUPS ============ -->
<div class="overlay" id="overlay">

  <!-- returning employee: is this you? -->
  <div class="popup" id="confirmWho" hidden>
    <div class="avatar" id="whoInitials">—</div>
    <h2>Is this you?</h2>
    <div class="who" id="whoName">—</div>
    <div class="state" id="whoState">—</div>
    <div class="actions">
      <button class="btn ghost" id="whoNo">No, try again</button>
      <button class="btn primary" id="whoYes">Yes, that's me</button>
    </div>
  </div>

  <!-- new employee: choose your own PIN -->
  <div class="popup" id="setupPin" hidden>
    <h2>Welcome, <span id="setupName">—</span></h2>
    <p class="sub">Choose your own PIN. {{ min_len }}-{{ max_len }} digits. You'll use it from now on.</p>
    <div class="banner" id="setupBanner"></div>
    <div class="field">
      <div class="field-label" id="setupLabel">New PIN</div>
      <div class="field-value">
        <span class="dots" id="setupDots"></span>
        <span class="caret"></span>
      </div>
    </div>
    <div class="numpad" id="setupNumpad"></div>
    <div class="actions">
      <button class="btn ghost" id="setupCancel">Cancel</button>
      <button class="btn primary" id="setupSave" disabled>Save PIN</button>
    </div>
  </div>

  <!-- google: pick an account (stub) -->
  <div class="popup" id="googlePicker" hidden>
    <h2>Choose an account</h2>
    <p class="sub">Demo picker standing in for Google sign-in.</p>
    <div class="account-list" id="accountList"></div>
    <div class="actions">
      <button class="btn ghost" id="googleCancel">Cancel</button>
    </div>
  </div>

  <!-- google: one-time link -->
  <div class="popup" id="googleLink" hidden>
    <h2>Link this account</h2>
    <p class="sub">Enter your PIN once for <span id="linkEmail">—</span>. After this, Google alone signs you in.</p>
    <div class="banner" id="linkBanner"></div>
    <div class="field">
      <div class="field-label">PIN</div>
      <div class="field-value">
        <span class="dots" id="linkDots"></span>
        <span class="caret"></span>
      </div>
    </div>
    <div class="numpad" id="linkNumpad"></div>
    <div class="actions">
      <button class="btn ghost" id="linkCancel">Cancel</button>
      <button class="btn primary" id="linkSave" disabled>Link account</button>
    </div>
  </div>

  <!-- clocked in / out -->
  <div class="popup" id="confirmPopup" hidden>
    <div class="check" id="confirmIcon">&#10003;</div>
    <div class="who" id="confirmName">—</div>
    <div class="confirm-action" id="confirmAction">—</div>
    <div class="confirm-time" id="confirmTime">—</div>
    <p class="sub" id="confirmNote">Have a good shift.</p>
  </div>

</div>

<script>
const MIN_LEN = {{ min_len }};
const MAX_LEN = {{ max_len }};
const RESET_AFTER_MS = 4000;

const $ = id => document.getElementById(id);

const el = {
  entry: $("entry"), banner: $("banner"), overlay: $("overlay"), scanHint: $("scanHint"),
  pinDots: $("pinDots"), numpad: $("numpad"),
  btnEnter: $("btnEnter"), btnClear: $("btnClear"), btnGoogle: $("btnGoogle"),
  clock: $("clock"),

  confirmWho: $("confirmWho"), whoName: $("whoName"), whoState: $("whoState"),
  whoInitials: $("whoInitials"), whoYes: $("whoYes"), whoNo: $("whoNo"),

  setupPin: $("setupPin"), setupName: $("setupName"), setupDots: $("setupDots"),
  setupNumpad: $("setupNumpad"), setupBanner: $("setupBanner"),
  setupLabel: $("setupLabel"), setupSave: $("setupSave"), setupCancel: $("setupCancel"),

  googlePicker: $("googlePicker"), accountList: $("accountList"), googleCancel: $("googleCancel"),

  googleLink: $("googleLink"), linkEmail: $("linkEmail"), linkDots: $("linkDots"),
  linkNumpad: $("linkNumpad"), linkBanner: $("linkBanner"),
  linkSave: $("linkSave"), linkCancel: $("linkCancel"),

  confirmPopup: $("confirmPopup"), confirmName: $("confirmName"),
  confirmAction: $("confirmAction"), confirmTime: $("confirmTime"),
  confirmIcon: $("confirmIcon"), confirmNote: $("confirmNote")
};

// pin        - what's typed on the main screen
// setupValue - the new PIN a first-timer is choosing
// linkValue  - the PIN typed to link a Google account
// pendingPin - whose record the open popup is about
const state = { pin: "", setupValue: "", linkValue: "", pendingPin: null, pendingEmail: null };

function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  }).then(r => r.json());
}

function logEvent(type, detail) {
  post("/api/log", { type, detail }).catch(() => {});
}

/* ---------- shared numpad builder ---------- */

function buildNumpad(container, onDigit, onBack) {
  container.innerHTML = "";
  const add = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = "key" + (cls ? " " + cls : "");
    b.textContent = label;
    b.addEventListener("click", fn);
    container.appendChild(b);
  };
  ["1","2","3","4","5","6","7","8","9"].forEach(n => add(n, "", () => onDigit(n)));
  add("", "util", () => {});                 // spacer keeps 0 centred
  add("0", "", () => onDigit("0"));
  add("\\u232B", "back", onBack);
}

function renderDots(container, value) {
  container.innerHTML = "";
  for (let i = 0; i < value.length; i++) {
    const d = document.createElement("span");
    d.className = "dot";
    container.appendChild(d);
  }
}

/* ---------- main PIN entry ---------- */

function renderPin() {
  renderDots(el.pinDots, state.pin);
  el.btnEnter.disabled = state.pin.length < MIN_LEN;
}

function pressPin(digit) {
  if (state.pin.length >= MAX_LEN) return;
  state.pin += digit;
  logEvent("keypress", { field: "pin", length: state.pin.length });
  hideBanner(el.banner, el.entry);
  renderPin();
}

function backspacePin() {
  state.pin = state.pin.slice(0, -1);
  logEvent("backspace", { field: "pin" });
  hideBanner(el.banner, el.entry);
  renderPin();
}

function clearPin() {
  state.pin = "";
  hideBanner(el.banner, el.entry);
  logEvent("clear");
  renderPin();
}

/* ---------- banners ---------- */

function showBanner(bannerEl, shakeEl, message) {
  bannerEl.textContent = message;
  bannerEl.className = "banner show error";
  shakeEl.classList.remove("shake");
  void shakeEl.offsetWidth;                  // restart the animation
  shakeEl.classList.add("shake");
}

function hideBanner(bannerEl) {
  bannerEl.className = "banner";
}

/* ---------- popup plumbing ---------- */

const POPUPS = ["confirmWho", "setupPin", "googlePicker", "googleLink", "confirmPopup"];

function openPopup(name) {
  POPUPS.forEach(id => { el[id].hidden = id !== name; });
  el.overlay.classList.add("show");
}

function closePopup() {
  el.overlay.classList.remove("show");
  POPUPS.forEach(id => { el[id].hidden = true; });
}

function initials(name) {
  return (name || "")
    .split(/\\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(part => part[0].toUpperCase())
    .join("");
}

/* ---------- step 1: look the PIN up ---------- */

async function submitPin() {
  if (state.pin.length < MIN_LEN) return;

  let data;
  try {
    data = await post("/api/lookup", { pin: state.pin });
  } catch (err) {
    showBanner(el.banner, el.entry, "Kiosk is offline. Tell your site lead.");
    return;
  }

  if (data.state === "unknown") {
    showBanner(el.banner, el.entry, data.message);
    state.pin = "";
    renderPin();
    return;
  }

  state.pendingPin = state.pin;

  if (data.state === "new") {
    // First time in: they were issued this code, now they pick their own.
    state.setupValue = "";
    el.setupName.textContent = data.name;
    hideBanner(el.setupBanner);
    renderSetup();
    openPopup("setupPin");
    return;
  }

  showWhoPopup(data.name, data.clocked_in);
}

/* ---------- step 2: "is this you?" ---------- */

function showWhoPopup(name, clockedIn) {
  el.whoName.textContent = name;
  el.whoInitials.textContent = initials(name);
  el.whoState.textContent = clockedIn
    ? "Currently on shift — confirming clocks you out."
    : "Currently clocked out — confirming clocks you in.";
  openPopup("confirmWho");
}

async function confirmWho() {
  const viaGoogle = state.pendingEmail !== null;
  const url = viaGoogle ? "/api/google/clock" : "/api/clock";
  const data = await post(url, { pin: state.pendingPin });

  if (!data.ok) {
    closePopup();
    showBanner(el.banner, el.entry, data.message);
    clearPin();
    return;
  }
  showConfirmation(data);
}

function rejectWho() {
  // "No, try again" — wipe everything and go back to the numpad.
  logEvent("rejected_name", { pin: state.pendingPin });
  closePopup();
  state.pendingPin = null;
  state.pendingEmail = null;
  clearPin();
}

/* ---------- step 2b: first-time PIN setup ---------- */

function renderSetup() {
  renderDots(el.setupDots, state.setupValue);
  el.setupSave.disabled = state.setupValue.length < MIN_LEN;
}

async function saveNewPin() {
  const data = await post("/api/set-pin", {
    current_pin: state.pendingPin,
    new_pin: state.setupValue
  });

  if (!data.ok) {
    // Conflicting or badly shaped PIN: show the error, let them pick again.
    showBanner(el.setupBanner, el.setupPin, data.message);
    state.setupValue = "";
    renderSetup();
    return;
  }

  // PIN is theirs now — carry straight on into clocking in.
  state.pendingPin = state.setupValue;
  state.pendingEmail = null;
  showWhoPopup(data.name, false);
}

/* ---------- google ---------- */

async function openGoogle() {
  logEvent("google_tapped");
  const data = await (await fetch("/api/google/accounts")).json();

  el.accountList.innerHTML = "";
  data.accounts.forEach(account => {
    const b = document.createElement("button");
    b.className = "account";

    const av = document.createElement("span");
    av.className = "avatar";
    av.textContent = initials(account.name);

    const box = document.createElement("span");
    const n = document.createElement("div");
    n.className = "account-name";
    n.textContent = account.name;
    const e = document.createElement("div");
    e.className = "account-email";
    e.textContent = account.email;
    box.append(n, e);

    b.append(av, box);
    b.addEventListener("click", () => pickAccount(account.email));
    el.accountList.appendChild(b);
  });

  openPopup("googlePicker");
}

async function pickAccount(email) {
  const data = await post("/api/google/signin", { email });

  if (data.state === "needs_link") {
    // One-time link: prove who you are with the PIN, once.
    state.pendingEmail = email;
    state.linkValue = "";
    el.linkEmail.textContent = email;
    hideBanner(el.linkBanner);
    renderLink();
    openPopup("googleLink");
    return;
  }

  state.pendingPin = data.pin;
  state.pendingEmail = email;
  showWhoPopup(data.name, data.clocked_in);
}

function renderLink() {
  renderDots(el.linkDots, state.linkValue);
  el.linkSave.disabled = state.linkValue.length < MIN_LEN;
}

async function saveLink() {
  const data = await post("/api/google/link", {
    email: state.pendingEmail,
    pin: state.linkValue
  });

  if (!data.ok) {
    showBanner(el.linkBanner, el.googleLink, data.message);
    state.linkValue = "";
    renderLink();
    return;
  }

  state.pendingPin = data.pin;
  showWhoPopup(data.name, false);
}

/* ---------- confirmation ---------- */

// One timer for the whole kiosk: when the next person in line scans while
// this screen is still up, their result replaces it and gets the full time.
let resetTimer = null;

function showConfirmation(data) {
  const clockedOut = data.action === "clocked out";

  el.confirmName.textContent = data.name;
  el.confirmAction.textContent = data.action;
  el.confirmTime.textContent = data.time;
  el.confirmIcon.innerHTML = clockedOut ? "&#8594;" : "&#10003;";
  if (data.repeat) {
    el.confirmNote.textContent = "Already recorded a moment ago. Nothing changed.";
  } else {
    el.confirmNote.textContent = clockedOut
      ? "You're clocked out. See you next shift."
      : "You're clocked in. Have a good shift.";
  }
  el.confirmPopup.classList.toggle("out", clockedOut);

  hideBanner(el.banner);
  openPopup("confirmPopup");

  clearTimeout(resetTimer);
  resetTimer = setTimeout(() => {
    closePopup();
    state.pendingPin = null;
    state.pendingEmail = null;
    clearPin();
  }, RESET_AFTER_MS);
}

/* ---------- ID card scans ---------- */

// Every scan, from any source, comes through here. A camera scanner added
// later just calls handleScan(code, "camera") with what it decoded.
async function handleScan(code, source) {
  // A PIN setup, Google link or "is this you?" popup belongs to whoever is
  // using the kiosk right now; don't clock someone else in underneath it.
  // The result screen is fine to replace: that's just the next in line.
  if (el.overlay.classList.contains("show") && el.confirmPopup.hidden) {
    logEvent("scan_ignored", { source, reason: "popup open" });
    return;
  }

  flashScanHint();

  let data;
  try {
    data = await post("/api/badge/scan", { badge: code, source });
  } catch (err) {
    showBanner(el.banner, el.entry, "Kiosk is offline. Tell your site lead.");
    return;
  }

  // Any half-typed PIN belonged to no one; the card decides who this is.
  state.pin = "";
  renderPin();

  if (!data.ok) {
    clearTimeout(resetTimer);
    closePopup();
    showBanner(el.banner, el.entry, data.message);
    return;
  }

  showConfirmation(data);
}

function flashScanHint() {
  el.scanHint.classList.add("hit");
  setTimeout(() => el.scanHint.classList.remove("hit"), 700);
}

// For trying the scan flow with no scanner attached: open the browser's
// developer console on the kiosk page and run  simulateScan("100234")
window.simulateScan = code => handleScan(String(code), "simulated");

/* ---------- clock ---------- */

function tickClock() {
  el.clock.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/* ---------- wire up ---------- */

buildNumpad(el.numpad, pressPin, backspacePin);

buildNumpad(el.setupNumpad,
  d => {
    if (state.setupValue.length >= MAX_LEN) return;
    state.setupValue += d;
    hideBanner(el.setupBanner);
    renderSetup();
  },
  () => { state.setupValue = state.setupValue.slice(0, -1); renderSetup(); }
);

buildNumpad(el.linkNumpad,
  d => {
    if (state.linkValue.length >= MAX_LEN) return;
    state.linkValue += d;
    hideBanner(el.linkBanner);
    renderLink();
  },
  () => { state.linkValue = state.linkValue.slice(0, -1); renderLink(); }
);

el.btnEnter.addEventListener("click", submitPin);
el.btnClear.addEventListener("click", clearPin);
el.btnGoogle.addEventListener("click", openGoogle);

el.whoYes.addEventListener("click", confirmWho);
el.whoNo.addEventListener("click", rejectWho);

el.setupSave.addEventListener("click", saveNewPin);
el.setupCancel.addEventListener("click", () => { closePopup(); clearPin(); });

el.googleCancel.addEventListener("click", () => { closePopup(); state.pendingEmail = null; });
el.linkCancel.addEventListener("click", () => { closePopup(); state.pendingEmail = null; clearPin(); });

/* ---------- barcode scanner + physical keyboard ---------- */

// A USB/Bluetooth scanner is a keyboard that types the barcode very fast,
// then presses Enter (some are set to press Tab). Keys are held back for a
// moment before being treated as typing: if Enter arrives while they're
// still pouring in, the whole burst was a scan.
const SCAN_MAX_GAP_MS = 80;     // scanners: ~5-30ms per character; people: 100ms+
const SCAN_MIN_LENGTH = 4;      // anything shorter is treated as typing
const SCAN_TERMINATORS = ["Enter", "Tab"];

let keyBuffer = [];
let flushTimer = null;

// Typing a PIN on a physical keyboard is a development convenience only;
// on the tablet people use the on-screen numpad.
function applyTypedKey(key) {
  if (el.overlay.classList.contains("show")) return;
  if (key === "Backspace") backspacePin();
  else if (key === "Enter") submitPin();
  else if (/^[0-9]$/.test(key)) pressPin(key);
}

function flushAsTyping() {
  const keys = keyBuffer;
  keyBuffer = [];
  flushTimer = null;
  keys.forEach(applyTypedKey);
}

window.addEventListener("keydown", e => {
  const isChar = e.key.length === 1;
  const isTerminator = SCAN_TERMINATORS.includes(e.key);
  if (e.repeat) return;                                            // a held-down key, never a scanner
  if (!isChar && !isTerminator && e.key !== "Backspace") return;   // Shift and friends
  e.preventDefault();
  clearTimeout(flushTimer);

  const looksLikeScan = isTerminator
    && keyBuffer.length >= SCAN_MIN_LENGTH
    && keyBuffer.every(k => k.length === 1);

  if (looksLikeScan) {
    const code = keyBuffer.join("");
    keyBuffer = [];
    handleScan(code, "scanner");
    return;
  }

  if (e.key === "Tab") {           // a Tab on its own means nothing here
    flushAsTyping();
    return;
  }

  keyBuffer.push(e.key);
  flushTimer = setTimeout(flushAsTyping, SCAN_MAX_GAP_MS);
});

renderPin();
tickClock();
setInterval(tickClock, 1000);
</script>
</body>
</html>
"""


# =====================================================================
# DASHBOARD PAGE — what the employer sees
# =====================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clock-In Dashboard</title>
<style>
""" + BASE_CSS + """
  body { padding: 24px; }
  .wrap { max-width: 1100px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }

  .head { display: flex; align-items: flex-end; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  h1 { font-size: 24px; margin: 0 0 4px; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; }
  .tile-label { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
  .tile-value { font-size: 32px; font-weight: 700; margin-top: 6px; font-variant-numeric: tabular-nums; }
  .tile.on .tile-value { color: var(--ok); }
  .tile.pending .tile-value { color: var(--warn); }

  .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  input[type="search"], input[type="text"], select {
    background: var(--field);
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: 10px;
    padding: 11px 14px;
    font-size: 15px;
    font-family: inherit;
  }
  input[type="search"] { flex: 1; min-width: 200px; }
  input:focus, select:focus { outline: none; border-color: var(--accent); }

  .register { display: flex; gap: 8px; margin-left: auto; }
  .register button {
    background: var(--accent);
    border: 1px solid var(--accent);
    color: #fff;
    border-radius: 10px;
    padding: 11px 16px;
    font-size: 15px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    white-space: nowrap;
  }

  /* the freshly issued code, shown once after registering */
  .pin-callout {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    background: rgba(77,141,255,.1);
    border: 1px solid rgba(77,141,255,.45);
    border-radius: 14px;
    padding: 16px 20px;
  }
  .pin-callout-label { font-size: 15px; font-weight: 600; margin-bottom: 2px; }
  .pin-callout-pin {
    font-size: 40px;
    font-weight: 700;
    letter-spacing: .18em;
    color: var(--accent);
    font-variant-numeric: tabular-nums;
  }

  .panel { background: var(--card); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 14px 18px; font-size: 15px; }
  th {
    font-size: 12px;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
    background: #151c29;
    border-bottom: 1px solid var(--line);
    font-weight: 600;
  }
  tbody tr { border-bottom: 1px solid #242e3f; }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: #1d2534; }
  .name { font-weight: 600; }
  .mono { font-variant-numeric: tabular-nums; color: var(--muted); }

  .pill {
    display: inline-block;
    padding: 4px 11px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .03em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .pill.in { background: rgba(62,207,142,.14); color: var(--ok); }
  .pill.out { background: rgba(122,135,157,.16); color: var(--off); }
  .pill.pending { background: rgba(245,196,81,.15); color: var(--warn); }

  .tag {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 3px 8px;
  }
  .google-cell { font-size: 13px; color: var(--muted); }
  .empty-row { text-align: center; color: var(--muted); padding: 34px; font-size: 15px; }
  tr.hidden { display: none; }

  /* one-line result after linking or unlinking a card */
  .notice { border-radius: 12px; padding: 12px 16px; font-size: 15px; font-weight: 600; }
  .notice.ok { background: rgba(62,207,142,.12); color: var(--ok); border: 1px solid rgba(62,207,142,.4); }
  .notice.error { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.4); }

  /* ---------- link a school ID card ---------- */
  .card-link {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 14px 16px;
  }
  .card-link-text { flex: 1 1 260px; }
  .card-link-title { font-size: 15px; font-weight: 600; margin-bottom: 2px; }
  .card-link input[type="text"] { width: 190px; }
  .card-link button {
    background: var(--accent);
    border: 1px solid var(--accent);
    color: #fff;
    border-radius: 10px;
    padding: 11px 16px;
    font-size: 15px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    white-space: nowrap;
  }

  .badge-cell { white-space: nowrap; }
  .inline { display: inline; }
  .unlink {
    background: none;
    border: 1px solid var(--line);
    color: var(--muted);
    border-radius: 6px;
    padding: 3px 8px;
    margin-left: 8px;
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
  }
  .unlink:hover { color: var(--err); border-color: rgba(255,107,107,.5); }

  @media (max-width: 760px) {
    body { padding: 14px; }
    /* hide the Google and ID card columns */
    th:nth-child(5), td:nth-child(5), th:nth-child(6), td:nth-child(6) { display: none; }
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="head">
    <div>
      <h1>Clock-In Dashboard</h1>
      <p class="sub">{{ site }}</p>
    </div>
    <p class="sub">{{ today }}</p>
  </div>

  {% if notice %}
  <div class="notice {{ 'error' if notice_kind == 'error' else 'ok' }}">{{ notice }}</div>
  {% endif %}

  {% if new_pin %}
  <div class="pin-callout">
    <div>
      <div class="pin-callout-label">Temporary code for {{ new_name }}</div>
      <p class="sub">Give it to them now. They'll choose their own PIN at the kiosk.</p>
    </div>
    <div class="pin-callout-pin">{{ new_pin }}</div>
  </div>
  {% endif %}

  <div class="tiles">
    <div class="tile on">     <div class="tile-label">On shift</div>     <div class="tile-value">{{ counts.on }}</div></div>
    <div class="tile">        <div class="tile-label">Clocked out</div>  <div class="tile-value">{{ counts.out }}</div></div>
    <div class="tile pending"><div class="tile-label">PIN not set</div>  <div class="tile-value">{{ counts.pending }}</div></div>
    <div class="tile">        <div class="tile-label">Total staff</div>  <div class="tile-value">{{ counts.all }}</div></div>
  </div>

  <div class="controls">
    <input type="search" id="search" placeholder="Search by name, PIN or card…" autocomplete="off">
    <select id="statusFilter">
      <option value="all">All statuses</option>
      <option value="in">On shift</option>
      <option value="out">Clocked out</option>
      <option value="pending">PIN not set</option>
    </select>
    <form class="register" method="post" action="/register">
      <input type="text" name="name" placeholder="New employee name" autocomplete="off" required>
      <button type="submit">Register &amp; issue code</button>
    </form>
  </div>

  {% if punches %}
  <form class="card-link" method="post" action="/badge/link">
    <div class="card-link-text">
      <div class="card-link-title">Link a school ID card</div>
      <p class="sub">Pick the employee, then scan their card. The scanner presses Enter for you.</p>
    </div>
    <select name="pin" id="linkPin" required>
      <option value="" disabled selected>Employee…</option>
      {% for p in punches %}
      <option value="{{ p.id }}">{{ p.name }}{% if p.badge %} (replace card){% endif %}</option>
      {% endfor %}
    </select>
    <input type="text" name="badge" id="linkBadge" placeholder="Scan card here" autocomplete="off" required>
    <button type="submit">Link card</button>
  </form>
  {% endif %}

  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>Employee</th><th>PIN</th><th>Status</th><th>Last punch</th><th>Google</th><th>ID card</th><th>Method</th>
        </tr>
      </thead>
      <tbody id="rows">
        {% for p in punches %}
        <tr data-name="{{ p.name|lower }}" data-id="{{ p.id }}" data-badge="{{ p.badge|lower }}"
            data-status="{{ 'pending' if p.temporary else p.status }}">
          <td class="name">{{ p.name }}</td>
          <td class="mono">{{ p.id }}</td>
          <td>
            {% if p.temporary %}
              <span class="pill pending">PIN not set</span>
            {% elif p.status == 'in' %}
              <span class="pill in">On shift</span>
            {% else %}
              <span class="pill out">Clocked out</span>
            {% endif %}
          </td>
          <td class="mono">{{ p.time }}</td>
          <td class="google-cell">{{ p.google or '—' }}</td>
          <td class="badge-cell">
            {% if p.badge %}
              <span class="mono">{{ p.badge }}</span>
              <form class="inline" method="post" action="/badge/unlink">
                <input type="hidden" name="pin" value="{{ p.id }}">
                <button class="unlink" type="submit" title="Remove this card, e.g. if it's lost">Unlink</button>
              </form>
            {% else %}
              <span class="mono">—</span>
            {% endif %}
          </td>
          <td><span class="tag">{{ p.method }}</span></td>
        </tr>
        {% endfor %}
        <tr id="emptyRow" class="{% if punches %}hidden{% endif %}">
          <td class="empty-row" colspan="7">
            {% if punches %}No employees match that search.
            {% else %}Nobody registered yet — add someone above.{% endif %}
          </td>
        </tr>
      </tbody>
    </table>
  </div>

</div>

<script>
const searchEl = document.getElementById("search");
const filterEl = document.getElementById("statusFilter");
const emptyRow = document.getElementById("emptyRow");
const dataRows = [...document.querySelectorAll("#rows tr[data-name]")];

function applyFilters() {
  const q = searchEl.value.trim().toLowerCase();
  const status = filterEl.value;
  let shown = 0;

  dataRows.forEach(row => {
    const matchesText = !q
      || row.dataset.name.includes(q)
      || row.dataset.id.includes(q)
      || row.dataset.badge.includes(q);
    const matchesStatus = status === "all" || row.dataset.status === status;
    const visible = matchesText && matchesStatus;

    row.classList.toggle("hidden", !visible);
    if (visible) shown++;
  });

  emptyRow.classList.toggle("hidden", shown > 0);
}

searchEl.addEventListener("input", applyFilters);
filterEl.addEventListener("change", applyFilters);

// Picking an employee leaves focus on the dropdown, where a scanner's
// keystrokes would type-ahead and jump the selection to another name.
// Move straight to the card box so the scan lands there.
const linkPin = document.getElementById("linkPin");
const linkBadge = document.getElementById("linkBadge");
if (linkPin) linkPin.addEventListener("change", () => linkBadge.focus());
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # debug=True reloads the process, which would re-seed and hand out a
    # second set of codes, so only seed in the real (reloaded) worker.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        seed_demo_staff()

    # host="0.0.0.0" so the Android tablet on the same network can reach it.
    app.run(host="0.0.0.0", port=5000, debug=True)
