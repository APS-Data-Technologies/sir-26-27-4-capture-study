"""
NFC wall tag: tap with your own phone to clock in / out
-------------------------------------------------------
One NFC tag (e.g. an NTAG215 sticker) is posted at the site. Employees tap it
with their own phone. The tag holds a web link; phones open links from NFC
tags by themselves (iPhone and Android, no app needed), so the tap opens this
module's /tap page in the phone's browser.

    First tap on a phone
        The page asks for the employee's PIN, shows "Is this you?", and on
        yes clocks them in or out. It offers to remember the phone.
    Every tap after that (remembered phone)
        One tap clocks them in or out immediately.

Setting up the tag
    1. Open /admin. The "NFC wall tag" panel shows the exact link to put on
       the tag.
    2. Write that link onto the tag as a URL record, with a free phone app
       such as "NFC Tools" (Write -> Add a record -> URL). Lock the tag
       afterwards in the same app so nobody can overwrite it.
    3. Stick the tag where people arrive.
    Phones must be able to reach this server, so they need to be on the
    same Wi-Fi as the computer running app.py.

Why the link has a secret code in it
    /tap only works with the site's current code, so a guessed link doesn't
    work. Anyone who saves the link could still use it from elsewhere; if
    that happens, press "Make a new link" on /admin and rewrite the tag.
    The old link stops working immediately.

How it plugs in
    app.py registers this module with:
        import nfctag
        app.register_blueprint(nfctag.create_blueprint(clock_response, log_event))
    and shows nfctag.admin_panel() on the admin page. The clock lever itself
    stays in clock_system.py, so a phone tap, a card scan and a PIN all flip
    the same switch.
"""

import os
import secrets
import socket
import time
from datetime import datetime

from flask import Blueprint, jsonify, make_response, redirect, render_template_string, request, url_for
from markupsafe import Markup

import clock_system as cs

# ---------------------------------------------------------------------
# The site code written into the tag's link
# ---------------------------------------------------------------------
# Kept in a small file next to this module so the link on the tag survives
# server restarts. Delete the file (or press "Make a new link") to replace it.
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nfc_site_token.txt")


def _load_or_create_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
            if token:
                return token
    except FileNotFoundError:
        pass
    return rotate_token()


def rotate_token() -> str:
    """Makes a new site code; the link on the tag must then be rewritten."""
    global site_token
    site_token = secrets.token_urlsafe(9)
    with open(TOKEN_FILE, "w") as f:
        f.write(site_token)
    return site_token


site_token = None  # set on first use by current_token()


def current_token() -> str:
    global site_token
    if site_token is None:
        site_token = _load_or_create_token()
    return site_token


# ---------------------------------------------------------------------
# Remembered phones
# ---------------------------------------------------------------------
# The phone keeps a random device ID in a cookie; this maps it to the
# employee's PIN. The ID itself means nothing on its own, and forgetting a
# phone (from the phone or from /admin) deletes the mapping.
DEVICE_COOKIE = "clock_device"
DEVICE_COOKIE_DAYS = 365

# device ID -> PIN
remembered_devices = {}


def remember_device(pin: str) -> str:
    device_id = secrets.token_urlsafe(24)
    remembered_devices[device_id] = cs.clean_pin(pin)
    return device_id


def device_pin(device_id):
    """The PIN this phone belongs to, or None if unknown or since removed."""
    pin = remembered_devices.get(device_id or "")
    if pin is None or pin not in cs.employee_records:
        return None
    return pin


def forget_devices_for(pin: str) -> int:
    pin = cs.clean_pin(pin)
    doomed = [d for d, p in remembered_devices.items() if p == pin]
    for d in doomed:
        del remembered_devices[d]
    return len(doomed)


# ---------------------------------------------------------------------
# Double-tap guard
# ---------------------------------------------------------------------
# A tap clocks in or out instantly, and phones sometimes open the link twice
# for one tap. Within this window a repeat just shows the earlier result
# instead of clocking the person straight back out.
TAP_COOLDOWN_SECONDS = 10

