"""
Staff admin: add staff from a spreadsheet, and edit one employee by hand.

Plugs into app.py the same way nfctag.py and digitalID.py do:

    import staffadmin
    app.register_blueprint(staffadmin.create_blueprint(
        log_event, BASE_CSS, SITE_LABEL, on_pin_moved, on_status_corrected))

Routes (admin side; the prototype has no login, same as /admin):

    GET  /admin/staff-template.csv          the blank spreadsheet template
    POST /admin/import                      upload a sheet, or paste a Google
                                            Sheets link -> preview page
    POST /admin/import/<token>/confirm      actually add the previewed rows
    GET  /admin/import/<token>/codes.csv    the temporary codes just issued

    GET  /admin/employee/<pin>              edit one employee
    POST /admin/employee/<pin>/details      name, job title, ID card number
    POST /admin/employee/<pin>/pin          set a new PIN
    POST /admin/employee/<pin>/reset-pin    issue a fresh temporary code
    POST /admin/employee/<pin>/status       correct clocked in / out
    POST /admin/employee/<pin>/forget-phone forget one remembered phone
    POST /admin/employee/<pin>/forget-all   forget all their phones

The spreadsheet
---------------
Columns (header row required, any order, extra columns ignored):

    Name            required
    Job title       optional
    ID card number  optional: the barcode on their school ID card

Nobody's PIN goes in the sheet. Each imported person gets a temporary code,
listed after the import (and downloadable as a CSV) so they can be handed
out; at the kiosk each person then chooses their own PIN, as usual.

A row whose name already belongs to an employee is skipped, so an updated
sheet can be uploaded again without making duplicates. A name listed twice
in the sheet is only added once.

Google Sheets
-------------
- "Download template" gives a CSV; in Google Sheets use File -> Import ->
  Upload to turn it into a Google Sheet.
- Easier for everyone: make ONE template Google Sheet, share it as
  "Anyone with the link: Viewer", and start app.py with
      GOOGLE_TEMPLATE_URL=<its link>
  The admin page then shows "Make a copy in Google Sheets", which opens
  Google's "Make a copy" screen for that template.
- To import, either download the sheet (File -> Download -> .csv or .xlsx)
  and upload it, or paste the sheet's link. Pasting needs the sheet shared
  as "Anyone with the link" and this computer online.
"""

import csv
import io
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime

from flask import Blueprint, Response, redirect, render_template_string, request, url_for
from markupsafe import Markup

import clock_system as cs
import nfctag

TEMPLATE_COLUMNS = ["Name", "Job title", "ID card number"]

# Header spellings we accept for each column, compared lowercase with
# punctuation and extra spaces removed.
COLUMN_ALIASES = {
    "name": {"name", "full name", "staff name", "employee", "employee name"},
    "title": {"job title", "title", "role", "position", "job"},
    "badge": {"id card number", "id card", "id number", "card", "card number",
              "badge", "badge number", "barcode", "id"},
}

MAX_NAME_LENGTH = 60
MAX_TITLE_LENGTH = 40
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000

# Previews waiting for "Add these staff", and finished imports' code lists.
# token -> {"created": time.time(), ...}. Old ones are dropped after an hour.
pending_imports = {}
finished_imports = {}
KEEP_SECONDS = 3600


# ---------------------------------------------------------------------
# Reading a spreadsheet
# ---------------------------------------------------------------------
class SheetError(ValueError):
    """A problem with the uploaded sheet, worded for the admin."""


def _tidy(text):
    return " ".join(str(text if text is not None else "").split())


def _name_key(name):
    """Names count as the same person regardless of case and spacing."""
    return _tidy(name).casefold()


def _header_key(text):
    return re.sub(r"[^a-z0-9 ]", "", _tidy(text).lower()).strip()


def rows_from_csv_bytes(data):
    for encoding in ("utf-8-sig", "cp1252"):      # Excel on Windows saves cp1252
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SheetError("Couldn't read that file as text. Save it as CSV (UTF-8) and try again.")
    return list(csv.reader(io.StringIO(text)))


def rows_from_xlsx_bytes(data):
    try:
        import openpyxl
    except ImportError:
        raise SheetError("Reading .xlsx files needs the openpyxl package (pip install openpyxl). "
                         "Or download the sheet as .csv instead.")
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise SheetError("That .xlsx file couldn't be opened. Is it a real Excel/Google Sheets file?")
    sheet = book.worksheets[0]
    return [["" if v is None else v for v in row] for row in sheet.iter_rows(values_only=True)]


