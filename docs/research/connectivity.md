# Vanchor-NG connectivity: phone-as-controller while keeping the phone's internet

*Research note — 2026-06-25. No app code changed by this doc. Sources cited inline with URLs.*

## Problem statement

The control software runs on a **Raspberry Pi on the boat** and serves a web UI
(FastAPI + WebSocket on `0.0.0.0:8000`) plus an NMEA-TCP port (`:10110`). The
skipper wants to **use their phone (browser) as the controller** while the
**phone keeps its cellular internet** for everything else (messages, maps,
weather, etc.).

The original Vanchor ran the **Pi as a WiFi Access Point**; the phone joined it
and the AP had **no uplink**, so the phone lost all data. We want both:

1. The phone reaches the Vanchor web UI reliably, and
2. The phone keeps its own internet, and
3. **Control keeps working with no cell coverage offshore** (the boat link must
   not depend on the internet).

The app itself needs **no** internet: we already cache **map tiles in IndexedDB**
(`ui/static/offline.js`) and **routing charts** server-side. So the internet is
wanted purely for the phone's *other* apps, not for Vanchor.

A useful framing: requirement (3) means the **control path and the internet path
must be two independent links**. Any design that makes control ride *through* the
internet (cloud relay, VPN to a server) is disqualified offshore. Every option
below keeps control on a **local RF link** between phone and Pi; the only
question is how the phone *also* gets internet on top.

---

## The five approaches, compared

