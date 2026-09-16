"""
Employee PIN Clock-In / Clock-Out System
-----------------------------------------
This module is meant to plug into two things that already exist elsewhere
in your system and are NOT built here:

    randomcode()  -> prebuilt function that returns a random 4-digit
                      code, somewhere in the range 0001-9999. We only
                      ever CALL this function. It is used for the
                      TEMPORARY code handed to a new employee.

    pinterface    -> the prebuilt front-end/interface that collects a
                      PIN, calls the functions below, and displays
                      whatever string it gets back.
                      (In this repo that's app.py.)

Everything else (the uniqueness check, the in-use/lever check, the
first-time PIN setup, the conflict check, and the Google account link)
is built here.

Two ways in
-----------
RETURNING employee
    Enters their 4-6 digit PIN. lookup() hands back the name on file so
    the interface can show a confirm popup; if the name is wrong they
    back out and try again. Confirming calls process_clock_entry(),
    which flips the clocked-in/clocked-out lever.

    They may instead sign in with Google. The first Google sign-in asks
    for their PIN once to link the account (link_google_account); after
    that, find_by_google_email() finds them with no PIN at all.

NEW employee
    Is handed a randomly produced temporary code. Entering it returns
    NEEDS_PIN_SETUP, and the interface prompts them to choose their own
    4-6 digit PIN via set_custom_pin(). A chosen PIN that's already in
    use comes back as a conflict and they pick again.
"""

import random
from typing import Dict, Optional

TEMP_PIN_LENGTH = 4       # length of the generated code handed to new hires
MIN_PIN_LENGTH = 4        # shortest PIN an employee may choose
MAX_PIN_LENGTH = 6        # longest PIN an employee may choose
MAX_PIN_ATTEMPTS = 10000  # guards the uniqueness loop; see generate_unique_pin()

# ---------------------------------------------------------------------
# Result strings. The interface matches on these, so they live in one
# place rather than being retyped as literals in both files.
# ---------------------------------------------------------------------
DOES_NOT_EXIST = "Does not exist"
NEEDS_PIN_SETUP = "needs pin setup"
CLOCKED_IN = "clocked in"
CLOCKED_OUT = "clocked out"
PIN_SET = "pin set"
PIN_TAKEN = "that pin is already taken"
PIN_BAD_FORMAT = "pin must be 4-6 digits"
GOOGLE_LINKED = "google account linked"
GOOGLE_ALREADY_LINKED = "that google account is already linked to someone else"


def randomcode():
    """Stand-in for the prebuilt randomcode(). Delete once the real one exists.

    Range starts at 1 so "0000" is never handed out, matching the
    documented 0001-9999 range.
    """
    return random.randint(1, 9999)


# ---------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------
# Keyed by PIN (string). Each entry tracks the employee's name, whether
# that PIN is currently "clocked_in" or "clocked_out", whether the PIN is
# still the temporary one they were issued, and any linked Google account.
employee_records: Dict[str, Dict] = {}
# Example entry once someone is registered:
#   "0472": {
#       "name": "Jane Doe",
#       "status": "clocked_out",
#       "temporary": True,        # still the issued code, not self-chosen
#       "google_email": None,
#   }


# ---------------------------------------------------------------------
# 0. Boundary helpers
# ---------------------------------------------------------------------
# randomcode() is prebuilt, so we don't control what type it hands back.
# It currently returns an int (472), while every PIN in employee_records
# is a zero-padded string ("0472"). Normalising here means an int-returning
# randomcode() and a string-returning one both work, and code_exists()
# never compares an int against string keys.
def as_pin(value) -> str:
    """Coerce a generated code into a zero-padded temporary PIN string."""
    return str(value).strip().zfill(TEMP_PIN_LENGTH)


