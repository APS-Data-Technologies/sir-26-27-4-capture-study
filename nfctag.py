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
