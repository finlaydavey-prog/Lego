#!/usr/bin/env python3
"""Generate everything needed to make an NFC sticker (plus fallbacks) that
joins a Wi-Fi network on Android and iPhone.

Outputs (in --out, default ./nfc_wifi_out):

  wifi.ndef         Raw NDEF message. Record 1 is a Wi-Fi Simple Configuration
                    credential (MIME application/vnd.wfa.wsc), which Android
                    joins natively. If --url is given, record 2 is a URI record
                    that iPhones open from the lock screen (iOS ignores the
                    Wi-Fi record).
  wifi.ndef.hex     The same bytes as hex, for writer apps that accept raw data.
  wifi.tlv.bin      NDEF wrapped in a Type 2 tag TLV (03 len ... FE), i.e. what
                    ends up in an NTAG's user memory from page 4 onward.
  wifi.mobileconfig iOS configuration profile that adds the network. Host it at
                    the --url so tapping the sticker on an iPhone offers to
                    install it.
  wifi-qr.txt       The standard WIFI: QR payload. Both iPhone and Android
                    camera apps join from it natively - print it next to the
                    sticker as a universal fallback.

Standard library only. Run with --help for options.
"""
from __future__ import annotations

import argparse
import plistlib
import struct
import sys
import uuid
from pathlib import Path

# --- Wi-Fi Simple Configuration (WSC) attribute IDs -------------------------
ATTR_CREDENTIAL = 0x100E
ATTR_NETWORK_INDEX = 0x1026
ATTR_SSID = 0x1045
ATTR_AUTH_TYPE = 0x1003
ATTR_ENCR_TYPE = 0x100F
ATTR_NETWORK_KEY = 0x1027
ATTR_MAC_ADDRESS = 0x1020
ATTR_VENDOR_EXT = 0x1049
ATTR_VERSION = 0x104A

WFA_VENDOR_ID = b"\x00\x37\x2A"
WFA_SUBELEM_VERSION2 = b"\x00\x01\x20"  # Version2 = 2.0

# security name -> (WSC auth type, WSC encryption type, iOS EncryptionType)
SECURITY = {
    "wpa2": (0x0020, 0x0008, "WPA2"),    # WPA2-Personal, AES   (also fine for WPA2/WPA3 mixed)
    "wpa":  (0x0022, 0x000C, "WPA"),     # WPA/WPA2 mixed, AES+TKIP
    "wpa3": (0x0020, 0x0008, "WPA3"),    # WSC has no SAE value; Android treats as WPA2-PSK
    "open": (0x0001, 0x0001, "None"),
}

WSC_MIME = b"application/vnd.wfa.wsc"

# Usable NDEF bytes per common tag (user memory minus the 2-byte TLV header
# and 1-byte terminator).
TAGS = [("NTAG213", 144), ("NTAG215", 504), ("NTAG216", 888)]


def tlv(attr: int, value: bytes) -> bytes:
    return struct.pack(">HH", attr, len(value)) + value


def wsc_payload(ssid: str, password: str, security: str) -> bytes:
    auth, encr, _ = SECURITY[security]
    cred = b"".join([
        tlv(ATTR_NETWORK_INDEX, b"\x01"),
        tlv(ATTR_SSID, ssid.encode("utf-8")),
        tlv(ATTR_AUTH_TYPE, struct.pack(">H", auth)),
        tlv(ATTR_ENCR_TYPE, struct.pack(">H", encr)),
        tlv(ATTR_NETWORK_KEY, password.encode("utf-8")),
        tlv(ATTR_MAC_ADDRESS, b"\xff" * 6),  # broadcast = "any BSSID"
    ])
    return b"".join([
        tlv(ATTR_VERSION, b"\x10"),
        tlv(ATTR_CREDENTIAL, cred),
        tlv(ATTR_VENDOR_EXT, WFA_VENDOR_ID + WFA_SUBELEM_VERSION2),
    ])


def ndef_record(tnf: int, rtype: bytes, payload: bytes, mb: bool, me: bool) -> bytes:
    sr = len(payload) < 256
    header = (0x80 if mb else 0) | (0x40 if me else 0) | (0x10 if sr else 0) | tnf
    length = bytes([len(payload)]) if sr else struct.pack(">I", len(payload))
    return bytes([header, len(rtype)]) + length + rtype + payload



def uri_payload(url: str) -> bytes:
    for code, prefix in ((2, "https://www."), (1, "http://www."), (4, "https://"), (3, "http://")):
        if url.startswith(prefix):
            return bytes([code]) + url[len(prefix):].encode("utf-8")
    return b"\x00" + url.encode("utf-8")


