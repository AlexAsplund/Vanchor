# Getting started with Vanchor-NG

A from-zero guide for boat owners. No coding experience needed, no boat
needed today — by the end you'll have a simulated boat holding a virtual
anchor on your own screen, and you'll know exactly which buttons do what.

Every step tells you what success looks like, so you always know whether it
worked. Take it at your own pace.

Want the big picture first? See the **[3D concept walkthrough](concept/index.html)** — if you're reading this on GitHub, view it on the project's GitHub Pages site or open `http://vanchor.local:8000/concept` once vanchor is running (the boat serves it)
— an animated tour of how the boat holds a GPS anchor, no install needed.

---

## 1. What Vanchor-NG is (in one breath)

Vanchor-NG is a **virtual anchor** for boats with an electric trolling motor.
Instead of dropping a metal hook to the bottom, the software quietly nudges
the boat with the motor so it holds one exact spot on the water — all by
itself, over any bottom, at any depth.

It has two pieces, and neither one is complicated to picture:

- **The boat brain** — a small always-on computer that lives on the boat
  (typically a Raspberry Pi, a credit-card-sized computer). It reads the
  GPS and steers the motor.
- **Your phone or laptop** — the screen. You open a web page, see the boat
  on a map, and tap what you want it to do.

Here's the part that matters today: **you will try the whole thing as a
simulation, on your couch.** Vanchor-NG starts in full simulation by
default — a pretend boat on a pretend lake, with pretend wind. No boat, no
motor, no wiring, nothing that can move, spin, or get wet. Everything you
click is completely safe.

Two terms you'll see again, glossed once here:

- **GPS** — the satellite locator that tells the boat where it is, to within
  a few metres.
- **Drift** — the boat sliding off its spot because wind or current is
  pushing it. Fighting drift is the virtual anchor's whole job.

Want the visual version first? The [animated concept page](concept/) tells
the story in about 60 seconds. The curious can skim the full
[feature list](FEATURES.md) — but you don't need any of it to continue.

## 2. Before you start (what you actually need)

The entire shopping list for today:

- **A laptop or desktop computer** — Mac, Windows, or Linux all work.
- **About 10 minutes.**

That's it. No boat, no trolling motor, no wiring, no soldering, nothing to
buy. There is a hardware chapter near the end of this guide, but it's
optional and comes much later — after you're comfortable in the simulator.

One prerequisite: **Python 3.11 or newer** must be installed. Python is the
free programming language Vanchor-NG is written in; you won't be writing
any of it, your computer only needs it installed to run the app. To check
whether you have it, open a terminal (the text window where you type
commands — on Mac it's the app called *Terminal*, on Windows it's
*PowerShell*) and type:

```bash
python --version
```

