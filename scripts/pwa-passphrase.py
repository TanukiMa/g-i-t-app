"""Make the value for PWA_PASSPHRASE_HASH (PWA_ENABLED=optin): the passphrase that switches the app on in a browser.

    python scripts/pwa-passphrase.py            # asks for the passphrase (not shown), prints the setting
    python scripts/pwa-passphrase.py --check    # asks for a passphrase and the setting, says whether they match

The setting is "pbkdf2$sha256$<iterations>$<salt>$<hash>": a salted PBKDF2-HMAC-SHA256 hash. static/pwa.js checks what a
visitor types against it in the browser. It keeps the app from being switched on by accident; it is NOT access control
(the site is public, and the hash is in every page, so anybody can try guesses offline): choose a long passphrase
(four random words, or 16 characters or more). The passphrase itself is never stored anywhere.
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import os
import re
import sys

ITERATIONS = 200_000
PATTERN = re.compile(r"^pbkdf2\$sha256\$(\d{4,7})\$([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+)$")


def derive(phrase: str, salt: bytes, iterations: int) -> bytes:
    # NFKC like static/pwa.js, so that full-width / half-width input gives the same hash
    import unicodedata
    return hashlib.pbkdf2_hmac("sha256", unicodedata.normalize("NFKC", phrase).encode("utf-8"), salt, iterations, dklen=32)


def make(phrase: str) -> str:
    salt = os.urandom(16)
    digest = derive(phrase, salt, ITERATIONS)
    return "pbkdf2$sha256$%d$%s$%s" % (ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def matches(phrase: str, spec: str) -> bool:
    m = PATTERN.match(spec.strip())
    if not m:
        return False
    return hmac.compare_digest(derive(phrase, base64.b64decode(m.group(2)), int(m.group(1))), base64.b64decode(m.group(3)))


def main():
    parser = argparse.ArgumentParser(description="Hash of the passphrase that switches the PWA on (PWA_ENABLED=optin)")
    parser.add_argument("--check", action="store_true", help="check a passphrase against an existing setting")
    args = parser.parse_args()
    if args.check:
        spec = input("PWA_PASSPHRASE_HASH: ").strip().removeprefix("PWA_PASSPHRASE_HASH=")
        print("match" if matches(getpass.getpass("Passphrase: "), spec) else "NO match")
        return
    phrase = getpass.getpass("Passphrase: ")
    if len(phrase) < 12:
        print("That is short: anybody can try guesses offline against the hash. Use 12 characters or more (16+ is better).", file=sys.stderr)
    if phrase != getpass.getpass("Again: "):
        sys.exit("The two entries differ.")
    print("PWA_PASSPHRASE_HASH=" + make(phrase))


if __name__ == "__main__":
    main()
