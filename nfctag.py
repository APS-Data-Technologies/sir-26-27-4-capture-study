import nfc
import binascii

def on_connect(tag):
    """Called automatically when a tag is detected."""
    print(f"Tag detected: {tag}")
    print(f"Tag type: {tag.type}")
    print(f"Identifier (UID): {binascii.hexlify(tag.identifier).decode()}")

    # If the tag supports NDEF (most common format for stored data)
    if tag.ndef:
        print(f"NDEF records found: {len(tag.ndef.records)}")
        for record in tag.ndef.records:
            print(f"  Type: {record.type}")
            print(f"  Data: {record.data}")
    else:
        print("No NDEF data on this tag (raw tag only).")

    return True  # returning True keeps the tag "activated"


def read_nfc_tag():
    # Common device paths:
    #   'usb'          -> auto-detect a USB NFC reader
    #   'usb:04e6:5591' -> specific vendor:product ID
    #   'tty:USB0:pn532' -> a PN532 board on a serial port
    clf = nfc.ContactlessFrontend('usb')

    print("Waiting for an NFC tag... (present a tag to the reader)")

    clf.connect(rdwr={'on-connect': on_connect})

    clf.close()


if __name__ == "__main__":
    read_nfc_tag()
"""
NFC Clock-In System
--------------------
Reads NFC tag/card taps and logs worker clock-in/clock-out events.
 
Requirements:
    pip install nfcpy
 
Hardware:
    Works with most common USB NFC readers supported by nfcpy, e.g.:
    - ACR122U
    - SCL3711
    - PN532 (in USB mode)
    See https://nfcpy.readthedocs.io/en/latest/topics/get-started.html
    for the exact device path if auto-detection ("usb") doesn't work.
 
Setup:
    1. Create a file called workers.csv in this same folder with two columns:
           uid,name
           04a1b2c3,Jane Smith
           04d4e5f6,John Doe
       (Find each badge's UID by running this script and tapping it once —
        the UID will be printed even if the worker isn't recognized yet.)
 
    2. Run this script. It will keep listening for taps until you press Ctrl+C.
 
    3. Clock-in/out events get appended to attendance_log.csv with a
       timestamp. Each tap toggles the worker's status (in -> out -> in ...).
"""
 
import csv
import os
from datetime import datetime
 
import nfc
 
WORKERS_FILE = "workers.csv"
LOG_FILE = "attendance_log.csv"
 
 
def load_workers(path):
    """Load UID -> name mapping from a CSV file."""
    workers = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                workers[row["uid"].strip().lower()] = row["name"].strip()
    return workers
 
 
def get_last_status(log_path, uid):
    """Look at the log file to find the worker's last status (in/out)."""
    if not os.path.exists(log_path):
        return "out"  # nobody has clocked in yet, so next action is "in"
 
    last_status = "out"
    with open(log_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["uid"] == uid:
                last_status = row["status"]
    return last_status
 
 
def log_event(log_path, uid, name, status):
    """Append a clock-in/out event to the log file."""
    file_exists = os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "uid", "name", "status"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "uid": uid,
            "name": name,
            "status": status,
        })
 
 
def nfcReceive(tag):
    """
    Just receives whatever nfcpy already handed us for this tag -- no
    manual parsing. nfcpy's tag.ndef.records is already a list of parsed
    record objects (ndef.TextRecord, ndef.UriRecord, etc), so we simply
    pass those straight through.
    """
    return {
        "uid": tag.identifier.hex(),
        "type": tag.type,
        "records": list(tag.ndef.records) if tag.ndef is not None else [],
    }
 
 
def on_connect(tag):
    """Called automatically by nfcpy whenever a tag is detected."""
    tag_data = nfcReceive(tag)
    uid = tag_data["uid"]
 
    workers = load_workers(WORKERS_FILE)
    name = workers.get(uid, f"Unknown badge ({uid})")
 
    last_status = get_last_status(LOG_FILE, uid)
    new_status = "out" if last_status == "in" else "in"
 
    log_event(LOG_FILE, uid, name, new_status)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {name} clocked {new_status.upper()} (UID: {uid})")
 
    if tag_data["records"]:
        print(f"  Tag also carries {len(tag_data['records'])} NDEF record(s):")
        for r in tag_data["records"]:
            print(f"    {r}")  # nfcpy's record objects already print their contents
 
    return True  # returning True releases the tag so the next tap can be read
 
 
def main():
    print("NFC Clock-In System")
    print("Waiting for badge taps... (Ctrl+C to quit)\n")
 
    try:
        with nfc.ContactlessFrontend("usb") as clf:
            while True:
                clf.connect(rdwr={"on-connect": on_connect})
    except OSError as e:
        print("Could not connect to an NFC reader.")
        print("Check that it's plugged in and nfcpy supports it.")
        print(f"Details: {e}")
    except KeyboardInterrupt:
        print("\nStopped.")
 
 
if __name__ == "__main__":
    main()
