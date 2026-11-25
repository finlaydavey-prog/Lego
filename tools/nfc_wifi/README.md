# NFC Wi-Fi sticker (Android + iPhone)

## The catch

| Phone | Native "tap to join Wi-Fi" from a sticker? |
|---|---|
| **Android 5+** | **Yes.** It reads the standard Wi-Fi Simple Configuration record (`application/vnd.wfa.wsc`) and shows a "Connect to network?" prompt. |
| **iPhone** | **No.** iOS ignores Wi-Fi records. It only acts on a **URL** record from the lock screen (iPhone XS and newer). Shortcuts NFC automations can't help either, because they ignore what's on the tag. |

So one sticker holds **two records**:

1. **Wi-Fi record (first)**: Android acts on the first record and joins.
2. **URL record (second)**: an iPhone shows a banner. Tapping it opens a hosted
   `.mobileconfig` profile, and installing it adds the network.

Print the `WIFI:` QR code next to the sticker as a fallback. Both iPhone and
Android camera apps join from it with no extra app.

## Steps

### 1. Generate the files

```bash
python3 nfc_wifi.py --ssid "MyHomeWiFi" --password "supersecret123" \
    --url https://<you>.github.io/wifi/wifi.mobileconfig
```

Use `--security wpa2|wpa|wpa3|open` and `--hidden` if they apply. The output folder `nfc_wifi_out/` contains:

* `wifi.mobileconfig`: the iOS profile (host this)
* `wifi.ndef`, `wifi.ndef.hex`: the exact sticker contents
* `wifi.tlv.bin`: the raw NTAG memory image (for USB writers such as the ACR122U)
* `wifi-qr.txt`: the QR payload. Turn it into an image with any QR generator, or run `qrencode -o wifi.png "$(cat nfc_wifi_out/wifi-qr.txt)"`.

The script reports which tags the data fits. A Wi-Fi record alone fits an
**NTAG213**. Wi-Fi plus URL usually needs an **NTAG215 or NTAG216**.

### 2. Host the profile (iPhone half)

Put `wifi.mobileconfig` on any **HTTPS** host, such as GitHub Pages, Netlify or
your own server. Ideally serve it with
`Content-Type: application/x-apple-aspen-config`. On an iPhone, open the URL in
Safari: you should see **Profile Downloaded**, then go to
**Settings → Profile Downloaded → Install**.

> The profile contains the password in plain text, and anyone with the URL can
> download it. Use it for a guest network, or a URL nobody can guess.

### 3. Write the sticker

The easiest way is the free **NFC Tools** app (Android or iPhone):

1. **Write → Add a record → Wi-Fi network**: enter the SSID, password, and WPA2-Personal/AES.
2. **Add a record → URL**: enter the profile URL.
3. Make sure the Wi-Fi record is **first** in the list, then tap **Write** and hold the phone to the sticker.
4. Optional: **Other → Lock tag**, so nobody can overwrite it. Locking is permanent.

You can also write `wifi.ndef` with any tool that takes raw NDEF, or write
`wifi.tlv.bin` from page 4 onward with a USB reader.

### 4. Test

* **Android:** turn on NFC, unlock the phone and tap. You should see "Connect to network?".
* **iPhone XS or newer:** wake the screen and tap the top edge. You should see a URL banner; tap it, then install the profile.
* If an iPhone shows nothing, it's ignoring the second record. Either swap the record order and keep the QR code for Android, or use **two stickers** (one per platform).

## Tests

```bash
python3 -m pytest -q test_nfc_wifi.py
```
