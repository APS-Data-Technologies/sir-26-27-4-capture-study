"""
Employee PIN Clock-In / Clock-Out System
-----------------------------------------
This module is meant to plug into two things that already exist elsewhere
in your system and are NOT built here:

    randomcode()  -> prebuilt function that returns a random 4-digit
                      code as a string (e.g. "0472"), somewhere in the
                      range 0001-9999. We only ever CALL this function.

    pinterface    -> the prebuilt front-end/interface that collects a
                      PIN and a name from the employee, calls
                      process_clock_entry() below, and displays
                      whatever string it gets back.

Everything else (the uniqueness check, the in-use/lever check, the
name-matching check, and the registration logic) is built here.
"""
import random
from typing import Dict


# ---------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------
# Keyed by 4-digit PIN (string). Each entry tracks the employee's name
# and whether that PIN is currently "clocked_in" or "clocked_out".
employee_records: Dict[str, Dict[str, str]] = {}
# Example entry once someone is registered:
#   "0472": {"name": "Jane Doe", "status": "clocked_out"}


# ---------------------------------------------------------------------
# 1. Check if a code already exists (i.e. is already assigned to someone)
# ---------------------------------------------------------------------
def code_exists(pin: str) -> bool:
    """
    Returns True if `pin` has already been assigned to an employee,
    False if it's free to assign.
    """
    return pin in employee_records


# ---------------------------------------------------------------------
# 2. Generate a unique 4-digit PIN
# ---------------------------------------------------------------------
def generate_unique_pin() -> str:
    """
    Gets a candidate PIN from the prebuilt randomcode() function. If
    that PIN is already taken (code_exists() is True), it calls
    randomcode() again, and keeps doing so until it gets a PIN that
    isn't already in use.

    randomcode() itself is prebuilt and is only called here, never
    defined in this file.
    """
    candidate = randomcode()

    while code_exists(candidate):
        candidate = randomcode()

    return candidate


# ---------------------------------------------------------------------
# 3. Register a new employee with a freshly generated unique PIN
# ---------------------------------------------------------------------
def register_employee(name: str) -> str:
    """
    Creates a new employee record with a unique PIN and starts them
    off as clocked_out. Returns the PIN that was assigned so it can be
    given to the employee.
    """
    new_pin = generate_unique_pin()
    employee_records[new_pin] = {"name": name, "status": "clocked_out"}
    return new_pin


# ---------------------------------------------------------------------
# 4. Main function pinterface calls every time someone enters a PIN
# ---------------------------------------------------------------------
def process_clock_entry(pin: str, name: str) -> str:
    """
    Called by pinterface with the PIN and name the employee entered.

    Returns exactly one of:
        "Does not exist"
            -> the PIN isn't registered to anyone.
        "password or username is incorrect"
            -> the PIN exists, but the name given doesn't match the
               name on file for that PIN.
        "clocked in"
            -> PIN and name matched, and this entry just clocked the
               employee IN.
        "clocked out"
            -> PIN and name matched, and this entry just clocked the
               employee OUT.

    The clock status acts as a lever/toggle: the first time a code is
    entered it flips to clocked in and stays that way until the same
    code is entered again, which flips it back to clocked out.
    """
    # Step 1: does the PIN exist at all?
    if not code_exists(pin):
        return "Does not exist"

    record = employee_records[pin]

    # Step 2: does the name match the name tied to this PIN?
    if record["name"] != name:
        return "password or username is incorrect"

    # Step 3: name + PIN match -> flip the lever
    if record["status"] == "clocked_out":
        record["status"] = "clocked_in"
        return "clocked in"
    else:
        record["status"] = "clocked_out"
        return "clocked out"


# ---------------------------------------------------------------------
# Demo / manual test only - remove this block in production.
# It fakes randomcode() so the file can run standalone. In your real
# environment, delete this section; randomcode() and pinterface will
# already be provided.
# ---------------------------------------------------------------------
if __name__ == "__main__":
    import random

    def randomcode() -> str:  # noqa: F811 - test stand-in only
        return f"{random.randint(1, 9999):04d}"

    pin = register_employee("Jane Doe")
    print(f"Assigned PIN: {pin}")

    print(process_clock_entry(pin, "Jane Doe"))   # -> "clocked in"
    print(process_clock_entry(pin, "Jane Doe"))   # -> "clocked out"
    print(process_clock_entry(pin, "John Smith")) # -> "password or username is incorrect"
    print(process_clock_entry("1234", "Nobody"))  # -> "Does not exist"
