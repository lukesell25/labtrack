"""
cac_reader.py - Watches the USB smart card reader and identifies whoever
just tapped their CAC, without requiring a PIN.

How this works:
  1. pyscard's CardMonitor watches for card insertion/removal at the PC/SC
     level (talks to pcscd). This part needs no crypto middleware.
  2. On insertion, we open a PKCS#11 session against OpenSC's PKCS#11 module
     and read the PIV Authentication certificate object off the card.
     Reading a *public* object like this does not require a PIN on a
     standard CAC - PIN entry is only enforced for private-key operations
     (signing/decrypting), not for reading the cert bytes.
  3. We parse the EDIPI out of the certificate (either from the Subject
     Alternative Name's "otherName" DoD Person Identifier field, falling
     back to the trailing digits of the Subject CN, which CACs conventionally
     format as "LAST.FIRST.MIDDLE.0123456789").
  4. We debounce so a single tap fires exactly one event, and we ignore
     the card until it's removed and a new one is presented.

IMPORTANT - things you will likely need to adjust on real hardware:
  - PKCS11_MODULE_PATH below. It varies by distro/architecture. Run
    `find / -name "opensc-pkcs11.so" 2>/dev/null` on the Pi after installing
    opensc to find the real path and update the constant.
  - Some CAC issuances put the EDIPI in a different SAN OID than others.
    The DoD Person Identifier otherName OID used below (2.16.840.1.101.3.6.6)
    is the standard PIV FASC-N/EDIPI field, but if extraction fails on your
    cards, `opensc-tool -r 0 -s ...` or `pkcs15-tool --list-certificates`
    can help you inspect what's actually on the card, and you can fall back
    to the Subject CN parsing path (which is already implemented as a
    fallback below).
"""

import logging
import threading
import time

from cryptography import x509
from cryptography.hazmat.backends import default_backend

# pyscard (and the pcscd/opensc system packages it talks to) are only ever
# present on the Raspberry Pi, where the physical card reader is attached.
# Import it lazily so this module - and app.py, which imports it - can
# still be imported and run on a plain dev machine (e.g. Windows/WSL via
# Claude Code) for everything that isn't CAC hardware itself: the Flask
# routes, the dashboard, the kiosk display, the database logic. Only
# start_cac_monitor() actually requires pyscard to be installed.
try:
    from smartcard.CardMonitoring import CardMonitor, CardObserver

    _PYSCARD_AVAILABLE = True
except ImportError:
    CardMonitor = None
    CardObserver = object  # lets _TapObserver below still be defined
    _PYSCARD_AVAILABLE = False

log = logging.getLogger("cac_reader")

# Adjust this to match your system - see docstring above.
PKCS11_MODULE_PATH = "/usr/lib/aarch64-linux-gnu/opensc-pkcs11.so"

# Debounce window: ignore repeat reads of the same physical tap.
DEBOUNCE_SECONDS = 3

# Microsoft UPN otherName OID. On DoD CACs this is commonly populated with a
# value that starts with the 10-digit EDIPI (sometimes followed by extra
# digits), e.g. "0000000000157005@mil".
UPN_OTHERNAME_OID = "1.3.6.1.4.1.311.20.2.3"


def _extract_edipi_from_cert(cert: x509.Certificate) -> str | None:
    """
    Pull the 10-digit EDIPI out of a PIV certificate.

    Note: PIV certs also carry a SAN otherName under OID 2.16.840.1.101.3.6.6
    (the "DoD Person Identifier" field), but on real-world CACs this holds
    the FASC-N - a packed binary structure, not the EDIPI as text - so it is
    NOT used here. Decoding it as a string and grabbing digits would produce
    plausible-looking but wrong output. The two fields below are what
    actually carry the EDIPI in practice.
    """
    # Preferred: CAC Subject CN is conventionally "LAST.FIRST.MIDDLE.EDIPI".
    try:
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        last_part = cn.split(".")[-1]
        if last_part.isdigit() and len(last_part) == 10:
            return last_part
    except (IndexError, Exception):
        pass

    # Fallback: SAN otherName carrying a UPN. DoD certs format this as the
    # 10-digit EDIPI followed by additional digits, e.g. "0000000000157005@mil".
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for other_name in san.get_values_for_type(x509.OtherName):
            if other_name.type_id.dotted_string == UPN_OTHERNAME_OID:
                text = other_name.value.decode("utf-8", errors="ignore")
                digits = "".join(ch for ch in text if ch.isdigit())
                if len(digits) >= 10:
                    return digits[:10]
    except x509.ExtensionNotFound:
        pass

    return None


