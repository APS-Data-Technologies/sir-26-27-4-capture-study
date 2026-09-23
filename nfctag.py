"""
NFC tag clock-in / clock-out
----------------------------
Lets employees clock in or out by tapping an NFC tag (e.g. an NTAG215
sticker on their badge) against the kiosk tablet.

How it reads tags
    The tablet's own NFC chip, through Web NFC in the browser. No reader
    hardware or driver is needed, but Web NFC has three hard requirements:
      1. Chrome on Android. Not iPad, not desktop browsers.
      2. HTTPS. The tablet must open the kiosk at https://..., not http://.
         For testing, start the server with a self-signed certificate:
             pip install cryptography
             KIOSK_HTTPS=1 python app.py
         then open https://<this-machine-ip>:5000 on the tablet and accept
         the certificate warning once.
      3. A tap on "Turn on NFC" the first time. Chrome remembers the
         permission, so after that the kiosk starts listening by itself.

What identifies a tag
    Its serial number (UID). Every NTAG215 has a unique 7-byte UID burned
    in at the factory, so tags work straight out of the packet: nothing
    has to be written onto them first. The employer links each tag to an
    employee once, on the admin page, by tapping it on the tablet.

How it plugs in
    app.py registers this module with two lines:
        import nfctag
        app.register_blueprint(nfctag.create_blueprint(clock_response, log_event))
    and loads /nfc/nfc.js on the kiosk and admin pages. The clock lever
    itself stays in clock_system.py, so a tag tap, a card scan and a PIN
    all flip the same switch.
"""

import re
import time

from flask import Blueprint, Response, jsonify, redirect, request, url_for
from markupsafe import Markup

import clock_system as cs

# ---------------------------------------------------------------------
# Result strings
# ---------------------------------------------------------------------
TAG_LINKED = "tag linked"
TAG_UNLINKED = "tag unlinked"
TAG_ALREADY_LINKED = "that tag is already linked to someone else"
TAG_BAD_FORMAT = "tag serial number is missing or malformed"

# A tapped tag clocks in or out instantly. Phones and tablets often read the
# same tag two or three times during one tap, and a nervous second tap would
# clock the person straight back out, so repeats inside this window just
# show the earlier result again.
TAP_COOLDOWN_SECONDS = 10

# PIN -> (time.monotonic() of the tap, the response it got)
recent_taps = {}


# ---------------------------------------------------------------------
# Tag <-> employee links (stored on the clock_system record as "nfc_tag")
# ---------------------------------------------------------------------
def normalize_serial(value) -> str:
    """Reduce a tag serial to bare uppercase hex so every form matches.

    Chrome reports serials as "04:a2:3b:1c:5d:80:00"; someone typing one in
    might write "04A23B1C5D8000" or "04-a2-3b...". All become "04A23B1C5D8000".
    """
    return re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()


def is_valid_serial(serial: str) -> bool:
    """NFC UIDs are 4, 7 or 10 bytes: 8, 14 or 20 hex digits."""
    return len(serial) in (8, 14, 20)


def find_by_tag(serial: str):
    """Returns the PIN whose record is linked to this tag, or None."""
    serial = normalize_serial(serial)
    if not serial:
        return None
    for pin, record in cs.employee_records.items():
        if record.get("nfc_tag") == serial:
            return pin
    return None


def link_tag(pin: str, serial: str) -> str:
    """Ties a tag to an employee. Replaces any tag they had before."""
    pin = cs.clean_pin(pin)
    record = cs.employee_records.get(pin)
    serial = normalize_serial(serial)

    if record is None:
        return cs.DOES_NOT_EXIST
    if not is_valid_serial(serial):
        return TAG_BAD_FORMAT

    existing = find_by_tag(serial)
    if existing is not None and existing != pin:
        return TAG_ALREADY_LINKED

    record["nfc_tag"] = serial
    return TAG_LINKED


def unlink_tag(pin: str) -> str:
    """Removes an employee's tag, e.g. when it's lost."""
    record = cs.employee_records.get(cs.clean_pin(pin))
    if record is None:
        return cs.DOES_NOT_EXIST
    record["nfc_tag"] = None
    return TAG_UNLINKED


def format_serial(serial: str) -> str:
    """"04A23B1C5D8000" -> "04:A2:3B:1C:5D:80:00" for display."""
    return ":".join(serial[i:i + 2] for i in range(0, len(serial), 2))