# PIN -> (time.monotonic() of the tap, the response it got)
recent_taps = {}

# PIN -> when their current shift started (wall-clock seconds), for the
# phone's status page. Set on a phone clock-in; other methods leave it unset
# and the page just doesn't show a timer.
shift_started = {}


def lan_base_url() -> str:
    """The address phones on the Wi-Fi should use, e.g. http://192.168.1.20:5000.

    The admin page is often opened as localhost, which a phone can't reach,
    so this finds the computer's own network address instead.
    """
    scheme = request.scheme
    port = request.host.rsplit(":", 1)[1] if ":" in request.host else ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the Wi-Fi interface
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return request.host_url.rstrip("/")
    return f"{scheme}://{ip}{':' + port if port else ''}"


def tag_url() -> str:
    return f"{lan_base_url()}/tap?t={current_token()}"


# ---------------------------------------------------------------------
# Admin page panel (rendered into the dashboard by app.py)
# ---------------------------------------------------------------------
def admin_panel() -> Markup:
    """Shows the link to write onto the tag and the remembered phones."""
    counts = {}
    for pin in remembered_devices.values():
        if pin in cs.employee_records:
            counts[pin] = counts.get(pin, 0) + 1

    rows = "".join(
        f'<li><span class="nfc-name">{Markup.escape(cs.employee_records[pin]["name"])}</span>'
        f'<span class="sub">{n} phone{"s" if n != 1 else ""}</span>'
        f'<form class="inline" method="post" action="/nfc/forget">'
        f'<input type="hidden" name="pin" value="{pin}">'
        f'<button class="unlink" type="submit">Forget</button></form></li>'
        for pin, n in sorted(counts.items(), key=lambda kv: cs.employee_records[kv[0]]["name"])
    ) or '<li class="sub">No phones remembered yet.</li>'

    url = Markup.escape(tag_url())
    return Markup(f"""
  <div class="card-link nfc-panel">
    <div class="card-link-text">
      <div class="card-link-title">NFC wall tag</div>
      <p class="sub">Write this link onto the tag as a URL record (e.g. with the free
        "NFC Tools" app), then lock the tag. Phones must be on the same Wi-Fi as this computer.</p>
    </div>
    <input type="text" class="nfc-url" value="{url}" readonly onclick="this.select()">
    <form method="post" action="/nfc/rotate"
          onsubmit="return confirm('The link currently on the tag will stop working. Continue?')">
      <button type="submit">Make a new link</button>
    </form>
    <ul class="nfc-list">{rows}</ul>
  </div>
  <style>
    .nfc-panel .nfc-url {{ flex: 1 1 320px; font-family: ui-monospace, monospace; font-size: 13px; }}
    .nfc-list {{ list-style: none; margin: 4px 0 0; padding: 0; width: 100%;
      display: flex; flex-direction: column; gap: 6px; font-size: 14px; }}
    .nfc-list li {{ display: flex; align-items: center; gap: 12px; }}
    .nfc-name {{ font-weight: 600; min-width: 140px; }}
    .nfc-list .unlink {{ background: none; border: 1px solid var(--line); color: var(--muted);
      padding: 3px 8px; font-size: 12px; font-weight: 400; border-radius: 6px; }}
    .nfc-list .unlink:hover {{ color: var(--err); border-color: rgba(255,107,107,.5); }}
  </style>""")