If it prints `Python 3.11` or higher, you're set. If it prints an older
number or an error, download the current version from
[python.org](https://www.python.org/downloads/) and run the installer, then
check again. (On some systems the command is `python3` instead of `python`.)

One more reassurance: everything in this guide runs on your own machine.
Nothing is uploaded to the internet, no account is created, and once the map
has loaded it even works offline.

## 3. Install it (copy, paste, done)

Open a terminal in the folder where you keep the Vanchor-NG code (if you
downloaded it as a ZIP from GitHub, unzip it and open a terminal in that
folder). Then run these two lines, one after the other:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,routing]"
```

What each part does, in plain words:

| You typed | What it means |
|---|---|
| `python -m venv .venv` | Create a **venv** — a private sandbox folder (named `.venv`) so this software can't disturb anything else on your computer. |
| `. .venv/bin/activate` | Step into that sandbox. Everything you install now stays inside it. |
| `pip install -e .` | **pip** is Python's built-in downloader. This tells it to download and set up Vanchor-NG and its helpers. |
| `"[dev,routing]"` | Also grab two optional extras: the testing tools (`dev`) and the smart water-route-finding parts (`routing`). |

> **On Windows** the second half of the first line is different: run
> `python -m venv .venv` and then `.venv\Scripts\activate` instead of
> `. .venv/bin/activate`. Everything else is identical.

The install prints a lot of text and takes a minute or two. When your prompt
comes back without a red error message, it worked.

Now start the boat brain:

```bash
vanchor --host 0.0.0.0 --port 8000
```

What each part does:

| You typed | What it means |
|---|---|
| `vanchor` | Start the boat brain (in simulation, since no hardware is configured). |
| `--host 0.0.0.0` | Let other devices on your Wi-Fi — like your phone — reach it too, not only this computer. |
| `--port 8000` | The "door number" the app answers on. Web addresses can include one; you'll type it in the next step. |

**What success looks like:** a few lines of startup text scroll by and then
stop, with the server listening (you'll see the address and port mentioned,
and a line about an HTTPS listener on port 8443 — more on that in
chapter 9). **Leave this window open.** It *is* the boat brain — closing
the window stops the boat.

## 4. Open the app (you're already "on the water")

Open a web browser (Chrome, Safari, Edge, Firefox — any of them) and go to:

```
http://localhost:8000
```

**localhost** means "this same computer" — the address says "open the app
running right here", nothing goes out to the internet to find it.

Vanchor-NG starts in **full simulation by default**. What you're looking at
is a pretend boat on a pretend lake, drifting in pretend wind. It is safe to
tap, drag, and press anything on this screen — there is no motor to spin
and nothing you press can damage anything.

**What success looks like:** a dark map appears; the chip in the top bar
flips from "connecting…" to a green **connected**; the **GPS**, **SOG**,
**HDG** and **DEPTH** chips fill in with numbers; and a boat marker sits on
the map. That's the whole system running.

<video src="media/first-launch.mp4" controls muted playsinline width="640">
  Your browser can't show the clip inline — <a href="media/first-launch.mp4">open first-launch.mp4</a> instead.
</video>

*Clip: the app starting. The connection chip turns green, the instrument
chips fill in, and the boat marker appears on the chart.*

If the page doesn't load, jump to [chapter 12](#12-if-something-goes-wrong-troubleshooting)
— the fixes are short.

## 5. A tour of the map screen

Everything lives on one screen. From top to bottom:

**Map orientation:** the small compass button under the zoom controls flips
the chart between **north-up** (the default) and **heading-up**, where the
map rotates so "up" is wherever the bow points — like a car navigator. The
rotation is smoothed and ignores small compass wiggle, so the chart doesn't
wobble while you sit still. The needle on the button always points at north.

**The top status bar (the "chips").** Small live readouts:

- **The connection chip** (green dot, says *connected*) — the browser is
  talking to the boat brain. If it ever says *reconnecting…*, the page has
  lost touch with it.
- **GPS** — satellite fix quality. *OK* means the boat knows where it is;
  *NO FIX* means it doesn't.
- **SOG** — Speed Over Ground: how fast you're actually moving across the
  map, in knots.
- **HDG** — Heading: which way the bow (the front) is pointing, in compass
  degrees.
- **DEPTH** — metres of water under you.
- A **battery** chip and a **ROUTE** chip appear when they have something to
  say — battery charge, and route progress as *distance travelled ▸
  distance remaining*.
- **LINK** — the health of the remote link when a phone is driving the boat.

**The HUD** (the floating instrument cluster): the big **Speed** number, a
compass rose under **Heading** that spins as the boat turns, **Depth**,
**Dist→Anchor** (how far you are from your anchor spot), and **Battery**.

**The mode rail** (the row of tiles along the bottom): this is where you
pick *what job the autopilot should do*. The tiles are **Manual** (you
drive), **Anchor** (hold a spot), **Heading** (hold a compass course),
**Route** (follow a path you draw), **APB** (follow an external
chartplotter — ignore this one for now), **Drift** (controlled slow drift),
**Stop**, **Remote** (a giant-button layout for phones), and **More**, which
opens the guided fishing modes (**Contour**, **Orbit**, **Troll**,
**Work Area**).

**The big STOP.** The most important control on the screen. One tap cuts
all motor commands — and it works in every mode, every time, no matter what
the autopilot is doing. On a phone it's the red **■ STOP** button that stays
visible even when the controls are collapsed; on the mode rail it's the
**Stop** tile. You'll practise with it in chapter 10.

**The view chips** (the four small icon buttons at the top right): preset
layouts of the same screen — **Chart view** (map first), **Helm view** (big
mode buttons and a dominant STOP), **Instruments view** (a large glance
HUD), and **Manual view** (big thrust and steering controls). Chart view for
looking at the map, Helm view for driving. Hover or long-press to see each
one's name.

## 6. Drive it yourself (Manual mode)

Before letting the autopilot do anything, get a feel for the boat by driving
it yourself — like a video-game joystick for the boat, and in the simulator
a completely consequence-free one.

1. Tap the **Manual** tile on the mode rail. A panel with a **steering
   wheel** (and a thrust slider above it) appears.
2. The wheel is the whole control in one place — a dial around a little
   boat, bow up:
   - **Drag the glowing handle around** the dial to point the motor head:
     up is straight ahead, right is starboard, straight down is astern.
   - **Drag it outward** for more power — the faint rings are 25/50/75/100%.
     Pull it back to the hub for zero. (The **Thrust** slider above does the
     same thing, with reverse left of centre.)
   - The **outer gold ring is a live compass** (N/E/S/W) that turns with the
     real boat; the readouts in the hub always show the head's angle both
     ways — relative to the bow *and* as a true bearing.
   - A small grey **ghost tick** on the inner ring is the *actual* head
     angle reported by the steering hardware, so you can see the head
     physically swing to your command.
3. Drag the handle a little to one side and outward. Watch the **SOG** chip
   climb from 0.0 and the boat carve a curve on the map.
4. Ease the handle back to the hub. The boat coasts to a stop.

**Relative / Absolute / Course** (the toggle above the wheel): in
*Relative* (the default) the handle stays where you put it relative to the
boat. In *Absolute* the handle holds a **compass bearing** — set it to N
and the motor keeps pushing north while the boat swings underneath. In
*Course* the boat **follows the straight line** drawn from where you
engaged it along that bearing (shown dashed on the chart) — wind can push
an *Absolute* boat sideways off its line, but *Course* steers back onto
it, like an invisible one-leg route.

**What success looks like:** SOG rises when you drag outward, the heading
changes when you drag sideways, and the boat glides to a halt when the
handle returns to the hub. That's the whole cause-and-effect the autopilot
will use on your behalf.

<video src="media/manual-driving.mp4" controls muted playsinline width="640">
  Your browser can't show the clip inline — <a href="media/manual-driving.mp4">open manual-driving.mp4</a> instead.
</video>

*Clip: one drag on the steering wheel sets direction and power; the boat
accelerates and turns, then coasts to a stop when the handle is eased back
to the hub.*

## 7. Drop a virtual anchor (the main event)

**Anchor hold** stores the boat's current GPS position and uses the motor
to keep the boat at it. No physical anchor is involved, so it works over
any bottom type and at any depth.

1. Tap the **Anchor** tile on the mode rail.
2. Set the **Anchor radius** slider — the size of the circle you're willing
   to drift within before the boat pushes back. Around 8 m is a comfortable
   start.
3. Press the button that says **Drop anchor here**.

**What happens:** a cyan anchor pin and a ring appear around the boat. Wind
and current push the boat away from the pin. The station-keeper steers the
motor toward the anchor point, applies thrust, and the boat moves back. The
cycle repeats for as long as the mode is active. The **Dist→Anchor** readout
in the HUD rises and falls with each cycle.

<video src="media/drop-anchor.mp4" controls muted playsinline width="640">
  Your browser can't show the clip inline — <a href="media/drop-anchor.mp4">open drop-anchor.mp4</a> instead.
</video>

*Clip: the **Smart station-keeping (learned)** and **Vectored thrust (full
rotation)** switches are turned on and the anchor is dropped. A steady
simulated wind and current push the boat off the spot; the station-keeper
drives it back. The view then zooms out and the basemap is switched to
**Topo**. A right-click on the water near the shoreline opens the map menu,
and **Along shoreline** plans a water-only route whose waypoints follow the
shoreline. A right-click on an island swaps that menu entry to **Loop around
island**, which plans a closed ring of waypoints around the island.*

The anchor panel has additional switches. None of them are needed today:

- **Hold heading while anchored** — also holds the bow on the heading the
  boat had when the anchor was dropped.
- **Smart station-keeping (learned)** — uses a learned neural-net
  station-keeper instead of the PID one. Falls back automatically if the
  model is unavailable.
- **Leif** — an experimental pure neural-net station-keeper that steers the
  motor through its full rotation. It has no PID fallback and uses more
  battery. Only one of Smart / Leif can be selected at a time.
- **Vectored thrust (full rotation)** — vectors the motor through its full
  rotation to push directly against wind and current, instead of the ±35°
  band. This is an option of the PID station-keeper.

The **Jog anchor** pad — the four arrow buttons under the drop button —
moves the anchor point a small step in the chosen direction, without
lifting and re-dropping.

## 8. Follow a route

Instead of holding one spot, the boat can travel a path you draw — handy
for trolling along a shoreline or motoring back to the ramp.

1. Tap the **Route** tile on the mode rail.
2. Press **＋ Add waypoints**, then tap the map a few times to drop
   **waypoints** — numbered stops the boat will visit in order. The app
   connects them with a line as you tap.
3. Press **▶ Start route**.

**What success looks like:** the **ROUTE** chip appears in the top bar
showing *distance travelled ▸ distance remaining* (plus time taken and time
left), and the boat tracks the line leg by leg, the active waypoint
highlight advancing as each one is reached.

There's also a faster one-tap version for a single destination: open the
**Go-to (tap map)** card below the mode panels, choose what happens **On
arrival** — **Anchor on arrival**, **Stop on arrival**, or **Keep
position** — then press **Tap map to go** and tap your destination. With
*Anchor on arrival* the boat drives there and drops the virtual anchor by
itself.

<video src="media/follow-route.mp4" controls muted playsinline width="640">
  Your browser can't show the clip inline — <a href="media/follow-route.mp4">open follow-route.mp4</a> instead.
</video>

*Clip: waypoints are added by tapping the map, a line connects them, and
the boat follows each leg to the final waypoint. Shown at 5× simulation
speed; the other clips are real time.*

One more thing you already installed: the `routing` extra from chapter 3
powers **Take me here (smart route)** in the Route panel — press **Pick
destination**, tap a spot, and it plans a route that stays on the water
instead of cutting across land and islands. It loads the waypoints for your
review; press **▶ Start route** when they look right.

A few extras once you're comfortable with routes:

- **Replace or Append** — if a route is already running (or unstarted
  waypoints are sitting in the editor) and you use any *Take me here*
  action — the go-to card, the map's press-and-hold menu, or a marker's
  route buttons — the app asks whether to **replace** the route or
  **append** the new waypoints to its end. Appending to a running route
  doesn't restart it; the boat keeps navigating.
- **Waypoint speeds** — press **and hold** a waypoint pin (about 3 s,
  works on pending and active routes) to open its menu and **Set speed…**:
  engine power in **%** or boat speed in **knots**. The boat adopts that
  speed *when it arrives at that waypoint* and keeps it for the following
  legs, until a later waypoint carries its own speed. If you change the
  speed by hand mid-route, your setting wins — until the next
  speed-carrying waypoint. Speed-carrying pins show their speed in the
  pin's tooltip and in the route editor list.
- **Save any route** — the **Save / Load routes** card saves the pending
  route, or, when nothing is pending, the route the boat is currently
  running — so you can bank a route you're already on. Waypoint speeds are
  saved with it.

## 9. Use your phone as the remote

On the water you'll want the controls in your hand, not on a laptop balanced
on a seat. Your phone becomes the helm over Wi-Fi.

**Connect:** with your phone on the same Wi-Fi network as the computer
running `vanchor`, open the phone's browser and go to:

```
http://vanchor.local:8000
```

**vanchor.local** is the boat brain announcing its own name on the local
network (a standard trick called mDNS), so you don't have to hunt for
number-addresses. If your network doesn't pass that name along, use the
computer's IP address instead — it's shown in the startup text in the
terminal window.

**What success looks like:** the same map appears on the phone, live. On a
phone the controls sit in a bottom sheet you can drag up and down, with
**■ STOP** always visible.

**The secure address:** two phone niceties — keeping the screen awake while
you're at the helm, and "installing" the app to your home screen — are only
allowed by browsers over **https** (the secure, encrypted version of a web
address). Vanchor-NG runs a second listener for exactly this:

```
https://vanchor.local:8443
```

> **A one-time scary screen, explained.** The first time you open the https
> address, the browser shows a "connection is not private" warning. That's
> because the boat brain made its own security certificate rather than
> buying one from a company — normal for a device on your own boat, and safe
> to accept here. Tap **Advanced** (or *Show details*), then **Proceed /
> visit website**. You only do this once per phone.

**Install it as an app:** Vanchor-NG is a **PWA** (Progressive Web App — a
website that behaves like an installed app). From the https address, use
your browser's *Add to Home Screen* (iPhone: the share button → *Add to Home
Screen*; Android: the menu → *Install app*). You get a full-screen helm with
its own icon that keeps working even if the network hiccups.

**Feel and hear the buttons:** on Android phones every button press gives a
short **vibration pulse** — heavier on STOP, a distinct buzz when a safety
alarm fires, and a confirmation pulse when a press-and-hold (waypoint menu,
map menu) registers. Handy with wet fingers on a rocking boat. (iPhones
don't expose vibration to browsers, so that switch is greyed out there.)

There are **sounds** too, synthesized on the device so they work offline.
Safety alarms escalate through three distinct sounds — a calm double beep
(low: battery reminders), a two-tone warble (medium: compass trouble) and a
fast siren (high: anchor drag, lost GPS, man overboard). Depth warnings are
the exception: they play a **sonar-style ping** — unmistakable but easy on
the ears, since shallow-water alerts can repeat on a long day. On top of
that: chimes for notifications, a distinct little melody for each mode you
engage (you'll learn to *hear* "anchor dropped" without looking), a ding
each time a waypoint is reached and a fanfare when the route completes,
plus subtle button ticks.

Both live under **Settings → Sound & touch**: a master switch and volume,
plus per-category switches (each with a ▶ preview button) so you can, say,
keep alarms and waypoint dings but silence button clicks.

## 10. Stopping and staying safe

The golden rule, worth repeating: **the big STOP halts all motor commands
instantly, from any mode, every time.** It is never hidden, never disabled,
and never waits for anything else to finish.

Practise it now, while it's free:

1. Get the boat moving — Manual mode with some **Thrust**, or a running
   route.
2. Tap **■ STOP** (on a phone it's always in view; on the desktop mode rail
   it's the **Stop** tile).

**What success looks like:** the thrust drops to zero at that instant and
the boat coasts down to a stop. That's it — no confirmation dialog, no
delay.

<video src="media/big-stop.mp4" controls muted playsinline width="640">
  Your browser can't show the clip inline — <a href="media/big-stop.mp4">open big-stop.mp4</a> instead.
</video>

*Clip: the boat is moving, STOP is pressed, and thrust cuts to zero. The
boat coasts to a stop and the **SOG** chip in the top bar falls to zero.*

In the simulator there is nothing to hurt — this section is practice for
real life, so that tapping STOP is muscle memory before it ever matters.

Beyond the button, Vanchor-NG carries a set of always-on guardrails. You
don't need to configure them today, but it's good to know they exist: open
the menu (the **☰** button, top right) and look under **Safety** to find
the shallow-water auto-stop (**Min depth**), red **no-go zones** you can
draw on the map, a **Stop motor on GPS-fix loss** failsafe, low-battery
**Return to Launch**, and the remote-link failsafe: if you're driving
**manually** and the connection drops, the motor stops (a dead-man's
switch); on an **active route or autopilot mode** the boat keeps flying its
mission — so locking your phone mid-route is fine — unless you set
`link_loss_continue_mission: false`, which instead holds position if
your phone drops off the network (if you were *hand-driving* at that moment
it stops the motor instead — a lost link while driving means stop, never
sail on).

## 11. Later: connecting real hardware

Read this chapter when you're comfortable in the simulator — and treat the
real build as its own project day, not something to squeeze in after this
guide. Nothing earlier in this guide required it, and the simulator remains
the best place to learn every mode.

> ## ⚠️ PROPS OUT OF WATER
>
> **For every first test: keep the propeller OUT of the water and the boat
> secured on land or a stand.** A trolling motor commanded by software can
> spin up and steer **without warning** — during startup, during
> calibration, or because of a configuration mistake. Never stand within
> reach of the propeller while testing. When in doubt: props out of water.

The parts, in plain terms:

- **The steering servo / gearbox** — a small motor (a *servo* is a motor
  that moves to a commanded position) that rotates the trolling-motor shaft
  left and right, doing the job your hand does on the tiller.
- **The thrust driver / ESC** — the electronics box (*Electronic Speed
  Controller*) that spins the propeller at a commanded power.

The autopilot reads GPS drift and commands both — exactly as it commanded
the simulated motor all through this guide. The software switch itself is
one flag:

```bash
vanchor --hardware
```

`--hardware` tells Vanchor to talk to the real motor instead of the pretend
one — but only run it **after** the wiring is done and bench-tested with
the props out of the water.

This guide deliberately stops short of wiring. The deeper docs take over
from here:

- **[Deploying on a Raspberry Pi](deploy-pi.md)** — the boat-ready install:
  OS setup, autostart, serving the app to your phone on the water.
- **[Custom hardware](custom-hardware.md)** — how the steering and thrust
  channels are wired up and configured, including split/independent setups.
- **[Firmware](../firmware/README.md)** — the Arduino sketches, schematics,
  pin maps, and wiring for the motor controllers.
- **[Safety matrix](safety-matrix.md)** — every failsafe, what triggers it,
  and what it does.

When in doubt: **props out of water.**

## 12. If something goes wrong (troubleshooting)

**"Address already in use" when starting `vanchor`.** Something else on
your computer is already using door number 8000. Pick another door:

```bash
vanchor --host 0.0.0.0 --port 8001
```

…and open `http://localhost:8001` instead (match the number you chose).

