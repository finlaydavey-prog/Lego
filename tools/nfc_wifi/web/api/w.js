// GET /w?s=<ssid>&p=<password>&t=<WPA2|WPA|WPA3|None>[&h=1]
// Returns an iOS configuration profile that adds the Wi-Fi network.
// Nothing is stored: the credentials live only in the link on the NFC tag.
const crypto = require("crypto");

const esc = (v) => String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const uuidFrom = (seed) => {
  const h = crypto.createHash("sha256").update(seed).digest("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`.toUpperCase();
};
const TYPES = { WPA2: "WPA2", WPA: "WPA", WPA3: "WPA3", NONE: "None" };

module.exports = (req, res) => {
  const q = req.query || {};
  const ssid = typeof q.s === "string" ? q.s : "";
  const pass = typeof q.p === "string" ? q.p : "";
  const type = TYPES[String(q.t || "WPA2").toUpperCase()] || "WPA2";
  const hidden = q.h === "1";

  if (!ssid || ssid.length > 32 || (type !== "None" && (pass.length < 8 || pass.length > 63))) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "text/plain; charset=utf-8");
    return res.end("This Wi-Fi link is incomplete. Re-create it on the setup page.");
  }

  // Stable IDs per network, so re-tapping replaces the profile instead of duplicating it.
  const id = uuidFrom("wifi:" + ssid);
  const pid = uuidFrom("profile:" + ssid);
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadVersion</key><integer>1</integer>
  <key>PayloadIdentifier</key><string>app.nfcwifi.${pid}</string>
  <key>PayloadUUID</key><string>${pid}</string>
  <key>PayloadDisplayName</key><string>Wi-Fi: ${esc(ssid)}</string>
  <key>PayloadDescription</key><string>Adds the Wi-Fi network ${esc(ssid)} to this device.</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadContent</key><array><dict>
    <key>PayloadType</key><string>com.apple.wifi.managed</string>
    <key>PayloadVersion</key><integer>1</integer>
    <key>PayloadIdentifier</key><string>app.nfcwifi.${id}</string>
    <key>PayloadUUID</key><string>${id}</string>
    <key>PayloadDisplayName</key><string>${esc(ssid)}</string>
    <key>SSID_STR</key><string>${esc(ssid)}</string>
    <key>HIDDEN_NETWORK</key>${hidden ? "<true/>" : "<false/>"}
    <key>AutoJoin</key><true/>
    <key>IsHotspot</key><false/>
    <key>EncryptionType</key><string>${type}</string>${type === "None" ? "" : `
    <key>Password</key><string>${esc(pass)}</string>`}
  </dict></array>
</dict></plist>
`;
  res.statusCode = 200;
  res.setHeader("Content-Type", "application/x-apple-aspen-config");
  res.setHeader("Content-Disposition", 'attachment; filename="wifi.mobileconfig"');
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Robots-Tag", "noindex");
  res.end(plist);
};
