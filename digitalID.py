"""
Digital ID cards — photo, name, ID number, and a scannable barcode + QR code.

Plugs into app.py the same way nfctag.py does:

    import digitalID
    app.register_blueprint(digitalID.create_blueprint(log_event, BASE_CSS, SITE_LABEL))

Routes (all admin-side; the prototype has no login, same as /dashboard):

    GET  /digital-id/<pin>              view + edit one employee's digital ID
    POST /digital-id/<pin>/save         save name, title, ID number, photo
    POST /digital-id/<pin>/photo/remove remove the photo
    POST /digital-id/<pin>/new-number   issue a fresh random ID number
    POST /digital-id/find               scan a barcode -> open that employee's ID

How it hangs together with clock_system.py
------------------------------------------
The ID number on the card IS the employee's linked badge value
(record["badge"]). The barcode and QR code both encode that exact value, so
scanning a digital ID at the kiosk goes through the normal /api/badge/scan
path and clocks the person in or out. Nothing about the clock rules changes.

Because the card lives on the employee record, the photo and job title are
stored right on that record too (record["photo"], record["title"]). That way
they follow the employee when they swap their temporary code for their own PIN.

Limitation: an employee has ONE badge value. Editing the ID number here also
replaces any physical school card that was linked to them.
"""

import base64
import random

from flask import Blueprint, redirect, render_template_string, request, url_for
from markupsafe import Markup

import clock_system as cs

MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_NAME_LENGTH = 60
MAX_TITLE_LENGTH = 40


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _sniff_image_type(data):
    """Identify an image by its first bytes rather than trusting the upload's label."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _initials(name):
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0].upper() for p in parts[:2]) or "?"


def _clean_text(value, limit):
    return " ".join((value or "").split())[:limit]


def issue_id_number(pin):
    """Link a fresh, unused 6-digit ID number to this employee and return it."""
    for _ in range(1000):
        candidate = str(random.randint(100000, 999999))
        if cs.find_by_badge(candidate) is None:
            if cs.link_badge(pin, candidate) == cs.BADGE_LINKED:
                return candidate
    raise RuntimeError("Could not find a free ID number.")


def ensure_id_number(pin):
    """Return the employee's ID number, issuing one if they don't have a card yet."""
    record = cs.lookup(pin)
    if record is None:
        return None
    return record.get("badge") or issue_id_number(pin)


def admin_panel():
    """Small panel for the dashboard: scan a card to open its digital ID."""
    return Markup(
        '<form class="card-link" method="post" action="/digital-id/find">'
        '<div class="card-link-text">'
        '<div class="card-link-title">Look up a digital ID</div>'
        '<p class="sub">Scan a barcode to open that employee\'s digital ID, '
        'or use the Digital ID link in the table.</p>'
        '</div>'
        '<input type="text" name="badge" placeholder="Scan barcode here" '
        'autocomplete="off" required>'
        '<button type="submit">Open ID</button>'
        '</form>'
    )


# ---------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------

def create_blueprint(log_event, base_css="", site="Main Office"):
    bp = Blueprint("digital_id", __name__)

    def back(pin, message, kind):
        return redirect(url_for("digital_id.editor", pin=pin, notice=message, kind=kind))

    def to_dashboard(message, kind):
        return redirect(url_for("dashboard", notice=message, kind=kind))

    @bp.route("/digital-id/<pin>")
    def editor(pin):
        pin = cs.clean_pin(pin)
        record = cs.lookup(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")

        had_number = bool(record.get("badge"))
        id_number = ensure_id_number(pin)
        if not had_number:
            log_event("digital_id_issued", {"pin": pin, "id": id_number})

        return render_template_string(
            EDITOR_HTML,
            base_css=base_css,
            site=site,
            pin=pin,
            name=record["name"],
            title=record.get("title") or "",
            photo=record.get("photo") or "",
            initials=_initials(record["name"]),
            id_number=id_number,
            temporary=record["temporary"],
            notice=request.args.get("notice"),
            notice_kind=request.args.get("kind", "ok"),
            max_name=MAX_NAME_LENGTH,
            max_title=MAX_TITLE_LENGTH,
        )

    @bp.route("/digital-id/<pin>/save", methods=["POST"])
    def save(pin):
        pin = cs.clean_pin(pin)
        record = cs.lookup(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")

        name = _clean_text(request.form.get("name"), MAX_NAME_LENGTH)
        title = _clean_text(request.form.get("title"), MAX_TITLE_LENGTH)
        if not name:
            return back(pin, "Name can't be empty.", "error")

        # Validate everything that can fail BEFORE changing anything, so a
        # rejected save never half-applies.
        photo_uri = None
        upload = request.files.get("photo")
        if upload and upload.filename:
            data = upload.read(MAX_PHOTO_BYTES + 1)
            if len(data) > MAX_PHOTO_BYTES:
                return back(pin, "That photo is over 2 MB. Choose a smaller one.", "error")
            mime = _sniff_image_type(data)
            if mime is None:
                return back(pin, "Photo must be a PNG, JPEG, GIF or WebP image.", "error")
            photo_uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

        new_number = cs.normalize_badge(request.form.get("id_number"))
        if new_number != record.get("badge"):
            result = cs.link_badge(pin, new_number)
            if result != cs.BADGE_LINKED:
                messages = {
                    cs.BADGE_BAD_FORMAT: "ID number is empty or too long.",
                    cs.BADGE_ALREADY_LINKED: "That ID number belongs to someone else. Pick a different one.",
                }
                return back(pin, messages.get(result, result), "error")

        record["name"] = name
        record["title"] = title
        if photo_uri:
            record["photo"] = photo_uri

        log_event("digital_id_saved", {"pin": pin, "id": record.get("badge")})
        return back(pin, "Digital ID saved.", "ok")

    @bp.route("/digital-id/<pin>/photo/remove", methods=["POST"])
    def remove_photo(pin):
        pin = cs.clean_pin(pin)
        record = cs.lookup(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        record["photo"] = None
        log_event("digital_id_photo_removed", {"pin": pin})
        return back(pin, "Photo removed.", "ok")

    @bp.route("/digital-id/<pin>/new-number", methods=["POST"])
    def new_number(pin):
        pin = cs.clean_pin(pin)
        if cs.lookup(pin) is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        number = issue_id_number(pin)
        log_event("digital_id_reissued", {"pin": pin, "id": number})
        return back(pin, f"New ID number issued: {number}.", "ok")

    @bp.route("/digital-id/find", methods=["POST"])
    def find():
        badge = cs.normalize_badge(request.form.get("badge"))
        pin = cs.find_by_badge(badge)
        log_event("digital_id_find", {"badge": badge, "found": pin is not None})
        if pin is None:
            return to_dashboard("No employee has that ID number.", "error")
        return redirect(url_for("digital_id.editor", pin=pin))

    return bp


# ---------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------

EDITOR_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Digital ID — {{ name }}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jsbarcode/3.11.6/JsBarcode.all.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
{{ base_css|safe }}
  [hidden] { display: none !important; }
  body { padding: 24px; }
  .wrap { max-width: 980px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
  .head { display: flex; align-items: flex-end; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  .back { color: var(--muted); font-size: 14px; text-decoration: none; }
  .back:hover { color: var(--text); text-decoration: underline; }

  .notice { border-radius: 12px; padding: 12px 16px; font-size: 15px; font-weight: 600; }
  .notice.ok { background: rgba(62,207,142,.12); color: var(--ok); border: 1px solid rgba(62,207,142,.4); }
  .notice.error { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.4); }
  .notice.warn { background: rgba(245,196,81,.12); color: var(--warn); border: 1px solid rgba(245,196,81,.4); }

  .layout { display: grid; grid-template-columns: minmax(0, 420px) minmax(0, 1fr); gap: 28px; align-items: start; }

  /* ---------- the card ---------- */
  .idcard {
    width: 100%;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 18px;
    overflow: hidden;
  }
  .idcard-top {
    background: var(--accent);
    color: #fff;
    padding: 12px 18px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    font-weight: 600;
  }
  .idcard-top .kind { font-size: 13px; opacity: .85; }
  .idcard-main { display: flex; gap: 16px; padding: 18px; }
  .photo {
    flex: 0 0 112px;
    height: 140px;
    border-radius: 10px;
    background: var(--field);
    border: 1px solid var(--line);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--accent);
    font-size: 40px;
    font-weight: 700;
  }
  .photo img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .idinfo { min-width: 0; display: flex; flex-direction: column; justify-content: center; gap: 4px; }
  .id-name { font-size: 24px; font-weight: 700; line-height: 1.15; overflow-wrap: anywhere; }
  .id-title { font-size: 15px; color: var(--muted); min-height: 1.2em; overflow-wrap: anywhere; }
  .id-label { font-size: 12px; color: var(--muted); margin-top: 12px; }
  .id-number { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: .06em; overflow-wrap: anywhere; }

  .codes {
    background: #fff;
    color: #111;
    padding: 16px 18px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .barcode-box { flex: 1; min-width: 0; }
  #barcode { display: block; width: 100%; height: 72px; }
  .barcode-text { text-align: center; font-size: 13px; letter-spacing: .12em; margin-top: 4px; font-variant-numeric: tabular-nums; }
  #qr { flex: 0 0 auto; width: 104px; height: 104px; }
  #qr img, #qr canvas { width: 104px !important; height: 104px !important; display: block; }
  .codes-error { color: #b00020; font-size: 13px; }

  .card-actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  .hint { font-size: 13px; color: var(--muted); margin: 10px 0 0; }

  /* ---------- the form ---------- */
  .panel { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 20px; }
  .panel h2 { font-size: 18px; margin: 0 0 4px; }
  form.edit { display: flex; flex-direction: column; gap: 16px; margin-top: 16px; }
  label { display: flex; flex-direction: column; gap: 6px; font-size: 14px; font-weight: 600; }
  label .sub { font-weight: 400; }
  input[type="text"] {
    background: var(--field);
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: 10px;
    padding: 12px 14px;
    font-size: 16px;
    font-family: inherit;
  }
  input[type="file"] {
    background: var(--field);
    border: 1px dashed var(--line);
    color: var(--muted);
    border-radius: 10px;
    padding: 12px 14px;
    font-size: 14px;
    font-family: inherit;
  }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  input:focus { outline: none; border-color: var(--accent); }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .btn {
    background: transparent;
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: 10px;
    padding: 11px 16px;
    font-size: 15px;
    font-weight: 600;
    font-family: inherit;
    cursor: pointer;
    white-space: nowrap;
  }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn:hover:not(.primary) { border-color: var(--muted); }
  .btn.danger:hover { color: var(--err); border-color: rgba(255,107,107,.5); }

  @media (max-width: 820px) {
    body { padding: 14px; }
    .layout { grid-template-columns: 1fr; }
  }

  @media print {
    body { background: #fff; padding: 0; }
    .no-print { display: none !important; }
    .layout { display: block; }
    .idcard { border: 1px solid #999; width: 3.4in; break-inside: avoid; color: #111; background: #fff; }
    .photo, .id-title, .id-label { color: #333; }
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="head no-print">
    <div>
      <a class="back" href="/dashboard">&larr; Back to dashboard</a>
      <h1>Digital ID</h1>
      <p class="sub">{{ site }}</p>
    </div>
  </div>

  {% if notice %}
  <div class="notice no-print {{ 'error' if notice_kind == 'error' else 'ok' }}">{{ notice }}</div>
  {% endif %}

  {% if temporary %}
  <div class="notice warn no-print">
    This employee hasn't chosen a PIN yet. Their ID works for clocking in already, because it uses the ID number, not the PIN.
  </div>
  {% endif %}

  <div class="layout">

    <div>
      <div class="idcard" id="idcard">
        <div class="idcard-top">
          <span>{{ site }}</span>
          <span class="kind">Staff ID</span>
        </div>
        <div class="idcard-main">
          <div class="photo" id="photoBox">
            <img id="photoImg" alt="Photo of {{ name }}" {% if photo %}src="{{ photo }}"{% else %}hidden{% endif %}>
            <span id="photoInit" {% if photo %}hidden{% endif %}>{{ initials }}</span>
          </div>
          <div class="idinfo">
            <div class="id-name" id="cardName">{{ name }}</div>
            <div class="id-title" id="cardTitle">{{ title }}</div>
            <div class="id-label">ID number</div>
            <div class="id-number" id="cardNumber">{{ id_number }}</div>
          </div>
        </div>
        <div class="codes">
          <div class="barcode-box">
            <svg id="barcode" preserveAspectRatio="xMidYMid meet"></svg>
            <div class="barcode-text" id="barcodeText">{{ id_number }}</div>
          </div>
          <div id="qr"></div>
        </div>
      </div>
      <p class="hint no-print" id="previewHint" hidden>Preview only. Save your changes to make this ID number scannable.</p>
      <div class="card-actions no-print">
        <button class="btn" type="button" onclick="window.print()">Print or save as PDF</button>
      </div>
    </div>

    <div class="panel no-print">
      <h2>Edit ID</h2>
      <p class="sub">Changes show on the card as you type. Save to apply them.</p>

      <form class="edit" method="post" action="{{ url_for('digital_id.save', pin=pin) }}" enctype="multipart/form-data">
        <label>Name
          <input type="text" name="name" id="fName" value="{{ name }}" maxlength="{{ max_name }}" required autocomplete="off">
        </label>
        <label>Job title
          <input type="text" name="title" id="fTitle" value="{{ title }}" maxlength="{{ max_title }}" placeholder="Optional" autocomplete="off">
        </label>
        <label>ID number
          <input type="text" name="id_number" id="fNumber" value="{{ id_number }}" maxlength="64" required autocomplete="off">
          <span class="sub">Both codes on the card encode this number. The kiosk clocks the person in or out when it's scanned.</span>
        </label>
        <label>Photo
          <input type="file" name="photo" id="fPhoto" accept="image/png,image/jpeg,image/gif,image/webp">
          <span class="sub">PNG, JPEG, GIF or WebP, up to 2 MB.</span>
        </label>
        <div class="row">
          <button class="btn primary" type="submit">Save changes</button>
          <button class="btn danger" type="submit" form="removePhotoForm" {% if not photo %}disabled{% endif %}>Remove photo</button>
          <button class="btn" type="submit" form="newNumberForm"
                  onclick="return confirm('Issue a new ID number? Any printed or saved copy of the old one will stop working.');">New random number</button>
        </div>
      </form>

      <form id="removePhotoForm" method="post" action="{{ url_for('digital_id.remove_photo', pin=pin) }}"></form>
      <form id="newNumberForm" method="post" action="{{ url_for('digital_id.new_number', pin=pin) }}"></form>
    </div>

  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const SAVED_NUMBER = {{ id_number|tojson }};

// Same tidy-up clock_system.normalize_badge does, so the preview matches what gets stored.
function normalize(value) {
  return (value || "").trim().replace(/^\*+|\*+$/g, "").trim().toUpperCase();
}

function initialsOf(name) {
  const parts = (name || "").split(/\s+/).filter(Boolean).slice(0, 2);
  return parts.map(p => p[0].toUpperCase()).join("") || "?";
}

function renderCodes() {
  const value = normalize($("fNumber").value);
  const svg = $("barcode");
  const qr = $("qr");

  $("cardNumber").textContent = value || "—";
  $("barcodeText").textContent = value;
  $("previewHint").hidden = value === SAVED_NUMBER;

  svg.innerHTML = "";
  qr.innerHTML = "";
  if (!value) return;

  if (typeof JsBarcode === "undefined" || typeof QRCode === "undefined") {
    qr.innerHTML = '<span class="codes-error">Barcode library failed to load. Check the internet connection.</span>';
    return;
  }

  try {
    JsBarcode(svg, value, { format: "CODE128", displayValue: false, margin: 0, width: 2, height: 72 });
    // Make the bars scale to the card width instead of a fixed pixel size.
    if (!svg.getAttribute("viewBox")) {
      svg.setAttribute("viewBox", "0 0 " + parseFloat(svg.getAttribute("width")) + " " + parseFloat(svg.getAttribute("height")));
    }
    svg.removeAttribute("width");
    svg.removeAttribute("height");
  } catch (err) {
    qr.innerHTML = '<span class="codes-error">This number can\'t be shown as a barcode.</span>';
    return;
  }

  new QRCode(qr, { text: value, width: 208, height: 208, correctLevel: QRCode.CorrectLevel.M });
}

$("fName").addEventListener("input", () => {
  const v = $("fName").value.trim() || "Name";
  $("cardName").textContent = v;
  $("photoInit").textContent = initialsOf(v);
});
$("fTitle").addEventListener("input", () => { $("cardTitle").textContent = $("fTitle").value.trim(); });
$("fNumber").addEventListener("input", renderCodes);

$("fPhoto").addEventListener("change", () => {
  const file = $("fPhoto").files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $("photoImg").src = reader.result;
    $("photoImg").hidden = false;
    $("photoInit").hidden = true;
  };
  reader.readAsDataURL(file);
});

renderCodes();
</script>
</body>
</html>
"""
