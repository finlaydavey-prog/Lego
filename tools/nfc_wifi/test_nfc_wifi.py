import plistlib
import nfc_wifi as n


def test_wsc_record_roundtrip():
    msg = n.ndef_message("HomeNet", "correcthorse", "wpa2", None)
    [(tnf, rtype, payload)] = n.parse_ndef(msg)
    assert tnf == 0x02 and rtype == n.WSC_MIME
    attrs = n.parse_wsc(payload)
    assert attrs[n.ATTR_SSID] == b"HomeNet"
    assert attrs[n.ATTR_NETWORK_KEY] == b"correcthorse"
    assert attrs[n.ATTR_AUTH_TYPE] == b"\x00\x20"
    assert attrs[n.ATTR_ENCR_TYPE] == b"\x00\x08"
    assert msg[0] & 0xC0 == 0xC0  # MB + ME on single record


def test_two_records_wsc_first_then_url():
    msg = n.ndef_message("HomeNet", "correcthorse", "wpa2", "https://example.com/wifi.mobileconfig")
    recs = n.parse_ndef(msg)
    assert [r[1] for r in recs] == [n.WSC_MIME, b"U"]
    assert recs[1][2] == b"\x04example.com/wifi.mobileconfig"


def test_mobileconfig_and_qr():
    p = plistlib.loads(n.mobileconfig("Café;Net", "pa:ss;word", "wpa3", False))
    wifi = p["PayloadContent"][0]
    assert wifi["SSID_STR"] == "Café;Net" and wifi["EncryptionType"] == "WPA3"
    assert n.wifi_qr("Café;Net", "pa:ss;word", "wpa2", False) == r"WIFI:T:WPA;S:Café\;Net;P:pa\:ss\;word;;"
    assert n.wifi_qr("Guest", "", "open", False) == "WIFI:T:nopass;S:Guest;;"


def test_fits_ntag213_for_typical_network():
    msg = n.ndef_message("MyHomeWiFi-5G", "a-sixteen-chars!", "wpa2", None)
    assert len(n.type2_tlv(msg)) <= 144 + 3
