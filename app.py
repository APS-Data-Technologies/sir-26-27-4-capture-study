"""
Clock-in kiosk prototype — UI layer.

Two screens:
    /           employee kiosk (PIN entry, ID card scan)
    /dashboard  employer view (summary tiles, staff table, registration,
                linking school ID cards, digital ID cards via digitalID.py,
                spreadsheet import + edit employee via staffadmin.py)

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
    Or they scan their school ID card: that clocks them in or out at once.
    Or they tap the NFC wall tag with their own phone (nfctag.py).

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

import json
import os
import threading
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
import nfctag
import digitalID
import staffadmin

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


# clock_system tracks WHO is in or out, but not WHEN. The dashboard wants
# a time per person, so the kiosk records one here as punches come in.
# Keyed by PIN -> {"time": "7:52 AM", "method": "PIN"}.
# The instrumentation sector can replace this with real punch history.
last_punch = {}


# Double-clock guard, shared by EVERY way of clocking (PIN, ID card, NFC).
# A double beep, a nervous second tap, or a card scan followed by an NFC tap
# would otherwise clock someone in and straight back out. Within this window
# any repeat for the same person just shows the latest result again.
CLOCK_COOLDOWN_SECONDS = 10

# PIN -> (time.monotonic() of their last clock, the response it got)
recent_clocks = {}

# Flask serves requests on several threads at once, so two requests for the
# same person (a phone that opens the tag link twice) could otherwise both
# pass the guard before either one flips the lever.
clock_lock = threading.Lock()


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
    if cs.pins_assigned():
        print(f"[seed] New Hire -> assigned PIN {issued}, card 100999")
    else:
        print(f"[seed] New Hire -> temporary code {issued}, card 100999 "
              f"(will be asked to pick a PIN)")


SITE_LABEL = "Main Office"


# ---------------------------------------------------------------------
# Saved settings (currently just the PIN policy), kept next to the NFC
# tag's code so Docker's data volume keeps both across restarts.
# ---------------------------------------------------------------------
SETTINGS_FILE = os.path.join(
    os.environ.get("NFC_DATA_DIR") or os.path.dirname(os.path.abspath(__file__)),
    "kiosk_settings.json",
)


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            mode = json.load(f).get("pin_mode", cs.PIN_MODE_CHOOSE)
    except (FileNotFoundError, ValueError):
        return
    if mode in (cs.PIN_MODE_CHOOSE, cs.PIN_MODE_ASSIGNED):
        cs.set_pin_mode(mode)


def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"pin_mode": cs.pin_mode}, f)


load_settings()


def record_punch(pin, method):
    """Note the time and capture method for the dashboard's last-punch column."""
    last_punch[pin] = {"time": datetime.now().strftime("%I:%M %p").lstrip("0"), "method": method}


# What the kiosk says when clock_system refuses to flip the lever.
CLOCK_FAILURE_MESSAGES = {
    cs.DOES_NOT_EXIST: "That code isn't registered. Check it and try again.",
    cs.NEEDS_PIN_SETUP: "Finish setting up first: enter the code you were given on the keypad.",
}


def clock_response(pin, method):
    """Flip the lever via clock_system and shape the reply (a dict).

    Every clock-in/out goes through here, whatever the method, so the
    double-clock guard sees them all. A repeat inside the cooldown returns
    the latest result with "repeat": True instead of flipping again.
    """
    pin = cs.clean_pin(pin)
    with clock_lock:
        now = time.monotonic()
        previous = recent_clocks.get(pin)
        if previous and now - previous[0] < CLOCK_COOLDOWN_SECONDS:
            return {**previous[1], "repeat": True}

        result = cs.process_clock_entry(pin)
        if result not in (cs.CLOCKED_IN, cs.CLOCKED_OUT):
            return {"ok": False, "message": CLOCK_FAILURE_MESSAGES.get(result, result)}

        record_punch(pin, method)
        response = {
            "ok": True,
            "name": cs.employee_records[pin]["name"],
            "action": result,
            "time": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
        }
        recent_clocks[pin] = (now, response)
        return response