def google_csv_url(link):
    """Turn any Google Sheets link into its CSV-download link (same tab)."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", link or "")
    if not m:
        raise SheetError("That doesn't look like a Google Sheets link.")
    gid = re.search(r"[#?&]gid=(\d+)", link)
    url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    return url + (f"&gid={gid.group(1)}" if gid else "")


def rows_from_google_link(link):
    url = google_csv_url(link)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = resp.read(MAX_UPLOAD_BYTES + 1)
            kind = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise SheetError("Google refused to share that sheet "
                         f"(error {e.code}). Set sharing to 'Anyone with the link'.")
    except (urllib.error.URLError, TimeoutError):
        raise SheetError("Couldn't reach Google Sheets. Is this computer online? "
                         "You can also download the sheet as .csv and upload it.")
    if "text/html" in kind:
        # Private sheets answer with a sign-in page instead of the data.
        raise SheetError("That sheet isn't shared. In Google Sheets: Share -> General access -> "
                         "'Anyone with the link', then try again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise SheetError("That sheet is too big (over 2 MB).")
    return rows_from_csv_bytes(data)


def find_columns(header):
    """Map our fields to column positions in the header row."""
    keys = [_header_key(h) for h in header]
    found = {}
    for field, aliases in COLUMN_ALIASES.items():
        for i, k in enumerate(keys):
            if k in aliases:
                found[field] = i
                break
    if "name" not in found:
        raise SheetError("The first row needs a 'Name' column. Start from the template so the "
                         f"columns are right: {', '.join(TEMPLATE_COLUMNS)}.")
    return found


def build_preview(rows):
    """Decide what would happen to every row, without changing anything.

    Returns a list of {"line", "name", "title", "badge", "action", "note"}
    where action is "add", "skip" or "error".
    """
    # Keep each row's real line number (as the spreadsheet shows it) so the
    # preview's "Row" column points at the right row, then drop blank rows.
    numbered = [(n, r) for n, r in enumerate(rows, start=1) if any(_tidy(c) for c in r)]
    if not numbered:
        raise SheetError("That sheet is empty.")
    header_line, header = numbered[0]
    cols = find_columns(header)
    if len(numbered) - 1 > MAX_ROWS:
        raise SheetError(f"That sheet has over {MAX_ROWS} rows.")

    def cell(row, field):
        i = cols.get(field)
        return _tidy(row[i]) if i is not None and i < len(row) else ""

    existing_names = {_name_key(r["name"]) for r in cs.employee_records.values()}
    seen_names, seen_badges = set(), set()
    preview = []
    for line, row in numbered[1:]:
        name = cell(row, "name")
        title = cell(row, "title")[:MAX_TITLE_LENGTH]
        badge = cs.normalize_badge(cell(row, "badge"))
        # Spreadsheets love to turn 100234 into "100234.0".
        if re.fullmatch(r"\d+\.0", badge):
            badge = badge[:-2]
        item = {"line": line, "name": name, "title": title, "badge": badge,
                "action": "add", "note": ""}
        key = _name_key(name)

        if not name:
            item.update(action="error", note="No name")
        elif len(name) > MAX_NAME_LENGTH:
            item.update(action="error", note=f"Name is over {MAX_NAME_LENGTH} characters")
        elif key in existing_names:
            item.update(action="skip", note="Already registered")
        elif key in seen_names:
            item.update(action="skip", note="Listed twice in the sheet; added once")
        elif badge and len(badge) > cs.MAX_BADGE_LENGTH:
            item.update(action="error", note="ID card number is too long")
        elif badge and cs.find_by_badge(badge):
            owner = cs.employee_records[cs.find_by_badge(badge)]["name"]
            item.update(action="error", note=f"ID card already belongs to {owner}")
        elif badge and badge in seen_badges:
            item.update(action="error", note="Same ID card number as an earlier row")

        if item["action"] == "add":
            seen_names.add(key)
            if badge:
                seen_badges.add(badge)
        preview.append(item)
    return preview


def _prune():
    cutoff = time.time() - KEEP_SECONDS
    for store in (pending_imports, finished_imports):
        for token in [t for t, v in store.items() if v["created"] < cutoff]:
            del store[token]


def _fmt_time(ts):
    if not ts:
        return "earlier"
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}, " + d.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------
# Admin-page panel
# ---------------------------------------------------------------------
def admin_panel():
    template_url = os.environ.get("GOOGLE_TEMPLATE_URL", "").strip()
    m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", template_url)
    copy_link = (
        f'<a class="sheet-btn" href="https://docs.google.com/spreadsheets/d/{m.group(1)}/copy" '
        f'target="_blank" rel="noopener">Make a copy in Google Sheets</a>'
        if m else
        '<a class="sheet-btn" href="https://sheets.new" target="_blank" rel="noopener" '
        'title="Then File → Import → Upload the template">New Google Sheet</a>'
    )
    return Markup(f"""
  <div class="card-link import-panel">
    <div class="card-link-text">
      <div class="card-link-title">Add staff from a spreadsheet</div>
      <p class="sub">Columns: <b>Name</b>, <b>Job title</b>, <b>ID card number</b> (only Name is
        required). You'll see a preview before anyone is added.
        {"" if m else "For Google Sheets: open a new sheet, then File → Import → Upload the template."}</p>
      <div class="sheet-links">
        <a class="sheet-btn" href="/admin/staff-template.csv">Download template</a>
        {copy_link}
      </div>
    </div>
    <form class="import-form" method="post" action="/admin/import" enctype="multipart/form-data">
      <input type="file" name="sheet" accept=".csv,.xlsx,text/csv">
      <span class="sub or">or</span>
      <input type="url" name="google_link" placeholder="Paste a Google Sheets link" autocomplete="off">
      <button type="submit">Preview import</button>
    </form>
  </div>
  <style>
    .import-panel {{ align-items: flex-start; }}
    .sheet-links {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
    .sheet-btn {{ font-size: 13px; font-weight: 600; color: var(--accent); text-decoration: none;
      border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; }}
    .sheet-btn:hover {{ border-color: var(--accent); }}
    .import-form {{ display: flex; flex-direction: column; gap: 8px; flex: 1 1 300px; }}
    .import-form input[type="file"] {{ background: var(--field); border: 1px dashed var(--line);
      color: var(--muted); border-radius: 10px; padding: 10px; font: inherit; font-size: 14px; }}
    .import-form input[type="url"] {{ background: var(--field); border: 1px solid var(--line);
      color: var(--text); border-radius: 10px; padding: 11px 14px; font: inherit; font-size: 15px; }}
    .import-form .or {{ text-align: center; }}
    .import-form button {{ align-self: flex-end; }}
  </style>""")


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
def create_blueprint(log_event, base_css, site, on_pin_moved, on_status_corrected):
    """on_pin_moved(old, new): app.py moves its own per-PIN state (last
    punch, double-clock guard). on_status_corrected(pin): app.py notes an
    admin correction on the dashboard."""
    bp = Blueprint("staffadmin", __name__)

    def page(template, **ctx):
        return render_template_string(template, base_css=base_css, site=site, **ctx)

    def to_dashboard(message, kind="ok"):
        return redirect(url_for("dashboard", notice=message, kind=kind))

    def to_editor(pin, message, kind="ok"):
        return redirect(url_for("staffadmin.editor", pin=pin, notice=message, kind=kind))

    # ---- spreadsheet import -------------------------------------------

    @bp.route("/admin/staff-template.csv")
    def template_csv():
        out = io.StringIO()
        csv.writer(out).writerow(TEMPLATE_COLUMNS)
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=staff-template.csv"})

    @bp.route("/admin/import", methods=["POST"])
    def import_preview():
        _prune()
        upload = request.files.get("sheet")
        link = (request.form.get("google_link") or "").strip()
        try:
            if upload and upload.filename:
                data = upload.read(MAX_UPLOAD_BYTES + 1)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise SheetError("That file is over 2 MB.")
                if upload.filename.lower().endswith(".xlsx"):
                    rows = rows_from_xlsx_bytes(data)
                elif upload.filename.lower().endswith((".csv", ".txt")):
                    rows = rows_from_csv_bytes(data)
                else:
                    raise SheetError("Upload a .csv or .xlsx file. In Google Sheets: File → Download.")
                source = upload.filename
            elif link:
                rows = rows_from_google_link(link)
                source = "Google Sheets link"
            else:
                raise SheetError("Choose a file or paste a Google Sheets link first.")
            preview = build_preview(rows)
        except SheetError as e:
            log_event("staff_import_refused", {"reason": str(e)})
            return to_dashboard(str(e), "error")

        token = secrets.token_urlsafe(12)
        pending_imports[token] = {"created": time.time(), "rows": preview, "source": source}
        log_event("staff_import_preview", {"rows": len(preview),
                                           "add": sum(r["action"] == "add" for r in preview)})
        return page(PREVIEW_HTML, token=token, rows=preview, source_label=source,
                    counts={k: sum(r["action"] == k for r in preview) for k in ("add", "skip", "error")})

    @bp.route("/admin/import/<token>/confirm", methods=["POST"])
    def import_confirm(token):
        pending = pending_imports.pop(token, None)
        if pending is None:
            return to_dashboard("That preview has expired or was already imported. Upload the sheet again.",
                                "error")
        # Re-check every row: staff may have changed since the preview.
        rows = build_preview([["Name", "Job title", "ID card number"]] +
                             [[r["name"], r["title"], r["badge"]] for r in pending["rows"]
                              if r["action"] == "add"])
        added = []
        for r in rows:
            if r["action"] != "add":
                continue
            code = cs.register_employee(r["name"])
            record = cs.employee_records[code]
            if r["title"]:
                record["title"] = r["title"]
            if r["badge"]:
                cs.link_badge(code, r["badge"])
            added.append({"name": r["name"], "title": r["title"], "badge": r["badge"], "code": code})

        token2 = secrets.token_urlsafe(12)
        finished_imports[token2] = {"created": time.time(), "added": added}
        log_event("staff_imported", {"added": len(added), "source": pending["source"]})
        return page(RESULT_HTML, token=token2, added=added,
                    dropped=len([r for r in pending["rows"] if r["action"] == "add"]) - len(added))

    @bp.route("/admin/import/<token>/codes.csv")
    def import_codes(token):
        done = finished_imports.get(token)
        if done is None:
            return to_dashboard("That list has expired. The codes are still on the dashboard table.", "error")
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Name", "Temporary code", "Job title", "ID card number"])
        for a in done["added"]:
            w.writerow([a["name"], a["code"], a["title"], a["badge"]])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=temporary-codes.csv"})

    # ---- edit one employee --------------------------------------------

    def load(pin):
        pin = cs.clean_pin(pin)
        return pin, cs.lookup(pin)

    @bp.route("/admin/employee/<pin>")
    def editor(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        phones = [{**ph, "when": _fmt_time(ph["added"])} for ph in nfctag.devices_for(pin)]
        return page(EDIT_HTML, pin=pin, record=record, phones=phones,
                    since=_fmt_time(record.get("since")) if record.get("since") else None,
                    notice=request.args.get("notice"), notice_kind=request.args.get("kind", "ok"),
                    min_len=cs.MIN_PIN_LENGTH, max_len=cs.MAX_PIN_LENGTH,
                    max_name=MAX_NAME_LENGTH, max_title=MAX_TITLE_LENGTH)

    @bp.route("/admin/employee/<pin>/details", methods=["POST"])
    def save_details(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        name = _tidy(request.form.get("name"))[:MAX_NAME_LENGTH]
        title = _tidy(request.form.get("title"))[:MAX_TITLE_LENGTH]
        badge = cs.normalize_badge(request.form.get("badge"))
        if not name:
            return to_editor(pin, "Name can't be empty.", "error")

        # Card first: it's the only part that can be refused.
        if badge != (record.get("badge") or ""):
            if not badge:
                cs.unlink_badge(pin)
            else:
                result = cs.link_badge(pin, badge)
                if result != cs.BADGE_LINKED:
                    owner = cs.find_by_badge(badge)
                    msg = (f"That ID card belongs to {cs.employee_records[owner]['name']}."
                           if owner else "That ID card number isn't valid.")
                    return to_editor(pin, msg + " Nothing was saved.", "error")
        record["name"] = name
        record["title"] = title
        log_event("staff_edited", {"pin": pin})
        return to_editor(pin, "Saved.")

    @bp.route("/admin/employee/<pin>/pin", methods=["POST"])
    def change_pin(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        new = cs.clean_pin(request.form.get("new_pin"))
        if new == pin:
            return to_editor(pin, "That's already their PIN.", "error")
        result = cs.set_custom_pin(pin, new)
        if result != cs.PIN_SET:
            messages = {
                cs.PIN_BAD_FORMAT: f"A PIN must be {cs.MIN_PIN_LENGTH}-{cs.MAX_PIN_LENGTH} digits.",
                cs.PIN_TAKEN: "Someone else already has that PIN.",
            }
            return to_editor(pin, messages.get(result, result), "error")
        on_pin_moved(pin, new)
        log_event("staff_pin_changed", {"from": pin, "to": new})
        return to_editor(new, f"PIN changed to {new}. Their remembered phones keep working.")

    @bp.route("/admin/employee/<pin>/reset-pin", methods=["POST"])
    def reset_pin(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        new = cs.reset_to_temporary_code(pin)
        on_pin_moved(pin, new)
        log_event("staff_pin_reset", {"from": pin, "to": new})
        return to_editor(new, f"New temporary code: {new}. Give it to {record['name']}; "
                              "they'll choose a new PIN the next time they use it at the kiosk.")

    @bp.route("/admin/employee/<pin>/status", methods=["POST"])
    def correct_status(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        want = request.form.get("status")
        if want not in ("clocked_in", "clocked_out"):
            return to_editor(pin, "Pick clocked in or clocked out.", "error")
        if record["status"] == want:
            return to_editor(pin, "They're already marked that way.", "error")
        record["status"] = want
        record["since"] = time.time() if want == "clocked_in" else None
        on_status_corrected(pin)
        log_event("staff_status_corrected", {"pin": pin, "status": want})
        return to_editor(pin, "Marked as " + ("clocked in." if want == "clocked_in" else "clocked out."))

    @bp.route("/admin/employee/<pin>/forget-phone", methods=["POST"])
    def forget_phone(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        device = request.form.get("device", "")
        # Only forget a phone that really belongs to this employee.
        if nfctag.remembered_devices.get(device) != pin or not nfctag.forget_device(device):
            return to_editor(pin, "That phone was already forgotten.", "error")
        log_event("staff_phone_forgotten", {"pin": pin})
        return to_editor(pin, "Phone forgotten. Next time they tap the tag with it, they'll enter their PIN.")

    @bp.route("/admin/employee/<pin>/forget-all", methods=["POST"])
    def forget_all(pin):
        pin, record = load(pin)
        if record is None:
            return to_dashboard("That employee doesn't exist anymore.", "error")
        n = nfctag.forget_devices_for(pin)
        log_event("staff_phone_forgotten", {"pin": pin, "count": n})
        return to_editor(pin, f"Forgot {n} phone{'s' if n != 1 else ''}.")

    return bp


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------
PAGE_CSS = """
  body { padding: 24px; }
  .wrap { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  h2 { font-size: 17px; margin: 0 0 4px; }
  .back { color: var(--muted); font-size: 14px; text-decoration: none; }
  .back:hover { color: var(--text); text-decoration: underline; }
  .panel { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 18px 20px; }
  .notice { border-radius: 12px; padding: 12px 16px; font-size: 15px; font-weight: 600; }
  .notice.ok { background: rgba(62,207,142,.12); color: var(--ok); border: 1px solid rgba(62,207,142,.4); }
  .notice.error { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.4); }
  .notice.warn { background: rgba(245,196,81,.12); color: var(--warn); border: 1px solid rgba(245,196,81,.4); }
  label { display: flex; flex-direction: column; gap: 6px; font-size: 14px; font-weight: 600; }
  input[type="text"] { background: var(--field); border: 1px solid var(--line); color: var(--text);
    border-radius: 10px; padding: 11px 14px; font-size: 15px; font-family: inherit; }
  input:focus { outline: none; border-color: var(--accent); }
  .btn { background: transparent; border: 1px solid var(--line); color: var(--text); border-radius: 10px;
    padding: 10px 16px; font-size: 15px; font-weight: 600; font-family: inherit; cursor: pointer;
    white-space: nowrap; text-decoration: none; display: inline-block; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.small { padding: 5px 10px; font-size: 13px; }
  .btn.danger:hover { color: var(--err); border-color: rgba(255,107,107,.5); }
  .btn:disabled { opacity: .4; cursor: default; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 12px; font-size: 14px; border-bottom: 1px solid #242e3f; }
  th { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
  .mono { font-variant-numeric: tabular-nums; }
  .pill { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .03em; }
  .pill.add { background: rgba(62,207,142,.14); color: var(--ok); }
  .pill.skip { background: rgba(147,160,181,.16); color: var(--muted); }
  .pill.error { background: rgba(255,107,107,.14); color: var(--err); }
  .code { font-size: 20px; font-weight: 700; letter-spacing: .12em; color: var(--accent); }
  @media print { .no-print { display: none !important; } body { background: #fff; color: #111; }
    .panel { border: none; } .code { color: #111; } }
"""

PREVIEW_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import preview</title>
<style>{{ base_css|safe }}""" + PAGE_CSS + """</style></head>
<body><div class="wrap">
  <div><a class="back" href="/admin">&larr; Back to dashboard (cancel)</a>
    <h1>Import preview</h1><p class="sub">{{ source_label }} · nothing has been added yet</p></div>

  <div class="notice {{ 'ok' if counts.add else 'warn' }}">
    {{ counts.add }} will be added{% if counts.skip %}, {{ counts.skip }} skipped{% endif %}
    {%- if counts.error %}, {{ counts.error }} with problems (left out){% endif %}.
  </div>

  <div class="panel">
    <table>
      <thead><tr><th>Row</th><th>Name</th><th>Job title</th><th>ID card</th><th></th><th></th></tr></thead>
      <tbody>
      {% for r in rows %}
        <tr><td class="mono">{{ r.line }}</td><td>{{ r.name or '—' }}</td><td>{{ r.title or '—' }}</td>
          <td class="mono">{{ r.badge or '—' }}</td>
          <td><span class="pill {{ r.action }}">{{ {'add': 'Add', 'skip': 'Skip', 'error': 'Problem'}[r.action] }}</span></td>
          <td class="sub">{{ r.note }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <form class="row" method="post" action="/admin/import/{{ token }}/confirm">
    <a class="btn" href="/admin">Cancel</a>
    <button class="btn primary" type="submit" {% if not counts.add %}disabled{% endif %}>
      Add {{ counts.add }} staff member{{ 's' if counts.add != 1 }}</button>
  </form>
  {% if counts.error %}<p class="sub">Rows with problems are left out. Fix them in the sheet and upload it again;
    people already added will be skipped.</p>{% endif %}
</div></body></html>
"""

RESULT_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Staff added</title>
<style>{{ base_css|safe }}""" + PAGE_CSS + """</style></head>
<body><div class="wrap">
  <div class="no-print"><a class="back" href="/admin">&larr; Back to dashboard</a></div>
  <h1>{{ added|length }} staff member{{ 's' if added|length != 1 }} added</h1>
  {% if dropped %}<div class="notice warn no-print">{{ dropped }} row(s) from the preview were left out because
    something changed in the meantime (e.g. that name or card was added by someone else).</div>{% endif %}
  <p class="sub">Give each person their temporary code. At the kiosk they enter it once and choose their own PIN.
    Save or print this list now: the codes won't be shown together again.</p>

  <div class="row no-print">
    <a class="btn primary" href="/admin/import/{{ token }}/codes.csv">Download codes (.csv)</a>
    <button class="btn" type="button" onclick="window.print()">Print</button>
  </div>

  <div class="panel">
    <table>
      <thead><tr><th>Name</th><th>Temporary code</th><th>Job title</th><th>ID card</th></tr></thead>
      <tbody>
      {% for a in added %}
        <tr><td>{{ a.name }}</td><td class="code mono">{{ a.code }}</td>
          <td>{{ a.title or '—' }}</td><td class="mono">{{ a.badge or '—' }}</td></tr>
      {% else %}
        <tr><td colspan="4" class="sub">Nobody was added.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div></body></html>
"""

EDIT_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit {{ record.name }}</title>
<style>{{ base_css|safe }}""" + PAGE_CSS + """
  .phones { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .phones li { display: flex; align-items: center; gap: 12px; background: var(--field);
    border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
  .phones .what { flex: 1; }
  .status-pill { display: inline-block; padding: 4px 11px; border-radius: 999px; font-size: 12px; font-weight: 700;
    text-transform: uppercase; }
  .status-pill.in { background: rgba(62,207,142,.14); color: var(--ok); }
  .status-pill.out { background: rgba(122,135,157,.16); color: var(--off); }
</style></head>
<body><div class="wrap">
  <div><a class="back" href="/admin">&larr; Back to dashboard</a>
    <h1>{{ record.name }}</h1>
    <p class="sub">{{ 'Temporary code' if record.temporary else 'PIN' }} <b class="mono">{{ pin }}</b>
      {% if record.temporary %} · hasn't chosen a PIN yet{% endif %}</p></div>

  {% if notice %}<div class="notice {{ 'error' if notice_kind == 'error' else 'ok' }}">{{ notice }}</div>{% endif %}

  <form class="panel" method="post" action="/admin/employee/{{ pin }}/details">
    <h2>Details</h2>
    <div class="grid" style="margin-top:12px">
      <label>Name <input type="text" name="name" value="{{ record.name }}" maxlength="{{ max_name }}" required></label>
      <label>Job title <input type="text" name="title" value="{{ record.title or '' }}" maxlength="{{ max_title }}" placeholder="Optional"></label>
      <label>ID card number <input type="text" name="badge" value="{{ record.badge or '' }}" placeholder="None. Scan a card here"
          autocomplete="off"></label>
    </div>
    <div class="row" style="margin-top:14px"><button class="btn primary" type="submit">Save details</button>
      <span class="sub">Clear the ID card box to unlink their card.</span></div>
  </form>

  <div class="panel">
    <h2>Remembered phones</h2>
    <p class="sub">Phones that clock this person in with one tap of the NFC tag. Forget the old one when they switch
      phones; their new phone asks for their PIN once.</p>
    {% if phones %}
    <ul class="phones">
      {% for ph in phones %}
      <li><span class="what"><b>{{ ph.kind }}</b> <span class="sub">· remembered {{ ph.when }}</span></span>
        <form method="post" action="/admin/employee/{{ pin }}/forget-phone">
          <input type="hidden" name="device" value="{{ ph.id }}">
          <button class="btn small danger" type="submit">Forget</button></form></li>
      {% endfor %}
    </ul>
    {% if phones|length > 1 %}
    <form method="post" action="/admin/employee/{{ pin }}/forget-all" style="margin-top:10px"
          onsubmit='return confirm({{ ("Forget all %d phones for %s?" % (phones|length, record.name))|tojson }})'>
      <button class="btn small danger" type="submit">Forget all phones</button></form>
    {% endif %}
    {% else %}<p class="sub" style="margin-top:8px">No phones remembered.</p>{% endif %}
  </div>

  <div class="panel">
    <h2>PIN</h2>
    <div class="row" style="margin-top:12px">
      <form class="row" method="post" action="/admin/employee/{{ pin }}/pin">
        <label>New PIN <input type="text" name="new_pin" inputmode="numeric" pattern="[0-9]{{ '{' }}{{ min_len }},{{ max_len }}{{ '}' }}"
            maxlength="{{ max_len }}" placeholder="{{ min_len }}-{{ max_len }} digits" autocomplete="off" required></label>
        <button class="btn" type="submit">Set PIN</button>
      </form>
      <form method="post" action="/admin/employee/{{ pin }}/reset-pin"
            onsubmit='return confirm({{ ("Give " ~ record.name ~ " a new temporary code? Their current PIN stops working.")|tojson }})'>
        <button class="btn" type="submit">Forgot PIN: issue temporary code</button></form>
    </div>
  </div>

  <div class="panel">
    <h2>Clock status</h2>
    <p class="sub" style="margin:6px 0 12px">Currently
      <span class="status-pill {{ 'in' if record.status == 'clocked_in' else 'out' }}">
        {{ 'On shift' if record.status == 'clocked_in' else 'Clocked out' }}</span>
      {% if since %} since {{ since }}{% endif %}.
      Use this to fix a mistake, e.g. someone who forgot to clock out.</p>
    <form method="post" action="/admin/employee/{{ pin }}/status"
          onsubmit='return confirm({{ ("Change " ~ record.name ~ "\u2019s clock status?")|tojson }})'>
      {% if record.status == 'clocked_in' %}
        <input type="hidden" name="status" value="clocked_out"><button class="btn" type="submit">Mark as clocked out</button>
      {% else %}
        <input type="hidden" name="status" value="clocked_in"><button class="btn" type="submit">Mark as clocked in</button>
      {% endif %}
    </form>
  </div>

  <div class="row">
    <a class="btn" href="/digital-id/{{ pin }}">Digital ID</a>
    <a class="btn danger" href="/admin/remove/{{ pin }}">Remove employee…</a>
  </div>
</div></body></html>
"""
