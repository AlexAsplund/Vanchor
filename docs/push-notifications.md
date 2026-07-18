# Web Push notifications (adoption #7)

Server-initiated push notifications let the boat raise an alarm on the
operator's phone even when no browser tab is open and the phone is locked.
This is the one channel that still works for the link-loss failsafe — by
definition that fires when NO client is connected.

> Push notifications are an *additional* layer on top of the existing in-app
> banner and sound alerts (alerts.js). The in-app path keeps working unchanged.

---

## Alarm sources

Five classes of alarms are wired as push-notification sources:

| Alarm | Trigger condition |
|-------|-------------------|
| Anchor drag | The motor-on anchor hold detects the boat has moved outside the drag threshold |
| Anchor watch | The motor-off passive watch circle (anchor alarm) detects a breach |
| Battery low | The battery ladder steps the thrust cap DOWN, or the RTL recommendation trips |
| Depth warning | Shallow-water auto-stop triggered, or sounder disagrees with chart |
| Link / GPS | Link-loss failsafe engages (motor stopped / holding / continuing), or GPS fix lost |

Each kind is rate-limited independently (30 s per-kind floor by default) plus a
global burst cap (10 per 60 s). Per-kind rate limits prevent flapping alarms
from flooding the phone. TTL defaults to 900 s — if the phone is offline the
push service holds the message for 15 minutes before discarding it.

---

## Setup walkthrough

### 1. Install the push extra

```bash
pip install "vanchor-ng[push]"
```

This pulls in `pywebpush` and `py-vapid` (and their `cryptography` +
`requests` dependencies). Without this extra the Settings card shows an
explanation instead of the subscribe controls — no import errors, no
breakage.

### 2. Open the app over HTTPS

Push requires a **secure context** (`https://`). The boat's HTTPS listener is
at port 8443 by default:

```
https://vanchor.local:8443
```

You must also **trust the boat's self-signed certificate** on your device. The
certificate is auto-generated into `<data_dir>/tls/` on first start. Installing
it varies by OS:

- **Android/Chrome**: install the `.crt` file from Settings -> Security ->
  Install a certificate.
- **iOS/Safari**: install the profile via Settings -> General -> VPN &
  Device Management, then trust it under General -> About -> Certificate Trust
  Settings.
- **macOS/iOS via Keychain**: drag the `.crt` into Keychain Access, then set
  Trust -> SSL/TLS to "Always Trust".

> Clicking through the browser interstitial ("proceed anyway") is NOT enough —
> the service worker refuses to register on an untrusted origin.

### 3. Enable in Settings

Open **Settings -> Sound & touch -> Notifications**, expand the card, and
toggle **Alarm notifications on this device**. The browser will ask for
permission; grant it.

The boat generates a VAPID keypair on first enable (stored in
`<data_dir>/push/vapid_private.pem`) and your browser subscribes with its
push service (FCM on Chrome/Android, Mozilla autopush on Firefox, Apple's
service on Safari/iOS).

### 4. Send a test notification

Tap **Send test notification** in the card. The notification appears at OS
level — even if the app tab is open.

---

## Platform constraints

1. **HTTPS required.** `PushManager` exists only on `https://` pages (or
   `http://localhost`). The app's plain-HTTP LAN default
   (`http://<pi>:8000`) cannot use push. The HTTPS listener at port 8443 is
   the supported path.

2. **Certificate must be trusted.** Chrome and Firefox refuse service workers
   on an origin whose TLS certificate the OS or browser does not trust.
   Clicking through the browser interstitial is NOT sufficient. Install and
   trust the boat's certificate on each device as described above.

3. **The Pi needs internet at send time.** Web Push is relayed through the
   browser vendor's push service (Google FCM, Mozilla autopush, Apple).
   Delivery requires the Pi to be able to reach those servers at the moment
   the alarm fires. Typical working setups:
   - Pi tethered to the phone's mobile hotspot.
   - Boat at the dock on home WiFi (the anchor-watch use case).
   If the Pi has no internet the send fails, logs one line, and retries on
   the next alarm edge. Messages are NOT queued across hours.

4. **iOS 16.4+ and Home Screen.** Safari/iOS supports Web Push only for PWAs
   added to the Home Screen. Use the Share sheet -> Add to Home Screen, then
   open the app from the Home Screen icon before enabling notifications.

5. **Delivery is best-effort.** TTL limits how long the push service holds an
   undelivered message. Push never replaces the in-app banner/sound path —
   alerts.js keeps working unchanged.

---

## Config reference

All keys are optional; defaults preserve current behaviour (zero subscriptions
means nothing ever sends).

```yaml
push:
  enabled: true              # master switch; false hides the API
  subject: mailto:vanchor@example.com   # VAPID contact claim (any valid mailto)
  ttl_s: 900                 # seconds push service holds message for offline phone
  min_interval_s: 30.0       # per-alarm-kind floor between notifications
  burst_limit: 10            # global cap: max notifications per burst window
  burst_window_s: 60.0
  timeout_s: 10.0            # HTTP timeout per webpush POST
```

---

## Privacy

- **Keys stay on the boat.** The VAPID private key is stored in
  `<data_dir>/push/vapid_private.pem` (mode 0o600). It is excluded from
  backup ZIPs so it never appears in support files you share.
- **Subscriptions stay on the boat.** Browser capability URLs (the push
  endpoint) are stored in `<data_dir>/push/subscriptions.json`. They are
  excluded from backups for two reasons: the VAPID private key must accompany
  them to be useful, and restoring them onto a different install would silently
  redirect someone's phone at the new install.
- **Re-subscribe automatically.** The Settings card re-upserts your
  subscription on every open, so after a data restore one card open is
  sufficient to re-register.
- The boat never sends push messages proactively on boot; notifications only
  fire on alarm edge transitions in the 1 Hz supervisor.

---

## Troubleshooting

**"Permission denied" in the card.**
You denied notifications for the site. Open the browser's site settings and
change Notifications to "Allow", then toggle the checkbox again.

**Brave browser / blocked push.**
Brave's aggressive ad-blocking can disable FCM. Look for a "Brave Shields"
icon in the address bar and check whether push is being blocked, or try
Chrome/Firefox.

**Certificate not trusted.**
If the card shows the unavailable hint about HTTPS but you're already on
the HTTPS origin, the self-signed cert may not be trusted. Follow the
installation steps above for your OS, then reload.

**No internet on the Pi at the lake.**
The alarm fires and logs one line ("push: send failed for ..."). The
phone never receives that notification. Tether the Pi to the phone's hotspot
or dock on home WiFi to enable push delivery.

**Subscription count shows 0 after app reinstall.**
Reinstalling or clearing the browser's site data clears the local
PushManager registration. Open Settings -> Sound & touch -> Notifications and
toggle the checkbox to re-subscribe. The server-side subscription from the
previous install is pruned automatically when the push service returns 404/410.