| # | Approach | Phone keeps internet? | Control works offshore (no cell)? | Extra hardware | Setup complexity | Reliability on a boat | iOS/Android quirks |
|---|----------|:---:|:---:|----------------|------------------|-----------------------|--------------------|
| **1** | **Phone hotspot + Pi joins as WiFi client** | **Yes** (phone keeps cellular; hotspot is built on top of it) | **Yes** — hotspot LAN keeps working with zero internet; Pi & phone are on the same subnet | None | Low–Med (Pi must auto-join the SSID) | **Good** — single radio link, no captive-portal games. Main risk: hotspot sleeps when phone screen locks *with no active client* | iOS hotspot needs "Maximize Compatibility" (2.4 GHz) + Auto-Lock long; a continuously-polling Pi keeps it awake. Android varies by OEM. |
| **2** | **Pi as AP, phone keeps cellular for "internet"** | **Maybe** — depends on OS heuristics that Apple/Google actively change; unreliable | **Yes** — AP LAN is local | None | Med (captive-portal suppression is fiddly) | **Fragile** — relies on the OS choosing to keep cellular up on a "no-internet" WiFi; both OSes regressed this | iOS shows "no internet" + may nag; Android "Mobile data always active" is broken on many Android 11+ devices |
| **3** | **Pi as AP with its own LTE uplink** (USB modem / cellular HAT + SIM) sharing via NAT | **Yes** (when there's cell coverage) | Control: **Yes** (LAN). *Internet:* no, offshore — but that's expected anywhere | **Yes** — modem/HAT, SIM, antenna, power | High | Good for control; internet limited by offshore coverage; more power/heat/failure surface | One network for everyone; nicest UX inshore. Needs data plan + good marine antenna |
| **4** | **Concurrent AP+STA on the Pi** (built-in chip both roles, or 2nd USB dongle) | **Yes** when joined to a marina/phone WiFi that has internet | Control: **Yes** (Pi AP LAN). Internet only when the upstream WiFi has it | None (single-chip) or a $10–15 USB dongle | High | **Single-chip AP+STA is officially unsupported and crashes** on Pi's Broadcom chip; a **2nd dongle** is reliable | Marina-WiFi dependent; not an offshore internet solution |
| **5** | **App-side robustness** (mDNS `vanchor.local`, PWA/installable, WS reconnect, bind all interfaces) | n/a — orthogonal | Makes control *more* robust on any transport | None | Low | Strictly improves every option above | Bonjour/`.local` works natively on iOS; Android 12+ resolves `.local` too |

Approach 5 is **not an alternative** to 1–4 — it is the layer that makes whichever
transport you pick pleasant and dependable, and **should be done regardless**.

---

## Recommendation

> **Primary: Approach 1 (Phone hotspot + Pi as WiFi client), hardened with Approach 5 (mDNS + PWA + WS reconnect).**
>
> **Fallback: Approach 3 (Pi-as-AP with its own LTE uplink)** when there is a
> *second, fixed* phone/tablet that should also see the UI, or when the skipper
> wants one shared boat network and is willing to add a modem. For pure
> marina-WiFi internet, **Approach 4 with a second USB dongle**.
>
> **Avoid Approach 2 as the primary design** — it depends on OS captive-portal
> heuristics that Apple and Google keep changing and that are widely reported
> broken. It is fine as a *degraded* mode but not as the plan.

### Why Approach 1 wins for *this* use case

- **Phone keeps cellular internet by construction.** A personal hotspot is built
  *on top of* the phone's cellular data — the phone is the router. The phone has
  full internet whenever it has signal; the Pi is just one more client on the
  hotspot LAN. There is no "WiFi has no internet" dilemma because the phone isn't
  joining anything — it's *hosting*.
- **Control survives loss of cell signal.** When the boat goes offshore and the
  phone loses cellular, the **hotspot WiFi LAN stays up** (the phone keeps
  broadcasting its SSID and routing the local subnet). The phone↔Pi link is
  unaffected, so **autopilot control keeps working**. The phone simply shows "no
  internet" for its other apps — exactly the acceptable trade.
- **One RF link, no captive-portal games.** Unlike Approach 2 you never fight the
  OS's "sign in to WiFi" nag, because the phone is the AP, not a confused client.
- **Zero extra hardware / cost / power / antenna** vs. Approaches 3 and 4.
- **Best reliability for a single-helmsman boat**, which is the common case.

### The one real risk in Approach 1, and the fix

iPhone (and some Android) **personal hotspots go to sleep / stop advertising when
the phone's screen locks *and no client is actively using the link***
([Apple Community: hotspot disconnects on sleep](https://discussions.apple.com/thread/254531927),
[MacRumors: persistent hotspot on sleep](https://forums.macrumors.com/threads/ios13-persistent-hotspot-hotspot-disconnects-when-device-is-in-sleep-mode.2212471/)).
Two mitigations, used together:

1. **Keep a client actively talking.** The Pi holds a persistent WebSocket/NMEA
   link and we already poll telemetry continuously — an *active* client keeps the
   hotspot awake. Our **systemd watchdog that re-pings the gateway** (below)
   guarantees steady traffic so the hotspot doesn't idle out.
2. **Phone settings:** set **Auto-Lock → Never** (or longest) while underway, and
   on iPhone enable **Personal Hotspot → Maximize Compatibility** (forces 2.4 GHz,
   which is what the Pi 3/Zero radios prefer and which has better range over open
   water) ([Apple Support: Personal Hotspot not working](https://support.apple.com/en-us/119837),
   [Tom's Guide: fix iPhone hotspot](https://www.tomsguide.com/phones/iphones/i-fixed-my-iphone-hotspot-issues-with-these-5-simple-steps)).
   Disable **Low Power Mode** and **Low Data Mode**, both of which kill hotspots.

Range note: a phone hotspot is low-power 2.4/5 GHz; expect solid coverage across
a small/medium boat. It is **not** a long-range link — for a big vessel or
helm-to-bow distance with metal in between, prefer the Pi-as-AP options with a
proper external antenna.

---

## Copy-pasteable setup — Approach 1 (recommended)

Target: **Raspberry Pi OS Bookworm (Pi 3/4/5)**, which uses **NetworkManager**
(`nmcli`) by default. Older Bullseye/`dhcpcd`+`wpa_supplicant` variant noted at the
end. The phone's hotspot is the WiFi network; the Pi joins it on boot.

### A. Auto-join the phone's hotspot on boot (NetworkManager / `nmcli`)

```bash
# One-time: create a saved connection for the phone's hotspot.
# Replace SSID and PASSWORD with the phone's Personal Hotspot name/password.
sudo nmcli connection add type wifi con-name boat-hotspot ifname wlan0 \
    ssid "Skipper-iPhone"
sudo nmcli connection modify boat-hotspot \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "hotspot-password" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    connection.autoconnect-retries 0          # 0 = retry forever (boat may boot before phone)

sudo nmcli connection up boat-hotspot
```

`autoconnect-retries 0` matters on a boat: the Pi often powers up **before** the
phone's hotspot is on. NetworkManager will keep retrying until the hotspot
appears. You can save **several** hotspots (skipper's + crew's phone) with
different priorities; NM joins the highest-priority one that's present.

> iPhone hotspot SSIDs contain the device name (e.g. "Alex's iPhone"); the
> apostrophe/case must match exactly. Renaming the iPhone (Settings → General →
> About → Name) to something ASCII-simple avoids quoting pain.

### B. A keep-alive watchdog (prevents hotspot idle-sleep + auto-recovers the link)

```ini
# /etc/systemd/system/boat-link-keepalive.service
[Unit]
Description=Keep phone hotspot awake and rejoin if dropped
After=NetworkManager.service

[Service]
ExecStart=/usr/local/bin/boat-link-keepalive.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
# /usr/local/bin/boat-link-keepalive.sh   (chmod +x)
#!/usr/bin/env bash
set -u
while true; do
  GW=$(ip route | awk '/default/ {print $3; exit}')   # hotspot gateway = the phone
  if [ -n "${GW:-}" ]; then
    ping -c1 -W2 "$GW" >/dev/null 2>&1                  # steady traffic keeps hotspot awake
  else
    nmcli connection up boat-hotspot >/dev/null 2>&1    # link dropped: rejoin
  fi
  sleep 10
done
```

```bash
sudo systemctl enable --now boat-link-keepalive.service
```

### C. Reach the Pi by name, not IP — advertise `vanchor.local` via Avahi (mDNS)

On a phone hotspot the Pi gets a DHCP address you don't control (often
`172.20.10.x` on iPhone). Don't chase the IP — publish an mDNS name.

```bash
sudo apt update && sudo apt install -y avahi-daemon
sudo hostnamectl set-hostname vanchor      # → resolves as vanchor.local
sudo systemctl enable --now avahi-daemon
```

Optionally advertise the HTTP service explicitly (nice for discovery, gives the
phone a tappable Bonjour service):

```xml
<!-- /etc/avahi/services/vanchor.service -->
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Vanchor on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>8000</port>
  </service>
</service-group>
```

Now the skipper opens **`http://vanchor.local:8000`** in the phone browser.
- **iOS/macOS**: Bonjour/`.local` works natively, always.
- **Android 12+**: resolves `.local` mDNS natively. On older Android, `.local`
  may fail in some browsers — fall back to the IP (`172.20.10.x:8000`) or a
  saved bookmark. (This is one more reason to ship the PWA in §5: an installed
  PWA pins the origin.)

### D. The app is already bound correctly

`src/vanchor/app.py` runs uvicorn with `--host 0.0.0.0` (see the run command
`vanchor --host 0.0.0.0 --nmea-tcp`), so it already listens on **all
interfaces**, including the hotspot-assigned address — no change needed. Just run
it as a service so it starts on boot:

```ini
# /etc/systemd/system/vanchor.service
[Unit]
Description=Vanchor-NG autopilot
After=network-online.target
Wants=network-online.target

[Service]
User=alex
WorkingDirectory=/home/alex/projects/vanchor-ng
ExecStart=/home/alex/projects/vanchor-ng/.venv/bin/vanchor --host 0.0.0.0 --nmea-tcp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now vanchor.service
```

> Note `vanchor.service` does **not** depend on `boat-hotspot` being up. Binding
> `0.0.0.0` means the app serves on `127.0.0.1` and `vanchor.local` regardless,
> so control comes up even before/without the hotspot — exactly the
> independence we want offshore.

### Bullseye / `dhcpcd` variant (older Pi OS)

If the Pi still uses `wpa_supplicant`, add the hotspot to
`/etc/wpa_supplicant/wpa_supplicant.conf`:

```conf
network={
    ssid="Skipper-iPhone"
    psk="hotspot-password"
    priority=100
}
```

then `sudo wpa_cli -i wlan0 reconfigure`. The Avahi and systemd steps above are
identical. (Bookworm/`nmcli` is recommended; prefer upgrading.)

---

## Copy-pasteable setup — Approach 2 (Pi-as-AP, suppress captive-portal nag)

Use this only as a **fallback/secondary** (e.g. you want a fixed boat SSID and
accept that "phone keeps internet" is best-effort). The goal of the captive-portal
config is the opposite of a normal captive portal: we want the OS to **quickly and
quietly decide "this WiFi has no internet"** and *not* hijack the connection or
nag, so the user can keep using cellular for data.

### hostapd (the AP itself)

```conf
# /etc/hostapd/hostapd.conf
interface=wlan0
driver=nl80211
ssid=Vanchor
hw_mode=g
channel=6
country_code=SE
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=changeme-strong
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
```

### dnsmasq (DHCP + DNS on the AP subnet)

```conf
# /etc/dnsmasq.d/vanchor.conf
interface=wlan0
dhcp-range=192.168.50.10,192.168.50.100,255.255.255.0,12h
dhcp-option=3,192.168.50.1     # gateway = the Pi
dhcp-option=6,192.168.50.1     # DNS = the Pi
address=/vanchor.local/192.168.50.1
```

Static IP for the AP interface (NetworkManager: mark `wlan0` as a shared/AP
connection, or with the legacy stack set `192.168.50.1/24` on `wlan0`).

### Captive-portal: tell both OSes "no internet" *gracefully* (don't hijack)

Modern phones probe well-known URLs to decide if a network has working internet:

- **Android / Chrome:** `http://connectivitycheck.gstatic.com/generate_204` (and
  `clients3.google.com/generate_204`) — expects an empty **HTTP 204**.
- **Apple:** `http://captive.apple.com/hotspot-detect.html` — expects a body
  containing exactly `Success`.

(Mechanism: if the probe doesn't get the expected success response, the OS marks
the network as captive and pops the "Sign in to WiFi" sheet —
[Medium: how captive portals work on a Pi](https://medium.com/@jbrathnayake98/how-captive-portals-work-building-one-from-scratch-on-raspberry-pi-f7da1601719b).)

For a "no-internet but no-nag" AP, the cleanest behaviour is to **let the probe
fail naturally** (the Pi has no uplink, so the probe to those hosts simply gets no
answer) — which makes the OS mark the WiFi "no internet" and (on a phone with
cellular) keep using mobile data. The thing to avoid is *intercepting* those
domains and returning a redirect/portal, because that triggers the "Sign in"
hijack. So:

- **Do NOT** add dnsmasq `address=/captive.apple.com/...` or
  `address=/connectivitycheck.gstatic.com/...` rewrites. Leave them un-answered.
- With **no uplink the probes time out** → both OSes settle on "Connected, no
  internet" without a sign-in page. The downside is the timeout is slow and the
  banner is "ugly".
- To make Android decide *fast and quietly*, you can serve an explicit **HTTP 204**
  for its probe so it concludes "validated" and stops nagging — but note this
  also makes Android think it *has* internet, which can make it route the phone's
  data over the (uplink-less) WiFi. **That re-creates the original "phone lost
  internet" bug.** So returning 204 is the *wrong* choice for our goal; **letting
  the probe fail is correct here.**

### Per-OS reality (why this is the fallback, not the plan)

- **iOS:** when a joined WiFi has no internet, iOS marks it "No Internet
  Connection" and **does not automatically use cellular for general app traffic**
  on that WiFi in a reliable, documented way; users frequently must toggle WiFi
  off or "Use Cellular" per app. There is no robust global "use cellular when
  WiFi has no internet" switch. (iOS 18+ adds *Wi-Fi Assist* / connectivity-assist
  features that lean on cellular for *speed*, but they are about a working-but-slow
  WiFi, not a no-uplink one, and are not a guarantee —
  [iDownloadBlog](https://www.idownloadblog.com/2023/04/26/how-to-stop-iphone-from-disconnecting-wi-fi-and-using-cellular-data/).)
- **Android:** the developer-option **"Mobile data always active"** and the
  intended "switch to mobile when WiFi has no internet" behaviour are **widely
  reported broken on Android 11+** and OEM-dependent (Samsung's "Intelligent
  Wi-Fi" is the closest working version)
  ([XDA: dev option no longer functional](https://xdaforums.com/t/dev-option-mobile-data-always-active-no-longer-functional.4440237/),
  [XDA: force mobile data when WiFi has no internet](https://xdaforums.com/t/force-android-to-use-mobile-data-when-wifi-connected-but-has-no-internet.4501535/),
  [Samsung Community: Intelligent Wi-Fi fails to switch](https://eu.community.samsung.com/t5/galaxy-s25-series/quot-intelligent-wifi-quot-fails-to-switch-to-cellular-data-when/td-p/13265574)).

Because both of these are heuristics the vendors keep changing and breaking,
**Approach 2 cannot be trusted to keep the phone's internet** — hence it is the
fallback, and Approach 1 (where the phone is the router and never has this
dilemma) is the recommendation.

---

## Approach 3 — Pi-as-AP with its own LTE uplink (best inshore UX, needs gear)

One boat network: the Pi runs the AP **and** has its own internet via a cellular
modem, NAT-shared to every device on the AP. Phone and Pi share one SSID; phone
gets both control and internet (when there's coverage). Control still rides the
**local AP LAN**, so it survives offshore even when the LTE drops.

Hardware options (2.4/5 GHz AP from the Pi's built-in radio + a cellular uplink):

| Uplink option | Notes | Rough cost |
|---|---|---|
| **USB LTE dongle** (e.g. Huawei E3372 / Quectel-based) | Appears as `usb0`/`wwan0`; cheapest; modest antenna | ~$30–60 + SIM |
| **Cellular HAT** (Sixfab, Waveshare SIM7600/RM5xx) | Cleaner integration, GPS too, external SMA antenna, more power draw | ~$60–150 + SIM |
| **Marine 4G/5G router upstream**, Pi joins by Ethernet/WiFi | Best antennas/range; the Pi need not handle cellular at all | $150–400 |

NAT/sharing sketch (uplink = `wwan0`, AP = `wlan0`):

```bash
sudo sysctl -w net.ipv4.ip_forward=1     # persist in /etc/sysctl.d/
sudo iptables -t nat -A POSTROUTING -o wwan0 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -o wwan0 -j ACCEPT
sudo iptables -A FORWARD -i wwan0 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
```

Use the §2 hostapd+dnsmasq, **with** internet now present (so captive checks pass
honestly). Trade-offs: extra cost/power/heat, a SIM/data plan, an antenna, and
the internet half is still useless offshore where there's no cell coverage — but
**control is unaffected** because it's the local LAN. Pick this when more than one
device needs the UI, or the skipper wants a single shared boat network.

---

## Approach 4 — Concurrent AP + STA on the Pi

Goal: Pi joins a **marina/phone WiFi for internet** *and* runs its **own AP** for
the helm. Two sub-variants:

- **Single built-in chip doing both (AP+STA):** *technically possible but
  officially unsupported and unstable* on the Pi's Broadcom chip. Both roles are
  forced onto **one channel** (the STA's), and connecting clients can crash the
  firmware. RaspberryPi/Linux issue **#7092** documents a consistent
  **BCM43455 firmware crash in concurrent STA+AP** on recent kernels, and the
  RaspAP docs explicitly call AP-STA mode "completely unsupported … for
  educational purposes only," recommending a second adapter for anything reliable
  ([raspberrypi/linux #7092](https://github.com/raspberrypi/linux/issues/7092),
  [RaspAP AP-STA docs](https://docs.raspap.com/ap-sta/),
  [Infineon: concurrent station+AP](https://community.infineon.com/t5/Wi-Fi-Bluetooth-for-Linux/cyw43438-concurrent-station-and-AP-mode/td-p/116814)).
  **Do not rely on single-chip AP+STA for a boat autopilot.**
- **Second USB WiFi dongle (recommended if you want this at all):** dongle = STA
  (joins marina WiFi for internet), built-in `wlan0` = AP. Two independent radios,
  each on its own channel → **stable**. A ~$10–15 RTL8188/8192-class dongle works;
  then NAT-share from the dongle's STA interface to the AP exactly as in §3.

Either way this is a **marina-WiFi** internet story, not an offshore one. For
*this* use case (phone keeps *its own* internet, offshore) it is strictly worse
than Approach 1. Keep it in mind only if the boat lives at a marina with WiFi and
you want the Pi online for updates/telemetry.

---

## Approach 5 — App-side robustness (do this regardless of transport)

These changes make the UI dependable on **any** of the links above and remove the
need to type IPs. They are independent and low-risk.

**Status in the current codebase:**

| Item | Status | File |
|---|---|---|
| Bind on all interfaces (`0.0.0.0`) | **Done** — `vanchor --host 0.0.0.0` | `src/vanchor/app.py` (`--host`, uvicorn `host=config.server.host`) |
| WebSocket auto-reconnect | **Done** — `ws.onclose` retries every 1 s | `src/vanchor/ui/static/core.js:95-97` |
| Offline map tiles (IndexedDB) + offline routing charts | **Done** | `src/vanchor/ui/static/offline.js` |
| mDNS / Bonjour `vanchor.local` | **Not yet** — needs Avahi on the Pi (see §C above) | OS-side, not app |
| **PWA: installable + offline app-shell (service worker + manifest)** | **Not yet** — no `manifest.webmanifest` / `sw.js`; `index.html` has no `<link rel="manifest">` | `src/vanchor/ui/static/` |

The one app-side gap worth closing is the **PWA**. Today the *map tiles* are
cached, but the **HTML/JS/CSS app shell is not** — so with **zero** internet and
a fresh browser the page can fail to load. A service worker that precaches the
shell makes the UI **installable to the home screen** and **load with no internet
at all**, which is exactly the offshore scenario.

Minimal PWA sketch (illustrative — to be implemented in a separate task, not by
this doc):

```html
<!-- index.html <head> -->
<link rel="manifest" href="/static/manifest.webmanifest" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
```

```json
// manifest.webmanifest
{
  "name": "Vanchor", "short_name": "Vanchor",
  "start_url": "/", "display": "standalone",
  "background_color": "#0a0e14", "theme_color": "#0a0e14",
  "icons": [{ "src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png" }]
}
```

```js
// sw.js — precache the app shell so it loads offline
const SHELL = "vanchor-shell-v1";
const ASSETS = ["/", "/static/index.html", "/static/style.css",
  "/static/core.js", "/static/map.js", /* …other JS modules… */];
self.addEventListener("install", e =>
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS))));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;            // never cache WS/POST
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
```

```js
// register in core.js / app.js
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
```

> Caveats: the service worker must **only** handle GET asset requests — never the
> WebSocket (`/ws`) or `POST /api/*` command traffic. iOS only allows
> service-worker install over `http://localhost`/`https://` or trusted contexts;
> on a plain `http://vanchor.local` LAN origin, iOS Safari support for installed
> PWAs is limited — test on the target iOS version. Android/Chrome installs
> cleanly over LAN HTTP. Bump `vanchor-shell-vN` on each UI release to refresh the
> cache.

---

## TL;DR for the skipper

1. **Use the phone's Personal Hotspot. Let the Pi join it.** Phone keeps its
   internet; control still works when you lose cell signal offshore.
2. On the Pi: save the hotspot in NetworkManager (`autoconnect`,
   `autoconnect-retries 0`), run the **keep-alive watchdog** (stops the hotspot
   idling out), install **Avahi** so the UI is at **`http://vanchor.local:8000`**,
   and run Vanchor as a **systemd service** bound to `0.0.0.0`.
3. On the phone: **Auto-Lock = Never** while underway, iPhone **Maximize
   Compatibility = On**, **Low Power / Low Data = Off**.
4. **Fallback:** add an **LTE modem to the Pi-as-AP** (one shared boat network,
   internet inshore) — or a **second USB WiFi dongle** for marina-WiFi internet.
   Don't rely on "Pi-as-AP and hope the phone keeps cellular" (Approach 2) — the
   OS heuristics for that are unreliable.
5. **Regardless:** finish the **PWA** (service-worker app-shell precache +
   manifest) so the UI installs to the home screen and loads with **zero**
   internet.

### Sources

- iPhone Personal Hotspot sleep/disconnect on screen lock — [Apple Community](https://discussions.apple.com/thread/254531927), [MacRumors](https://forums.macrumors.com/threads/ios13-persistent-hotspot-hotspot-disconnects-when-device-is-in-sleep-mode.2212471/)
- iPhone hotspot fixes / Maximize Compatibility / Low Power Mode — [Apple Support 119837](https://support.apple.com/en-us/119837), [Tom's Guide](https://www.tomsguide.com/phones/iphones/i-fixed-my-iphone-hotspot-issues-with-these-5-simple-steps)
- iOS WiFi-vs-cellular behaviour / Wi-Fi Assist — [iDownloadBlog](https://www.idownloadblog.com/2023/04/26/how-to-stop-iphone-from-disconnecting-wi-fi-and-using-cellular-data/)
- Android "Mobile data always active" broken on 11+ / Intelligent Wi-Fi — [XDA #1](https://xdaforums.com/t/dev-option-mobile-data-always-active-no-longer-functional.4440237/), [XDA #2](https://xdaforums.com/t/force-android-to-use-mobile-data-when-wifi-connected-but-has-no-internet.4501535/), [Samsung Community](https://eu.community.samsung.com/t5/galaxy-s25-series/quot-intelligent-wifi-quot-fails-to-switch-to-cellular-data-when/td-p/13265574)
- Captive-portal detection mechanism (generate_204 / captive.apple.com) on a Pi — [Medium](https://medium.com/@jbrathnayake98/how-captive-portals-work-building-one-from-scratch-on-raspberry-pi-f7da1601719b)
- Concurrent AP+STA on Pi is unsupported/unstable; use a 2nd adapter — [raspberrypi/linux #7092](https://github.com/raspberrypi/linux/issues/7092), [RaspAP AP-STA](https://docs.raspap.com/ap-sta/), [Infineon community](https://community.infineon.com/t5/Wi-Fi-Bluetooth-for-Linux/cyw43438-concurrent-station-and-AP-mode/td-p/116814)