# ---------------------------------------------------------------------
# The page the phone opens
# ---------------------------------------------------------------------
PHONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clock In / Out</title>
<style>
  :root {
    --bg: #0f1420; --card: #1a2130; --field: #131a27; --line: #333f55;
    --key: #2a3547; --text: #eef2f8; --muted: #93a0b5; --accent: #4d8dff;
    --ok: #3ecf8e; --err: #ff6b6b;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center; padding: 16px; }
  .card { width: min(420px, 100%); background: var(--card); border: 1px solid var(--line);
    border-radius: 20px; padding: 22px; display: flex; flex-direction: column; gap: 14px; text-align: center; }
  h1 { font-size: 20px; margin: 0; }
  .sub { color: var(--muted); font-size: 14px; margin: 0; }
  .who { font-size: 30px; font-weight: 700; }
  .action { font-size: 18px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--ok); }
  .out .action { color: var(--accent); }
  .time { color: var(--muted); font-size: 18px; font-variant-numeric: tabular-nums; }
  .icon { width: 84px; height: 84px; border-radius: 50%; margin: 0 auto; font-size: 44px; line-height: 80px;
    background: rgba(62,207,142,.13); border: 2px solid var(--ok); color: var(--ok); }
  .out .icon { background: rgba(77,141,255,.13); border-color: var(--accent); color: var(--accent); }
  .bad .icon { background: rgba(255,107,107,.12); border-color: var(--err); color: var(--err); }
  .error { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.4);
    border-radius: 12px; padding: 10px 14px; font-weight: 600; font-size: 15px; }
  .pin { background: var(--field); border: 2px solid var(--accent); border-radius: 14px;
    min-height: 60px; display: flex; align-items: center; justify-content: center; gap: 12px; }
  .dot { width: 14px; height: 14px; border-radius: 50%; background: var(--text); }
  .pad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .pad button, .btn { min-height: 58px; border-radius: 12px; border: 1px solid var(--line);
    background: var(--key); color: var(--text); font: inherit; font-size: 24px; font-weight: 600; }
  .pad button:active, .btn:active { transform: translateY(1px); }
  .btn { font-size: 17px; width: 100%; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.ghost { background: transparent; color: var(--muted); }
  .btn:disabled { opacity: .4; }
  .row { display: flex; gap: 10px; }
  label.remember { display: flex; align-items: center; justify-content: center; gap: 8px;
    color: var(--muted); font-size: 14px; }
  label.remember input { width: 20px; height: 20px; }
  a { color: var(--muted); font-size: 13px; }
  .pill { align-self: center; padding: 6px 14px; border-radius: 999px; font-weight: 700;
    font-size: 13px; letter-spacing: .05em; text-transform: uppercase; }
  .pill.on { background: rgba(62,207,142,.14); color: var(--ok); }
  .pill.off { background: rgba(147,160,181,.16); color: var(--muted); }
  .timer { font-size: 40px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .overlay { position: fixed; inset: 0; background: rgba(7,10,17,.8); display: flex;
    align-items: center; justify-content: center; padding: 16px; animation: fade .15s ease-out; }
  .popup { animation: rise .2s ease-out; }
  [hidden] { display: none !important; }
  .nfc-waves { position: relative; width: 90px; height: 90px; margin: 4px auto; }
  .nfc-waves span { position: absolute; inset: 0; border: 3px solid var(--accent); border-radius: 50%;
    animation: ripple 1.8s ease-out infinite; opacity: 0; }
  .nfc-waves span:nth-child(2) { animation-delay: .6s; }
  .nfc-waves span:nth-child(3) { animation-delay: 1.2s; }
  @keyframes ripple { from { transform: scale(.3); opacity: 1; } to { transform: scale(1); opacity: 0; } }
  @keyframes fade { from { opacity: 0; } }
  @keyframes rise { from { transform: translateY(14px); opacity: 0; } }
</style>
</head>
<body>

{% if view == "invalid" %}
  <div class="card bad">
    <div class="icon">!</div>
    <h1>This tag's link is out of date</h1>
    <p class="sub">Ask your site lead to update the NFC tag.</p>
  </div>

{% elif view == "status" %}
  <div class="card">
    <div class="who">{{ name }}</div>
    <div class="pill {{ 'on' if on_shift else 'off' }}">{{ 'On shift' if on_shift else 'Clocked out' }}</div>

    {% if on_shift and started_label %}
      <p class="sub">Since {{ started_label }}</p>
      <div class="timer" id="timer">0:00:00</div>
    {% elif on_shift %}
      <p class="sub">You're clocked in.</p>
    {% else %}
      <p class="sub">See you next shift.</p>
    {% endif %}

    <button class="btn primary" type="button" onclick="openScan()">Scan NFC tag</button>
    <p class="sub">To clock {{ 'out' if on_shift else 'in' }}, tap the tag again.</p>

    <form method="post" action="/tap/forget">
      <input type="hidden" name="t" value="{{ token }}">
      <button class="btn ghost" type="submit">Not you? Forget this phone</button>
    </form>
  </div>

  {% if just %}
  <!-- The result of the tap, as a pop-up over the status page -->
  <div class="overlay" id="popup" onclick="closePopup()">
    <div class="card popup {{ 'out' if was_out else '' }}">
      <div class="icon">{{ '&#8594;'|safe if was_out else '&#10003;'|safe }}</div>
      <div class="who">{{ name }}</div>
      <div class="action">{{ 'clocked out' if was_out else 'clocked in' }}</div>
      {% if at %}<div class="time">{{ at }}</div>{% endif %}
      <p class="sub">
        {% if just == "repeat" %}Already recorded a moment ago. Nothing changed.
        {% elif was_out %}You're clocked out. See you next shift.
        {% else %}You're clocked in. Have a good shift.{% endif %}
      </p>
      <button class="btn primary" type="button">OK</button>
    </div>
  </div>
  <script>
    function closePopup() {
      const p = document.getElementById("popup");
      if (p) p.remove();
      // Drop the result from the address, so a reload doesn't show it again.
      history.replaceState(null, "", location.pathname + "?t={{ token }}");
    }
    setTimeout(closePopup, 5000);
  </script>
  {% endif %}

  <!-- "Scan NFC tag" pop-up -->
  <div class="overlay" id="scanPopup" hidden>
    <div class="card popup">
      <div class="nfc-waves" aria-hidden="true"><span></span><span></span><span></span></div>
      <h1>Scan NFC</h1>
      <p class="sub" id="scanHelp">Hold the top of your phone near the clock-in tag.</p>
      <div class="error" id="scanError" hidden></div>
      <button class="btn ghost" type="button" onclick="closeScan()">Cancel</button>
    </div>
  </div>
  <script>
    // iPhones: Safari can't read NFC from a web page, but an unlocked iPhone
    // reads the tag by itself and opens its link, so the pop-up just says
    // where to hold it. Android Chrome: Web NFC can start the scan right
    // here (https only); when the tag is read we open its link, which
    // clocks the person in or out exactly like a normal tap.
    const isIPhone = /iPhone|iPad|iPod/.test(navigator.userAgent)
      || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    let scanAbort = null;

    function scanMessage(text, isError) {
      document.getElementById("scanHelp").hidden = !!isError;
      const err = document.getElementById("scanError");
      err.hidden = !isError;
      if (isError) err.textContent = text; else document.getElementById("scanHelp").textContent = text;
    }

    async function openScan() {
      document.getElementById("scanPopup").hidden = false;
      if (isIPhone) {
        scanMessage("Hold the top of your iPhone near the tag. Keep the screen on and unlocked; " +
                    "your iPhone reads it automatically.");
        return;
      }
      if (!("NDEFReader" in window)) {
        scanMessage("Hold the back of your phone near the tag. If nothing happens, check NFC is " +
                    "turned on in your phone's settings.");
        return;
      }
      scanMessage("Hold the back of your phone near the tag…");
      try {
        scanAbort = new AbortController();
        const reader = new NDEFReader();
        await reader.scan({ signal: scanAbort.signal });
        reader.onreading = event => {
          const decoder = new TextDecoder();
          for (const record of event.message.records) {
            if (record.recordType === "url" || record.recordType === "absolute-url") {
              const url = decoder.decode(record.data);
              if (url.includes("/tap?")) { closeScan(); location.href = url; return; }
            }
          }
          scanMessage("That isn't the clock-in tag. Try again.", true);
        };
        reader.onreadingerror = () => scanMessage("Couldn't read the tag. Hold it still and try again.", true);
      } catch (e) {
        scanMessage(e.name === "NotAllowedError"
          ? "NFC permission was blocked. Allow it in your browser's site settings."
          : "Hold the back of your phone near the tag. (In-page scanning needs the https link.)", true);
      }
    }

    function closeScan() {
      if (scanAbort) { scanAbort.abort(); scanAbort = null; }
      document.getElementById("scanPopup").hidden = true;
    }
  </script>

  {% if started_ms %}
  <script>
    const start = {{ started_ms }};
    const el = document.getElementById("timer");
    function tick() {
      const s = Math.max(0, Math.floor((Date.now() - start) / 1000));
      const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
      el.textContent = h + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
    }
    tick(); setInterval(tick, 1000);
  </script>
  {% endif %}

{% elif view == "result" %}
  <div class="card {{ 'out' if result.action == 'clocked out' else '' }}">
    <div class="icon">{{ '&#8594;'|safe if result.action == 'clocked out' else '&#10003;'|safe }}</div>
    <div class="who">{{ result.name }}</div>
    <div class="action">{{ result.action }}</div>
    <div class="time">{{ result.time }}</div>
    <p class="sub">
      {% if result.repeat %}Already recorded a moment ago. Nothing changed.
      {% elif result.action == 'clocked out' %}You're clocked out. See you next shift.
      {% else %}You're clocked in. Have a good shift.{% endif %}
    </p>
    {% if remembered %}
    <p class="sub">Next time, just tap the tag.</p>
    <form method="post" action="/tap/forget">
      <input type="hidden" name="t" value="{{ token }}">
      <button class="btn ghost" type="submit">Not you? Forget this phone</button>
    </form>
    {% endif %}
  </div>

{% elif view == "confirm" %}
  <div class="card">
    <h1>Is this you?</h1>
    <div class="who">{{ name }}</div>
    <p class="sub">{{ 'Currently on shift: confirming clocks you out.' if clocked_in
                     else 'Currently clocked out: confirming clocks you in.' }}</p>
    <form method="post" action="/tap/confirm">
      <input type="hidden" name="t" value="{{ token }}">
      <input type="hidden" name="pin" value="{{ pin }}">
      <label class="remember">
        <input type="checkbox" name="remember" value="1" checked>
        Remember this phone, so next time one tap is enough
      </label>
      <div class="row" style="margin-top:14px">
        <a class="btn ghost" href="/tap?t={{ token }}" style="display:flex;align-items:center;justify-content:center;text-decoration:none">No, try again</a>
        <button class="btn primary" type="submit">Yes, that's me</button>
      </div>
    </form>
  </div>

{% else %}
  <div class="card">
    <h1>Clock In / Out</h1>
    <p class="sub">Enter your {{ min_len }}-{{ max_len }} digit PIN. You only need to do this once on this phone.</p>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="post" action="/tap/identify" id="pinForm">
      <input type="hidden" name="t" value="{{ token }}">
      <input type="hidden" name="pin" id="pinValue">
      <div class="pin" id="dots"></div>
      <div class="pad" id="pad" style="margin-top:10px"></div>
      <button class="btn primary" type="submit" id="go" disabled style="margin-top:10px">Continue</button>
    </form>
  </div>
  <script>
    const MIN = {{ min_len }}, MAX = {{ max_len }};
    let pin = "";
    const dots = document.getElementById("dots"), go = document.getElementById("go"),
          field = document.getElementById("pinValue"), pad = document.getElementById("pad");
    function render() {
      dots.innerHTML = "";
      for (let i = 0; i < pin.length; i++) { const d = document.createElement("span"); d.className = "dot"; dots.appendChild(d); }
      field.value = pin;
      go.disabled = pin.length < MIN;
    }
    function key(label, fn) {
      const b = document.createElement("button"); b.type = "button"; b.textContent = label;
      b.addEventListener("click", fn); pad.appendChild(b);
    }
    "123456789".split("").forEach(n => key(n, () => { if (pin.length < MAX) { pin += n; render(); } }));
    key("", () => {});
    key("0", () => { if (pin.length < MAX) { pin += "0"; render(); } });
    key("\\u232B", () => { pin = pin.slice(0, -1); render(); });
    render();
  </script>
{% endif %}

</body>
</html>
"""


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
def create_blueprint(clock_response, log_event):
    """Builds the /tap routes.

    clock_response(pin, method) and log_event(type, detail) come from app.py,
    so phone taps flip the lever and get logged like every other method.
    """
    bp = Blueprint("nfc", __name__)

    def page(**ctx):
        return render_template_string(
            PHONE_HTML, token=current_token(),
            min_len=cs.MIN_PIN_LENGTH, max_len=cs.MAX_PIN_LENGTH, **ctx,
        )

    def token_ok():
        given = request.values.get("t", "")
        return secrets.compare_digest(given, current_token())

    def clock(pin):
        """Flip the lever, honouring the double-tap guard."""
        now = time.monotonic()
        previous = recent_taps.get(pin)
        if previous and now - previous[0] < TAP_COOLDOWN_SECONDS:
            log_event("nfc_tap", {"pin": pin, "result": "repeat ignored"})
            return {**previous[1], "repeat": True}

        response = clock_response(pin, "NFC")
        log_event("nfc_tap", {"pin": pin, "result": response.get("action") or response.get("message")})
        if response["ok"]:
            recent_taps[pin] = (now, response)
            if response["action"] == cs.CLOCKED_IN:
                shift_started[pin] = time.time()
            else:
                shift_started.pop(pin, None)
        return response

    def status_url(**extra):
        from urllib.parse import urlencode
        return "/tap/status?" + urlencode({"t": current_token(), **extra})

    @bp.route("/tap")
    def tap():
        """What the tag's link opens."""
        if not token_ok():
            log_event("nfc_tap", {"result": "bad site code"})
            return page(view="invalid"), 403

        pin = device_pin(request.cookies.get(DEVICE_COOKIE))
        if pin is None:
            return page(view="pin")

        response = clock(pin)
        if not response["ok"]:
            return page(view="pin", error=response["message"])
        return redirect(status_url(just="repeat" if response.get("repeat") else response["action"],
                                   at=response.get("time", "")))

    @bp.route("/tap/identify", methods=["POST"])
    def identify():
        """First tap on this phone: PIN entered, show "Is this you?"."""
        if not token_ok():
            return page(view="invalid"), 403

        pin = cs.clean_pin(request.form.get("pin"))
        record = cs.lookup(pin)
        if record is None:
            log_event("nfc_identify", {"pin": pin, "result": cs.DOES_NOT_EXIST})
            return page(view="pin", error="That PIN isn't registered. Check it and try again.")
        if record["temporary"]:
            return page(view="pin", error="Finish setting up first: enter the code you were given on the kiosk.")

        return page(view="confirm", pin=pin, name=record["name"],
                    clocked_in=record["status"] == "clocked_in")

    @bp.route("/tap/confirm", methods=["POST"])
    def confirm():
        """They confirmed the name: clock them, and remember the phone if asked."""
        if not token_ok():
            return page(view="invalid"), 403

        pin = cs.clean_pin(request.form.get("pin"))
        response = clock(pin)
        if not response["ok"]:
            return page(view="pin", error=response["message"])

        remember = request.form.get("remember") == "1"
        if remember:
            just = "repeat" if response.get("repeat") else response["action"]
            resp = redirect(status_url(just=just, at=response.get("time", "")))
        else:
            resp = make_response(page(view="result", result=response, remembered=False))
        if remember:
            resp.set_cookie(
                DEVICE_COOKIE, remember_device(pin),
                max_age=DEVICE_COOKIE_DAYS * 24 * 3600,
                httponly=True, samesite="Lax", secure=request.is_secure,
            )
            log_event("nfc_remember_phone", {"pin": pin})
        return resp

    @bp.route("/tap/status")
    def status():
        """The page that stays up: on shift or not, since when, and a button
        to clock the other way. Loading or reloading it changes nothing."""
        if not token_ok():
            return page(view="invalid"), 403
        pin = device_pin(request.cookies.get(DEVICE_COOKIE))
        if pin is None:
            return page(view="pin")

        record = cs.employee_records[pin]
        on_shift = record["status"] == "clocked_in"
        started = shift_started.get(pin) if on_shift else None
        return page(
            view="status",
            name=record["name"],
            on_shift=on_shift,
            started_label=datetime.fromtimestamp(started).strftime("%I:%M %p").lstrip("0") if started else None,
            started_ms=int(started * 1000) if started else None,
            just=request.args.get("just"),
            at=request.args.get("at", ""),
            was_out=request.args.get("just") == "clocked out"
                    or (request.args.get("just") == "repeat" and not on_shift),
        )

    @bp.route("/tap/forget", methods=["POST"])
    def forget_this_phone():
        """ "Not you?" on the phone: drop the remembered link and start over."""
        remembered_devices.pop(request.cookies.get(DEVICE_COOKIE, ""), None)
        resp = redirect(f"/tap?t={request.form.get('t', '')}")
        resp.delete_cookie(DEVICE_COOKIE)
        return resp

    # --- JSON API for the native iPhone app (ios/ClockTap) --------------
    # Same flow as the web pages above: the app reads the tag's link, sends
    # the site code "t" from it, identifies once with a PIN, and from then on
    # sends its remembered device ID.

    def body():
        return request.get_json(silent=True) or {}

    def app_token_ok(data):
        return secrets.compare_digest(str(data.get("t", "")), current_token())

    BAD_TAG = {"ok": False, "message": "This tag's link is out of date. Ask your site lead to update it."}

    @bp.route("/api/app/tap", methods=["POST"])
    def app_tap():
        """A remembered phone tapped: clock straight away."""
        data = body()
        if not app_token_ok(data):
            return jsonify(BAD_TAG), 403
        pin = device_pin(data.get("device"))
        if pin is None:
            return jsonify({"ok": False, "needs_pin": True})
        return jsonify(clock(pin))

    @bp.route("/api/app/identify", methods=["POST"])
    def app_identify():
        """First tap: PIN entered, return the name for "Is this you?"."""
        data = body()
        if not app_token_ok(data):
            return jsonify(BAD_TAG), 403
        pin = cs.clean_pin(data.get("pin"))
        record = cs.lookup(pin)
        if record is None:
            return jsonify({"ok": False, "message": "That PIN isn't registered. Check it and try again."})
        if record["temporary"]:
            return jsonify({"ok": False, "message": "Finish setting up first: enter the code you were given on the kiosk."})
        return jsonify({"ok": True, "name": record["name"], "clocked_in": record["status"] == "clocked_in"})

    @bp.route("/api/app/confirm", methods=["POST"])
    def app_confirm():
        """They confirmed the name: clock them and hand back a device ID."""
        data = body()
        if not app_token_ok(data):
            return jsonify(BAD_TAG), 403
        pin = cs.clean_pin(data.get("pin"))
        response = clock(pin)
        if response["ok"] and data.get("remember", True):
            response = {**response, "device": remember_device(pin)}
            log_event("nfc_remember_phone", {"pin": pin, "via": "app"})
        return jsonify(response)

    @bp.route("/api/app/forget", methods=["POST"])
    def app_forget():
        remembered_devices.pop(str(body().get("device", "")), None)
        return jsonify({"ok": True})

    @bp.route("/nfc/forget", methods=["POST"])
    def admin_forget():
        """Admin: forget every phone remembered for this employee."""
        pin = cs.clean_pin(request.form.get("pin"))
        record = cs.lookup(pin)
        n = forget_devices_for(pin)
        log_event("nfc_forget_phones", {"pin": pin, "count": n})
        name = record["name"] if record else "that employee"
        return redirect(url_for("dashboard", notice=f"Forgot {n} phone(s) for {name}.", kind="ok"))

    @bp.route("/nfc/rotate", methods=["POST"])
    def admin_rotate():
        """Admin: new site code. The tag must be rewritten with the new link."""
        rotate_token()
        log_event("nfc_rotate_link")
        return redirect(url_for(
            "dashboard", kind="ok",
            notice="New tag link made. Rewrite the NFC tag with the link below; the old one no longer works.",
        ))

    return bp