def ndef_message(ssid: str, password: str, security: str, url: str | None) -> bytes:
    wsc = wsc_payload(ssid, password, security)
    if not url:
        return ndef_record(0x02, WSC_MIME, wsc, mb=True, me=True)
    return (ndef_record(0x02, WSC_MIME, wsc, mb=True, me=False)
            + ndef_record(0x01, b"U", uri_payload(url), mb=False, me=True))


def type2_tlv(ndef: bytes) -> bytes:
    if len(ndef) < 0xFF:
        head = bytes([0x03, len(ndef)])
    else:
        head = b"\x03\xff" + struct.pack(">H", len(ndef))
    return head + ndef + b"\xfe"


def mobileconfig(ssid: str, password: str, security: str, hidden: bool) -> bytes:
    _, _, ios_encr = SECURITY[security]
    wifi = {
        "PayloadType": "com.apple.wifi.managed",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"local.nfcwifi.{uuid.uuid4()}",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadDisplayName": f"Wi-Fi ({ssid})",
        "SSID_STR": ssid,
        "HIDDEN_NETWORK": hidden,
        "AutoJoin": True,
        "EncryptionType": ios_encr,
        "IsHotspot": False,
    }
    if ios_encr != "None":
        wifi["Password"] = password
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"local.nfcwifi.profile.{uuid.uuid4()}",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadDisplayName": f"Join {ssid}",
        "PayloadDescription": f"Adds the Wi-Fi network {ssid} to this device.",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [wifi],
    }
    return plistlib.dumps(profile)


def qr_escape(s: str) -> str:
    return "".join("\\" + c if c in '\\;,:"' else c for c in s)


def wifi_qr(ssid: str, password: str, security: str, hidden: bool) -> str:
    t = "nopass" if security == "open" else ("SAE" if security == "wpa3" else "WPA")
    parts = [f"WIFI:T:{t}", f"S:{qr_escape(ssid)}"]
    if security != "open":
        parts.append(f"P:{qr_escape(password)}")
    if hidden:
        parts.append("H:true")
    return ";".join(parts) + ";;"


# --- decoding, used for --verify and tests ----------------------------------
def parse_ndef(data: bytes) -> list[tuple[int, bytes, bytes]]:
    out, i = [], 0
    while i < len(data):
        hdr = data[i]; tlen = data[i + 1]; i += 2
        if hdr & 0x10:
            plen = data[i]; i += 1
        else:
            plen = struct.unpack(">I", data[i:i + 4])[0]; i += 4
        idlen = 0
        if hdr & 0x08:
            idlen = data[i]; i += 1
        rtype = data[i:i + tlen]; i += tlen + idlen
        out.append((hdr & 0x07, rtype, data[i:i + plen])); i += plen
        if hdr & 0x40:
            break
    return out


def parse_wsc(payload: bytes) -> dict[int, bytes]:
    found, i = {}, 0
    while i + 4 <= len(payload):
        attr, ln = struct.unpack(">HH", payload[i:i + 4])
        val = payload[i + 4:i + 4 + ln]
        if attr == ATTR_CREDENTIAL:
            found.update(parse_wsc(val))
        else:
            found[attr] = val
        i += 4 + ln
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssid", required=True)
    ap.add_argument("--password", default="")
    ap.add_argument("--security", choices=SECURITY, default="wpa2")
    ap.add_argument("--hidden", action="store_true", help="network does not broadcast its SSID")
    ap.add_argument("--url", help="HTTPS URL where you will host wifi.mobileconfig "
                                  "(adds an iPhone record to the sticker)")
    ap.add_argument("--out", default="nfc_wifi_out", type=Path)
    a = ap.parse_args(argv)

    if a.security != "open" and not 8 <= len(a.password) <= 63:
        ap.error("WPA passwords must be 8-63 characters")
    if a.url and not a.url.startswith("https://"):
        print("warning: iOS will only offer to install a profile served over HTTPS", file=sys.stderr)

    ndef = ndef_message(a.ssid, a.password, a.security, a.url)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "wifi.ndef").write_bytes(ndef)
    (a.out / "wifi.ndef.hex").write_text(ndef.hex(" ").upper() + "\n")
    (a.out / "wifi.tlv.bin").write_bytes(type2_tlv(ndef))
    (a.out / "wifi.mobileconfig").write_bytes(mobileconfig(a.ssid, a.password, a.security, a.hidden))
    (a.out / "wifi-qr.txt").write_text(wifi_qr(a.ssid, a.password, a.security, a.hidden) + "\n")

    need = len(type2_tlv(ndef))
    fits = [name for name, cap in TAGS if need <= cap + 3]
    print(f"Wrote files to {a.out.resolve()}")
    print(f"NDEF message: {len(ndef)} bytes ({need} on tag) -> fits: {', '.join(fits) or 'NONE of NTAG213/215/216'}")
    print(f"QR payload:   {(a.out / 'wifi-qr.txt').read_text().strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