# ---------------------------------------------------------------------
# Admin page panel (rendered into the dashboard by app.py)
# ---------------------------------------------------------------------
def admin_panel(punches) -> Markup:
    """The "Link an NFC tag" form plus the list of linked tags."""
    options = "".join(
        f'<option value="{p["id"]}">{Markup.escape(p["name"])}</option>' for p in punches
    )
    linked = [
        (pin, rec) for pin, rec in sorted(cs.employee_records.items(), key=lambda kv: kv[1]["name"])
        if rec.get("nfc_tag")
    ]
    rows = "".join(
        f'<li><span class="nfc-name">{Markup.escape(rec["name"])}</span>'
        f'<span class="mono">{format_serial(rec["nfc_tag"])}</span>'
        f'<form class="inline" method="post" action="/nfc/unlink">'
        f'<input type="hidden" name="pin" value="{pin}">'
        f'<button class="unlink" type="submit">Unlink</button></form></li>'
        for pin, rec in linked
    ) or '<li class="sub">No tags linked yet.</li>'

    return Markup(f"""
  <form class="card-link" method="post" action="/nfc/link">
    <div class="card-link-text">
      <div class="card-link-title">Link an NFC tag</div>
      <p class="sub">Pick the employee, press <b>Read tag</b>, then hold their tag to the
        tablet. On a computer, type the tag's serial number instead.</p>
    </div>
    <select name="pin" required>
      <option value="" disabled selected>Employee…</option>{options}
    </select>
    <input type="text" name="serial" id="nfcLinkSerial" placeholder="Tag serial" autocomplete="off" required>
    <button type="button" id="nfcReadTag" class="nfc-read" hidden>Read tag</button>
    <button type="submit">Link tag</button>
    <ul class="nfc-list">{rows}</ul>
  </form>""")