**The browser shows nothing.** Check two things: the terminal window
running `vanchor` is still open (closing it stops the boat brain), and the
address is typed exactly as `http://localhost:8000` — including the
`http://` and the `:8000`.

**The phone can't find it.** The phone and the computer must be on the
*same* Wi-Fi network — a guest network or mobile data won't reach it. If
`http://vanchor.local:8000` doesn't resolve on your network, use the
computer's IP address (shown in the startup text) in its place.

**The map looks blank offline.** The background map tiles come from the
internet the first time they're shown. Without a connection you may see a
dark, empty background — but the boat, the anchor ring, and every control
still work; only the scenery is missing. Tiles you've already viewed are
kept for offline use.

**Worried an experiment broke your settings?** Vanchor-NG keeps everything
it saves (boats, depth maps, trips) in one data folder, and you can point a
throwaway session at a fresh one with the `VANCHOR_DATA_DIR` setting — so a
wild test session never has to touch your real setup. The
[README's configuration section](../README.md#configuration) shows how.

**Still stuck?** Open an issue at the
[project's issues page](https://github.com/AlexAsplund/Vanchor/issues) —
"I followed the getting-started guide and got stuck at step X" is a genuinely
useful bug report — or browse the rest of the [documentation](README.md).

## 13. Where to go next

- **Watch the [animated concept page](concept/)** — the 60-second visual
  story of how station-keeping works, matching what you saw in chapter 7.
- **Try the fishing modes** once anchoring feels familiar: **More** on the
  mode rail opens **Contour** (follow a depth line), **Orbit** (circle a
  marked point), **Troll** (an S-curve weave), and **Work Area** (hop
  between spots, holding at each). There's [a guide per mode](modes/).
- **Make the boat yours.** Open **☰ → Boat & tuning** to pick or edit a
  boat profile, and run **⚙ Set up / calibrate boat** — a short automatic
  drive that measures the boat and tunes the autopilot to it. Works on the
  simulated boat too. (In the simulator, **☰ → Simulator → ⛵ Select Boat**
  swaps between preset hulls.)
- **Read before you buy.** The [hardware](custom-hardware.md),
  [Raspberry Pi deployment](deploy-pi.md) and [safety matrix](safety-matrix.md)
  docs are worth a read before any money changes hands.
- **Tell us what confused you.** This guide is written for non-technical
  boat owners, and the best way to keep it that way is hearing where it
  failed you. Report anything unclear on the
  [issues page](https://github.com/AlexAsplund/Vanchor/issues) — that's
  contributing, too.

Fair winds — and enjoy never hauling an anchor again.