def clock_outcome(response):
    """One word for the event log: what happened to this clock attempt."""
    if response.get("repeat"):
        return "repeat ignored"
    return response.get("action") or response.get("message")


def move_pin_state(old_pin, new_pin):
    """Someone's PIN changed (they chose one, or an admin changed or reset
    it). Move everything this app keeps per PIN, so their last punch,
    double-clock guard and remembered phones follow them."""
    with clock_lock:
        for store in (last_punch, recent_clocks):
            if old_pin in store:
                store[new_pin] = store.pop(old_pin)
    nfctag.move_devices(old_pin, new_pin)


def status_corrected(pin):
    """An admin fixed someone's clock status by hand."""
    with clock_lock:
        recent_clocks.pop(pin, None)          # the old result no longer applies
    record_punch(pin, "Admin")


# NFC tags live in their own module; see nfctag.py for how they work.
app.register_blueprint(nfctag.create_blueprint(clock_response, log_event))


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
        today=f"{datetime.now():%A, %B} {datetime.now().day}, {datetime.now():%Y}",
        new_pin=request.args.get("pin"),
        new_name=request.args.get("name"),
        pins_assigned=cs.pins_assigned(),
        nfc_panel=nfctag.admin_panel(),
        digital_id_panel=digitalID.admin_panel(),
        import_panel=staffadmin.admin_panel(),
        notice=request.args.get("notice"),
        notice_kind=request.args.get("kind", "ok"),
    )


@app.route("/admin/pin-mode", methods=["POST"])
def change_pin_mode():
    """Admin switches between employees choosing PINs and assigned PINs."""
    mode = request.form.get("pin_mode")
    if mode not in (cs.PIN_MODE_CHOOSE, cs.PIN_MODE_ASSIGNED):
        return redirect(url_for("dashboard", notice="Pick one of the two PIN options.", kind="error"))
    if mode == cs.pin_mode:
        return redirect(url_for("dashboard", notice="That's already the PIN setting.", kind="ok"))
    finalized = cs.set_pin_mode(mode)
    save_settings()
    log_event("pin_mode_changed", {"mode": mode, "codes_made_permanent": finalized})
    if mode == cs.PIN_MODE_ASSIGNED:
        notice = "PINs are now assigned by the system. New staff get a random PIN and can't choose."
        if finalized:
            notice += (f" {finalized} employee{'s' if finalized != 1 else ''} still on a temporary code"
                       f" keep{'s' if finalized == 1 else ''} it as their PIN.")
    else:
        notice = "Employees now choose their own PIN. New staff get a temporary code first; existing PINs don't change."
    return redirect(url_for("dashboard", notice=notice, kind="ok"))


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


@app.route("/admin/remove/<pin>", methods=["GET", "POST"])
def remove_employee(pin):
    """Removing someone frees their PIN. Deliberately slow to do by accident:
    the dashboard only links here, and this page won't remove anyone until
    the admin types the employee's full name and ticks a box. The name is
    checked again here on the server, not just in the browser."""
    pin = cs.clean_pin(pin)
    record = cs.lookup(pin)
    if record is None:
        return redirect(url_for("dashboard", notice="That employee no longer exists.", kind="error"))

    error = None
    if request.method == "POST":
        if request.form.get("understand") != "yes":
            error = "Tick the box to confirm you understand this can't be undone."
        else:
            name = record["name"]
            result = cs.remove_employee(pin, request.form.get("typed_name", ""))
            if result == cs.EMPLOYEE_REMOVED:
                # Clear everything else this app held for that PIN, so the
                # next person to get it starts clean.
                last_punch.pop(pin, None)
                recent_clocks.pop(pin, None)
                phones = nfctag.forget_devices_for(pin)
                log_event("employee_removed", {"pin": pin, "phones_forgotten": phones})
                return redirect(url_for(
                    "dashboard", kind="ok",
                    notice=f"{name} was removed. PIN {pin} is free to use again.",
                ))
            error = "The name you typed doesn't match. Nothing was removed."
        log_event("employee_remove_refused", {"pin": pin, "reason": error})

    return render_template_string(
        REMOVE_HTML, pin=pin, record=record, error=error,
        phones=sum(1 for p in nfctag.remembered_devices.values() if p == pin),
    )


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
    response = clock_response(pin, "PIN")
    log_event("confirm", {"pin": pin, "result": clock_outcome(response)})
    return jsonify(response)