# ---------------------------------------------------------------------
# Browser side: Web NFC on the kiosk and the admin page
# ---------------------------------------------------------------------
NFC_JS = r"""
(function () {
  const supported = "NDEFReader" in window;

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(r => r.json());
  }

  function injectStyle(css) {
    const s = document.createElement("style");
    s.textContent = css;
    document.head.appendChild(s);
  }

  // Starts Web NFC and hands every tag read to onTag(serial).
  // Chrome only allows scan() after a tap, unless permission was already
  // granted on an earlier visit, which is why startNfc() is also tried on
  // page load and just quietly fails the first time.
  async function startNfc(onTag, onError) {
    const reader = new NDEFReader();
    await reader.scan();
    reader.onreading = event => onTag(event.serialNumber || "");
    reader.onreadingerror = () => onError("Couldn't read that tag. Hold it still and try again.");
    return reader;
  }

  async function alreadyAllowed() {
    try {
      const p = await navigator.permissions.query({ name: "nfc" });
      return p.state === "granted";
    } catch (e) {
      return false;
    }
  }

  function whyNot() {
    if (!window.isSecureContext) return "NFC needs the kiosk opened over https://";
    return "NFC needs Chrome on an Android tablet with NFC turned on.";
  }

  /* ---------------- kiosk ---------------- */

  function setUpKiosk(mount) {
    injectStyle(`
      .nfc-bar { display: flex; align-items: center; gap: 12px; padding: 10px 14px;
        border: 1px dashed var(--line); border-radius: 12px; color: var(--muted); font-size: 14px;
        transition: border-color .2s, color .2s, background-color .2s; }
      .nfc-bar .nfc-icon { flex: 0 0 auto; font-size: 20px; line-height: 1; }
      .nfc-bar .nfc-text { flex: 1; }
      .nfc-bar.ready { color: var(--text); }
      .nfc-bar.ready .nfc-icon { color: var(--ok); }
      .nfc-bar.hit { border-style: solid; border-color: var(--accent); background-color: rgba(77,141,255,.1); }
      .nfc-bar button { border: 1px solid var(--accent); background: var(--accent); color: #fff;
        border-radius: 10px; padding: 8px 12px; font: inherit; font-weight: 600; cursor: pointer; }
    `);

    mount.innerHTML =
      '<div class="nfc-bar" id="nfcBar">' +
      '<span class="nfc-icon">&#x1F4F6;</span>' +
      '<span class="nfc-text" id="nfcText"></span>' +
      '<button type="button" id="nfcStart" hidden>Turn on NFC</button>' +
      '</div>';

    const bar = document.getElementById("nfcBar");
    const text = document.getElementById("nfcText");
    const btn = document.getElementById("nfcStart");

    if (!supported || !window.isSecureContext) {
      text.textContent = "NFC tags not available here. " + whyNot();
      return;
    }

    let busyUntil = 0;

    async function onTag(serial) {
      // A single tap is often read two or three times in a row; the
      // server also has a cooldown, this just avoids the extra requests.
      const now = Date.now();
      if (now < busyUntil) return;
      busyUntil = now + 1500;

      if (typeof kioskBusy === "function" && kioskBusy()) {
        post("/api/log", { type: "nfc_ignored", detail: { reason: "popup open" } }).catch(() => {});
        return;
      }

      bar.classList.add("hit");
      setTimeout(() => bar.classList.remove("hit"), 700);

      let data;
      try {
        data = await post("/api/nfc/tap", { serial });
      } catch (e) {
        data = { ok: false, message: "Kiosk is offline. Tell your site lead." };
      }
      showScanResult(data);
    }

    function onError(message) {
      showScanResult({ ok: false, message });
    }

    function ready() {
      btn.hidden = true;
      bar.classList.add("ready");
      text.textContent = "NFC is on. Tap your tag on the back of the tablet.";
    }

    async function start() {
      try {
        await startNfc(onTag, onError);
        ready();
      } catch (e) {
        btn.hidden = false;
        text.textContent = e.name === "NotAllowedError"
          ? "NFC permission was blocked. Allow it in Chrome's site settings."
          : "NFC is off. Tap the button to start it.";
      }
    }

    text.textContent = "NFC is off. Tap the button to start it.";
    btn.addEventListener("click", start);

    alreadyAllowed().then(ok => { if (ok) start(); else btn.hidden = false; });
  }

  /* ---------------- admin page ---------------- */

  function setUpAdmin(field, button) {
    injectStyle(`
      .nfc-read { background: transparent !important; color: var(--accent) !important; }
      .nfc-list { list-style: none; margin: 4px 0 0; padding: 0; width: 100%;
        display: flex; flex-direction: column; gap: 6px; font-size: 14px; }
      .nfc-list li { display: flex; align-items: center; gap: 12px; }
      .nfc-name { font-weight: 600; min-width: 140px; }
      /* the form's big blue button style shouldn't reach the small Unlink buttons */
      .nfc-list .unlink { background: none; border: 1px solid var(--line); color: var(--muted);
        padding: 3px 8px; font-size: 12px; font-weight: 400; border-radius: 6px; }
      .nfc-list .unlink:hover { color: var(--err); border-color: rgba(255,107,107,.5); }
    `);

    if (!supported || !window.isSecureContext) return;   // typing the serial still works
    button.hidden = false;

    button.addEventListener("click", async () => {
      button.textContent = "Hold tag to tablet…";
      try {
        await startNfc(serial => {
          field.value = serial;
          button.textContent = "Read tag";
          field.focus();
        }, message => { button.textContent = "Read tag"; alert(message); });
      } catch (e) {
        button.textContent = "Read tag";
        alert("Couldn't start NFC: " + (e.message || e.name));
      }
    });
  }

  const kioskMount = document.getElementById("nfcMount");
  if (kioskMount) setUpKiosk(kioskMount);

  const adminField = document.getElementById("nfcLinkSerial");
  const adminButton = document.getElementById("nfcReadTag");
  if (adminField && adminButton) setUpAdmin(adminField, adminButton);
})();
"""


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
def create_blueprint(clock_response, log_event):
    """Builds the NFC routes.

    clock_response(pin, method) and log_event(type, detail) come from app.py,
    so NFC taps flip the lever and get logged exactly like every other method.
    """
    bp = Blueprint("nfc", __name__)

    @bp.route("/nfc/nfc.js")
    def nfc_js():
        return Response(NFC_JS, mimetype="application/javascript")

    @bp.route("/api/nfc/tap", methods=["POST"])
    def api_tap():
        serial = normalize_serial((request.get_json(silent=True) or {}).get("serial"))
        pin = find_by_tag(serial)

        if pin is None:
            log_event("nfc_tap", {"serial": serial, "result": "unknown tag"})
            return jsonify({
                "ok": False,
                "message": "This tag isn't linked to anyone yet. See your site lead.",
            })

        now = time.monotonic()
        previous = recent_taps.get(pin)
        if previous and now - previous[0] < TAP_COOLDOWN_SECONDS:
            log_event("nfc_tap", {"serial": serial, "result": "repeat ignored"})
            return jsonify({**previous[1], "repeat": True})

        response = clock_response(pin, "NFC")
        log_event("nfc_tap", {
            "serial": serial,
            "result": response.get("action") or response.get("message"),
        })
        if response["ok"]:
            recent_taps[pin] = (now, response)
        return jsonify(response)

    @bp.route("/nfc/link", methods=["POST"])
    def link():
        pin = cs.clean_pin(request.form.get("pin"))
        result = link_tag(pin, request.form.get("serial"))
        log_event("nfc_link", {"pin": pin, "result": result})

        if result == TAG_LINKED:
            name = cs.employee_records[pin]["name"]
            return redirect(url_for("dashboard", notice=f"NFC tag linked to {name}.", kind="ok"))

        messages = {
            cs.DOES_NOT_EXIST: "Pick an employee first.",
            TAG_BAD_FORMAT: "That doesn't look like a tag serial number. Read the tag again.",
            TAG_ALREADY_LINKED: "That tag is already linked to someone else. Unlink it there first.",
        }
        return redirect(url_for("dashboard", notice=messages.get(result, result), kind="error"))

    @bp.route("/nfc/unlink", methods=["POST"])
    def unlink():
        pin = cs.clean_pin(request.form.get("pin"))
        record = cs.lookup(pin)
        unlink_tag(pin)
        log_event("nfc_unlink", {"pin": pin})
        if record is None:
            return redirect(url_for("dashboard"))
        return redirect(url_for("dashboard", notice=f"NFC tag removed from {record['name']}.", kind="ok"))

    return bp