def clean_pin(value) -> str:
    """Strip whatever the interface sent down to bare digits."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def is_valid_pin(pin: str) -> bool:
    """A self-chosen PIN must be 4-6 digits."""
    return pin.isdigit() and MIN_PIN_LENGTH <= len(pin) <= MAX_PIN_LENGTH


# ---------------------------------------------------------------------
# 1. Check if a code already exists (i.e. is already assigned to someone)
# ---------------------------------------------------------------------
def code_exists(pin: str) -> bool:
    """
    Returns True if `pin` has already been assigned to an employee,
    False if it's free to assign.
    """
    return clean_pin(pin) in employee_records


# ---------------------------------------------------------------------
# 2. Generate a unique temporary code for a new employee
# ---------------------------------------------------------------------
def generate_unique_pin() -> str:
    """
    Gets a candidate code from the prebuilt randomcode() function. If
    that code is already taken (code_exists() is True), it calls
    randomcode() again, and keeps doing so until it gets one that isn't
    already in use.

    randomcode() itself is prebuilt and is only called here, never
    defined in this file.

    Raises RuntimeError rather than looping forever if the code space is
    full or randomcode() is stuck returning one value.
    """
    for _ in range(MAX_PIN_ATTEMPTS):
        candidate = as_pin(randomcode())
        if not code_exists(candidate):
            return candidate

    raise RuntimeError("No free code available - the 4-digit space is exhausted.")


# ---------------------------------------------------------------------
# 3. Register a new employee with a freshly generated temporary code
# ---------------------------------------------------------------------
def register_employee(name: str) -> str:
    """
    Creates a new employee record with a unique temporary code and starts
    them off as clocked_out. Returns the code that was assigned so it can
    be given to the employee.

    The record is marked temporary: the first time this code is entered
    at the kiosk, the employee is asked to choose a PIN of their own.
    """
    new_pin = generate_unique_pin()
    employee_records[new_pin] = {
        "name": name,
        "status": "clocked_out",
        "temporary": True,
        "google_email": None,
    }
    return new_pin


# ---------------------------------------------------------------------
# 4. Look a PIN up — the first thing the interface calls on entry
# ---------------------------------------------------------------------
def lookup(pin: str) -> Optional[Dict]:
    """
    Returns the record for `pin`, or None if no such PIN is registered.

    The interface uses this to decide which popup to show:
        None                -> "Does not exist", let them re-enter
        record["temporary"] -> first-time setup, prompt for a custom PIN
        otherwise           -> confirm popup showing record["name"]
    """
    return employee_records.get(clean_pin(pin))


def pin_status(pin: str) -> str:
    """Same check as lookup(), expressed as one of the result strings."""
    record = lookup(pin)
    if record is None:
        return DOES_NOT_EXIST
    if record["temporary"]:
        return NEEDS_PIN_SETUP
    return record["status"]


# ---------------------------------------------------------------------
# 5. First-time setup: employee replaces their issued code with their own
# ---------------------------------------------------------------------
def set_custom_pin(current_pin: str, new_pin: str) -> str:
    """
    Moves an employee's record from the code they were issued to a PIN
    they chose themselves.

    Returns one of:
        "Does not exist"          -> current_pin isn't registered
        "pin must be 4-6 digits"  -> the chosen PIN is the wrong shape
        "that pin is already taken"
                                  -> the chosen PIN belongs to someone
                                     else; the interface shows the error
                                     and lets them pick again
        "pin set"                 -> done; the record now lives under
                                     new_pin and is no longer temporary
    """
    current_pin = clean_pin(current_pin)
    new_pin = clean_pin(new_pin)

    if current_pin not in employee_records:
        return DOES_NOT_EXIST

    if not is_valid_pin(new_pin):
        return PIN_BAD_FORMAT

    # Re-choosing the code you already hold is a conflict with yourself;
    # allowing it would leave the record marked temporary forever.
    if code_exists(new_pin):
        return PIN_TAKEN

    record = employee_records.pop(current_pin)
    record["temporary"] = False
    employee_records[new_pin] = record
    return PIN_SET


# ---------------------------------------------------------------------
# 6. The lever: flip clocked in <-> clocked out
# ---------------------------------------------------------------------
def process_clock_entry(pin: str) -> str:
    """
    Called once the employee has been identified and has confirmed the
    name shown to them.

    Returns one of:
        "Does not exist"   -> the PIN isn't registered to anyone.
        "needs pin setup"  -> still on the issued code; they must choose
                              a PIN before they can clock in.
        "clocked in"       -> this entry just clocked the employee IN.
        "clocked out"      -> this entry just clocked the employee OUT.

    The clock status acts as a lever/toggle: the first time a code is
    entered it flips to clocked in and stays that way until the same
    code is entered again, which flips it back to clocked out.
    """
    pin = clean_pin(pin)
    record = employee_records.get(pin)

    if record is None:
        return DOES_NOT_EXIST

    if record["temporary"]:
        return NEEDS_PIN_SETUP

    if record["status"] == "clocked_out":
        record["status"] = "clocked_in"
        return CLOCKED_IN
    else:
        record["status"] = "clocked_out"
        return CLOCKED_OUT


# ---------------------------------------------------------------------
# 7. Google sign-in — the one-time link, then PIN-free entry
# ---------------------------------------------------------------------
def find_by_google_email(email: str) -> Optional[str]:
    """Returns the PIN whose record is linked to `email`, or None."""
    email = (email or "").strip().casefold()
    if not email:
        return None

    for pin, record in employee_records.items():
        if (record.get("google_email") or "").casefold() == email:
            return pin
    return None


def link_google_account(pin: str, email: str) -> str:
    """
    Ties a Google account to an employee record. This is the "one time"
    part: they enter their PIN once alongside the Google sign-in, and
    from then on Google alone identifies them.

    Returns one of:
        "Does not exist"
        "that google account is already linked to someone else"
        "google account linked"
    """
    pin = clean_pin(pin)
    record = employee_records.get(pin)

    if record is None:
        return DOES_NOT_EXIST

    existing = find_by_google_email(email)
    if existing is not None and existing != pin:
        return GOOGLE_ALREADY_LINKED

    record["google_email"] = (email or "").strip()
    return GOOGLE_LINKED


# ---------------------------------------------------------------------
# Demo / manual test only - remove this block in production.
# ---------------------------------------------------------------------
if __name__ == "__main__":
    issued = register_employee("Jane Doe")
    print(f"Issued temporary code: {issued}")

    # New employee: the issued code can't clock in until a PIN is chosen.
    print(process_clock_entry(issued))            # -> "needs pin setup"

    taken = register_employee("John Smith")
    print(set_custom_pin(issued, taken))          # -> "that pin is already taken"
    print(set_custom_pin(issued, "12"))           # -> "pin must be 4-6 digits"
    print(set_custom_pin(issued, "246810"))       # -> "pin set"

    # Returning employee: look up, confirm the name, flip the lever.
    print(lookup("246810")["name"])               # -> "Jane Doe"
    print(process_clock_entry("246810"))          # -> "clocked in"
    print(process_clock_entry("246810"))          # -> "clocked out"

    # Google: link once, then find with no PIN.
    print(link_google_account("246810", "jane@school.org"))  # -> "google account linked"
    print(find_by_google_email("jane@school.org"))           # -> "246810"

    # "0000" is never handed out, so it's always a safe "unknown PIN" case.
    print(process_clock_entry("0000"))            # -> "Does not exist"