@app.route("/api/set-pin", methods=["POST"])
def api_set_pin():
    """New employee replaces their issued code with a PIN of their own."""
    payload = request.get_json(silent=True) or {}
    current = cs.clean_pin(payload.get("current_pin"))
    chosen = cs.clean_pin(payload.get("new_pin"))

    # When PINs are assigned, nobody chooses one, even by calling this directly.
    if cs.pins_assigned():
        log_event("set_pin", {"from": current, "result": cs.PINS_ARE_ASSIGNED})
        return jsonify({"ok": False, "message": "PINs are assigned by your site. Use the PIN you were given."})

    result = cs.set_custom_pin(current, chosen)
    log_event("set_pin", {"from": current, "result": result})

    if result == cs.PIN_SET:
        move_pin_state(current, chosen)
        return jsonify({"ok": True, "name": cs.employee_records[chosen]["name"]})

    messages = {
        cs.PIN_TAKEN: "That PIN is already taken. Choose a different one.",
        cs.PIN_BAD_FORMAT: f"Your PIN must be {cs.MIN_PIN_LENGTH}-{cs.MAX_PIN_LENGTH} digits.",
        cs.DOES_NOT_EXIST: "That code isn't registered. Start over.",
    }
    return jsonify({"ok": False, "message": messages.get(result, result)})


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

    # clock_response() ignores a repeat within the cooldown, so a double
    # beep can't clock someone in and straight back out.
    response = clock_response(pin, "Badge")
    log_event("badge_scan", {"badge": badge, "source": source, "result": clock_outcome(response)})
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
  .admin-link { align-self: center; font-size: 12px; color: var(--muted); text-decoration: none; opacity: .7; }
  .admin-link:hover { opacity: 1; text-decoration: underline; }

  /* Landscape (laptop, tablet on its side): info on the left,
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
  btnEnter: $("btnEnter"), btnClear: $("btnClear"),
  clock: $("clock"),

  confirmWho: $("confirmWho"), whoName: $("whoName"), whoState: $("whoState"),
  whoInitials: $("whoInitials"), whoYes: $("whoYes"), whoNo: $("whoNo"),

  setupPin: $("setupPin"), setupName: $("setupName"), setupDots: $("setupDots"),
  setupNumpad: $("setupNumpad"), setupBanner: $("setupBanner"),
  setupLabel: $("setupLabel"), setupSave: $("setupSave"), setupCancel: $("setupCancel"),

  confirmPopup: $("confirmPopup"), confirmName: $("confirmName"),
  confirmAction: $("confirmAction"), confirmTime: $("confirmTime"),
  confirmIcon: $("confirmIcon"), confirmNote: $("confirmNote")
};

// pin        - what's typed on the main screen
// setupValue - the new PIN a first-timer is choosing
// pendingPin - whose record the open popup is about
const state = { pin: "", setupValue: "", pendingPin: null };

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

const POPUPS = ["confirmWho", "setupPin", "confirmPopup"];

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
  const data = await post("/api/clock", { pin: state.pendingPin });

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
    clearPin();
  }, RESET_AFTER_MS);
}

/* ---------- ID card scans ---------- */