def _read_piv_auth_cert_der(atr, connection) -> bytes | None:
    """
    Reads the PIV Authentication certificate off the currently-connected
    card via PKCS#11 (no PIN required for reading public cert objects).

    Uses python-pkcs11 (cffi-based) rather than PyKCS11 (SWIG-based) - the
    latter's generated C++ wrapper doesn't build cleanly against newer
    SWIG/Python versions on current Debian/Raspberry Pi OS releases.

    Retries briefly if OpenSC hasn't finished recognizing the token yet.
    pyscard's CardMonitor reports a card as "present" the moment PC/SC sees
    it electrically, which can be a beat before OpenSC has walked the
    card's PIV file structure and exposed it as a PKCS#11 token - without
    this retry, a tap landing right in that gap would be silently dropped.
    """
    import pkcs11
    from pkcs11 import Attribute, ObjectClass
    from pkcs11.exceptions import NoSuchToken

    lib = pkcs11.lib(PKCS11_MODULE_PATH)

    token = None
    for attempt in range(6):  # ~3 seconds total, 0.5s apart
        slots = lib.get_slots(token_present=True)
        if slots:
            try:
                token = slots[0].get_token()
                break
            except NoSuchToken:
                pass
        time.sleep(0.5)

    if token is None:
        log.warning("No PKCS#11 token became available after retrying - card not readable")
        return None

    # No user_pin passed - reading a public certificate object doesn't
    # require login on a standard CAC.
    with token.open() as session:
        certs = list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
        if not certs:
            return None

        for cert in certs:
            try:
                label = cert[Attribute.LABEL]
            except Exception:
                label = ""
            if label and "PIV Auth" in label:
                return bytes(cert[Attribute.VALUE])

        # No explicitly-labeled PIV Auth cert found - fall back to the first.
        return bytes(certs[0][Attribute.VALUE])


class _TapObserver(CardObserver):
    """
    Fires on_tap(edipi) whenever a new card is presented and identified.

    Also fires on_card_detected() immediately on physical insertion (before
    the ~3-second PKCS#11 read even starts) and on_unrecognized(reason) if
    a presented card can't be identified - both optional, both meant for
    giving the kiosk display something to show while a read is in flight.
    """

    def __init__(self, on_tap, on_card_detected=None, on_unrecognized=None):
        self._on_tap = on_tap
        self._on_card_detected = on_card_detected
        self._on_unrecognized = on_unrecognized
        self._last_seen_serial = None
        self._last_fire_time = 0

    def update(self, observable, actions):
        added_cards, removed_cards = actions

        for card in removed_cards:
            self._last_seen_serial = None

        for card in added_cards:
            # Fire immediately, before debounce/read - this is what lets the
            # kiosk show "reading card..." right away instead of only
            # finding out several seconds later once the read finishes.
            if self._on_card_detected:
                try:
                    self._on_card_detected()
                except Exception:
                    log.exception("on_card_detected callback failed")

            now = time.time()
            if now - self._last_fire_time < DEBOUNCE_SECONDS:
                continue
            try:
                connection = card.createConnection()
                connection.connect()
                der = _read_piv_auth_cert_der(card.atr, connection)
                if der is None:
                    log.warning("Could not read a certificate off the presented card")
                    if self._on_unrecognized:
                        self._on_unrecognized("unreadable")
                    continue
                cert = x509.load_der_x509_certificate(der, default_backend())
                edipi = _extract_edipi_from_cert(cert)
                if edipi is None:
                    log.warning("Read a certificate but could not extract an EDIPI from it")
                    if self._on_unrecognized:
                        self._on_unrecognized("no_edipi")
                    continue

                self._last_fire_time = now
                self._on_tap(edipi)
            except Exception as e:
                log.exception("Error reading presented card: %s", e)
                if self._on_unrecognized:
                    self._on_unrecognized("error")


def start_cac_monitor(on_tap, on_card_detected=None, on_unrecognized=None):
    """
    Starts watching the smart card reader in a background thread.
    on_tap(edipi: str) is called once per distinct, successfully-identified tap.
    on_card_detected() is called immediately on physical insertion, before
    the read even starts.
    on_unrecognized(reason: str) is called if a presented card can't be
    read or identified. reason is one of "unreadable", "no_edipi", "error".

    Returns the CardMonitor instance (keep a reference so it isn't garbage
    collected - pyscard's monitor stops if it is).

    Raises RuntimeError if pyscard isn't installed - expected and fine when
    running away from the Pi (e.g. local development on Windows/WSL); the
    caller (app.py) already handles this gracefully.
    """
    if not _PYSCARD_AVAILABLE:
        raise RuntimeError(
            "pyscard is not installed, so there's no CAC reader to watch. "
            "This is expected on a dev machine away from the Pi - the rest "
            "of the app (routes, dashboard, kiosk display, database) works "
            "fine without it. Use /api/manual-toggle to simulate taps."
        )

    monitor = CardMonitor()
    observer = _TapObserver(on_tap, on_card_detected=on_card_detected, on_unrecognized=on_unrecognized)
    monitor.addObserver(observer)
    log.info("CAC monitor started, waiting for card taps")
    return monitor, observer