// Every scan, from any source, comes through here. A camera scanner added
// later just calls handleScan(code, "camera") with what it decoded.
// A PIN setup or "is this you?" popup belongs to whoever is
// using the kiosk right now; a card scan or NFC tap shouldn't clock someone
// else in underneath it. The result screen is fine to replace: that's just
// the next person in line.
function kioskBusy() {
  return el.overlay.classList.contains("show") && el.confirmPopup.hidden;
}

// Shows what the server said about a card scan or NFC tap (nfctag.py uses
// this too): the in/out result screen, or the red banner on failure.
function showScanResult(data) {
  // Any half-typed PIN belonged to no one; the card or tag decides who this is.
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

async function handleScan(code, source) {
  if (kioskBusy()) {
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

  showScanResult(data);
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

el.btnEnter.addEventListener("click", submitPin);
el.btnClear.addEventListener("click", clearPin);

el.whoYes.addEventListener("click", confirmWho);
el.whoNo.addEventListener("click", rejectWho);

el.setupSave.addEventListener("click", saveNewPin);
el.setupCancel.addEventListener("click", () => { closePopup(); clearPin(); });


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

  .pin-policy { align-items: center; }
  .pin-policy .choice { display: flex; align-items: center; gap: 8px; font-size: 14px; color: var(--text);
    cursor: pointer; white-space: nowrap; }
  .pin-policy .choice input { width: 18px; height: 18px; }
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
  /* deliberately quiet: it only opens the confirmation page */
  .row-links { white-space: nowrap; }
  .name-link { color: inherit; text-decoration: none; }
  .name-link:hover { text-decoration: underline; }
  .id-link { font-size: 12px; color: var(--accent); text-decoration: none; margin-right: 10px; }
  .id-link:hover { text-decoration: underline; }
  .remove-link { font-size: 12px; color: var(--muted); text-decoration: none; opacity: .7; white-space: nowrap; }
  .remove-link:hover { color: var(--err); opacity: 1; text-decoration: underline; }
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
    /* hide the ID card column */
    th:nth-child(5), td:nth-child(5) { display: none; }
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
      {% if pins_assigned %}
      <div class="pin-callout-label">PIN for {{ new_name }}</div>
      <p class="sub">Give it to them now. This is their PIN; they can't change it.</p>
      {% else %}
      <div class="pin-callout-label">Temporary code for {{ new_name }}</div>
      <p class="sub">Give it to them now. They'll choose their own PIN at the kiosk.</p>
      {% endif %}
    </div>
    <div class="pin-callout-pin">{{ new_pin }}</div>
  </div>
  {% endif %}

  <div class="tiles">
    <div class="tile on">     <div class="tile-label">On shift</div>     <div class="tile-value">{{ counts.on }}</div></div>
    <div class="tile">        <div class="tile-label">Clocked out</div>  <div class="tile-value">{{ counts.out }}</div></div>
    {% if not pins_assigned or counts.pending %}
    <div class="tile pending"><div class="tile-label">PIN not set</div>  <div class="tile-value">{{ counts.pending }}</div></div>
    {% endif %}
    <div class="tile">        <div class="tile-label">Total staff</div>  <div class="tile-value">{{ counts.all }}</div></div>
  </div>

  <form class="card-link pin-policy" method="post" action="/admin/pin-mode"
        onsubmit='return confirmPinMode(this)'>
    <div class="card-link-text">
      <div class="card-link-title">PIN policy</div>
      <p class="sub">{{ 'PINs are assigned by the system. Staff use the PIN they are given.' if pins_assigned
                       else 'Staff get a temporary code, then choose their own PIN at the kiosk.' }}</p>
    </div>
    <label class="choice"><input type="radio" name="pin_mode" value="choose" {{ '' if pins_assigned else 'checked' }}>
      Employees choose their own PIN</label>
    <label class="choice"><input type="radio" name="pin_mode" value="assigned" {{ 'checked' if pins_assigned else '' }}>
      Assign each employee a random PIN</label>
    <button type="submit">Save</button>
  </form>
  <script>
    // Switching to assigned PINs turns pending temporary codes into permanent
    // PINs, so say so before it happens.
    function confirmPinMode(form) {
      const mode = form.pin_mode.value;
      const pending = {{ counts.pending }};
      if (mode === "assigned" && !{{ pins_assigned|tojson }}) {
        return confirm("Switch to assigned PINs? New staff will get a random PIN they can't change."
          + (pending ? "\\n\\n" + pending + " employee(s) still on a temporary code will keep that code as their permanent PIN." : ""));
      }
      return true;
    }
  </script>

  <div class="controls">
    <input type="search" id="search" placeholder="Search by name, PIN or card…" autocomplete="off">
    <select id="statusFilter">
      <option value="all">All statuses</option>
      <option value="in">On shift</option>
      <option value="out">Clocked out</option>
      {% if not pins_assigned or counts.pending %}<option value="pending">PIN not set</option>{% endif %}
    </select>
    <form class="register" method="post" action="/register">
      <input type="text" name="name" placeholder="New employee name" autocomplete="off" required>
      <button type="submit">Register &amp; {{ 'assign PIN' if pins_assigned else 'issue code' }}</button>
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
  {{ nfc_panel }}
  {{ digital_id_panel }}
  {{ import_panel }}
  {% endif %}

  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>Employee</th><th>PIN</th><th>Status</th><th>Last punch</th><th>ID card</th><th>Method</th><th></th>
        </tr>
      </thead>
      <tbody id="rows">
        {% for p in punches %}
        <tr data-name="{{ p.name|lower }}" data-id="{{ p.id }}" data-badge="{{ p.badge|lower }}"
            data-status="{{ 'pending' if p.temporary else p.status }}">
          <td class="name"><a class="name-link" href="/admin/employee/{{ p.id }}">{{ p.name }}</a></td>
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
          <td class="row-links"><a class="id-link" href="/admin/employee/{{ p.id }}">Edit</a>
              <a class="id-link" href="/digital-id/{{ p.id }}">Digital ID</a>
              <a class="remove-link" href="/admin/remove/{{ p.id }}">Remove…</a></td>
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


# =====================================================================
# REMOVE-EMPLOYEE CONFIRMATION PAGE
# =====================================================================

REMOVE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remove employee</title>
<style>
""" + BASE_CSS + """
  body { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .card { width: min(520px, 100%); background: var(--card); border: 1px solid rgba(255,107,107,.45);
    border-radius: 18px; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  h1 { margin: 0; font-size: 22px; color: var(--err); }
  .who { font-size: 26px; font-weight: 700; }
  ul { margin: 0; padding-left: 20px; color: var(--muted); font-size: 14px; line-height: 1.6; }
  .warn { background: rgba(245,196,81,.12); color: var(--warn); border: 1px solid rgba(245,196,81,.4);
    border-radius: 12px; padding: 10px 14px; font-size: 14px; font-weight: 600; }
  .error { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.4);
    border-radius: 12px; padding: 10px 14px; font-size: 14px; font-weight: 600; }
  label { font-size: 14px; color: var(--muted); display: flex; flex-direction: column; gap: 6px; }
  input[type="text"] { background: var(--field); border: 1px solid var(--line); color: var(--text);
    border-radius: 10px; padding: 12px 14px; font-size: 16px; font-family: inherit; }
  input[type="text"]:focus { outline: none; border-color: var(--err); }
  .check { flex-direction: row; align-items: center; gap: 10px; }
  .check input { width: 20px; height: 20px; }
  .actions { display: flex; gap: 10px; }
  .actions > * { flex: 1; min-height: 50px; border-radius: 10px; font: inherit; font-size: 15px;
    font-weight: 600; cursor: pointer; display: flex; align-items: center; justify-content: center; text-decoration: none; }
  .back { background: var(--accent); border: 1px solid var(--accent); color: #fff; }
  .danger { background: transparent; border: 1px solid var(--err); color: var(--err); }
  .danger:disabled { opacity: .35; cursor: not-allowed; }
  .danger:not(:disabled):hover { background: var(--err); color: #fff; }
</style>
</head>
<body>
  <form class="card" method="post" autocomplete="off">
    <h1>Remove employee</h1>
    <div class="who">{{ record.name }}</div>

    <ul>
      <li>Their record is deleted and PIN <b>{{ pin }}</b> becomes free for someone else.</li>
      {% set linked = [] %}
      {% if record.badge %}{% set _ = linked.append("ID card") %}{% endif %}
      {% if phones %}{% set _ = linked.append(phones ~ " remembered phone" ~ ("s" if phones != 1 else "")) %}{% endif %}
      {% if linked %}
      <li>Their linked {{ linked[:-1]|join(", ") ~ (" and " if linked|length > 1 else "") ~ linked[-1] }}
          will stop working.</li>
      {% endif %}
      <li><b>This can't be undone.</b> To bring them back you'd register them again.</li>
    </ul>

    {% if record.status == "clocked_in" %}
    <div class="warn">{{ record.name }} is currently clocked in. Removing them ends that shift with no clock-out.</div>
    {% endif %}

    {% if error %}<div class="error">{{ error }}</div>{% endif %}

    <label>Type their full name to confirm: <b style="color:var(--text)">{{ record.name }}</b>
      <input type="text" name="typed_name" id="typedName" spellcheck="false" autofocus>
    </label>

    <label class="check">
      <input type="checkbox" name="understand" value="yes" id="understand">
      I understand this permanently removes {{ record.name }}.
    </label>

    <div class="actions">
      <a class="back" href="/admin">Cancel, keep them</a>
      <button class="danger" type="submit" id="removeBtn" disabled>Remove permanently</button>
    </div>
  </form>

<script>
// The button only wakes up once the typed name matches and the box is
// ticked. The server checks the name again, so this is a convenience only.
const expected = {{ record.name|tojson }};
const tidy = s => s.trim().split(/\\s+/).join(" ").toLowerCase();
const typed = document.getElementById("typedName");
const box = document.getElementById("understand");
const btn = document.getElementById("removeBtn");
function update() { btn.disabled = !(box.checked && tidy(typed.value) === tidy(expected)); }
typed.addEventListener("input", update);
box.addEventListener("change", update);
// No pasting: the name has to be typed on purpose.
typed.addEventListener("paste", e => e.preventDefault());
</script>
</body>
</html>
"""


# Digital ID cards live in their own module (digitalID.py). Registered here,
# below BASE_CSS, because the ID page reuses the same styles.
app.register_blueprint(digitalID.create_blueprint(log_event, BASE_CSS, SITE_LABEL))

# Spreadsheet import and the edit-employee page (staffadmin.py).
app.register_blueprint(staffadmin.create_blueprint(
    log_event, BASE_CSS, SITE_LABEL, move_pin_state, status_corrected))


if __name__ == "__main__":
    # The reloader restarts the server whenever a .py file is saved. It runs
    # the code twice (a watcher, then the real worker), so seed only in the
    # worker, or the demo staff would get a second set of codes.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        seed_demo_staff()

    # Flask's interactive debugger lets anyone who sees an error page run
    # Python on this computer, and phones on the Wi-Fi can reach it, so it's
    # off unless asked for: KIOSK_DEBUG=1 python app.py
    debug = os.environ.get("KIOSK_DEBUG") == "1"

    # host="0.0.0.0" so the Android tablet on the same network can reach it.
    # Web NFC on the tablet needs https. KIOSK_HTTPS=1 python app.py serves
    # over https with a throwaway self-signed certificate (needs the
    # "cryptography" package); the tablet shows a warning to accept once.
    ssl = "adhoc" if os.environ.get("KIOSK_HTTPS") == "1" else None
    app.run(host="0.0.0.0", port=5000, debug=debug, use_reloader=True, ssl_context=ssl)
