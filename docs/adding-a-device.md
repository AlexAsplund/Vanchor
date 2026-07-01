# Adding a new device (sensor or motor)

This is the friendly, big-picture guide to teaching Vanchor about a new piece of
hardware — a compass/IMU, a GPS, a depth sounder, a motor board. If you want the
terse engineering reference (exact signatures, the checklist), see
[the developer guide](llms/device-drivers.md). This page is the "what and why".

## The one idea that makes it simple

Vanchor never talks to your hardware directly. Every sensor — the simulator's
fake one *and* a real wired one — does the same thing: it reads its device and
**puts out standard NMEA sentences** (the little `$..` text messages marine
gear uses). The rest of Vanchor — the map, the autopilot, every mode — only ever
reads those sentences. It has no idea whether they came from a simulator, a USB
GPS, or your driver.

So "add a compass" really means: **read your compass, turn the reading into an
`HDM` heading sentence, and hand it to Vanchor.** That's the whole job. (A motor
is the mirror image: it receives a steering/thrust command and drives the ESC.)

Because of this, your driver works identically in the simulator and on the water,
and you can test it on your laptop with no hardware plugged in.

## How Vanchor discovers your driver

You don't edit Vanchor's startup code to add hardware. You drop a single file
into `src/vanchor/hardware/drivers/`, and at start-up Vanchor scans that folder
and picks it up automatically. Your file "registers" itself with one line, and
from then on your device shows up as a choice in **Settings → Devices** just like
the built-in ones.

Add a file → it appears in the app. Remove the file → it's gone. Nothing else to
touch.

## What a driver file contains

Three small pieces (all in one file — copy `drivers/hwt901b.py` as your template):

1. **The reader.** A small class that, on a timer, reads your device and publishes
   the right NMEA sentence. The one rule: if talking to the device can *block*
   (most USB/serial reads do), do it on a worker thread so the app stays smooth.
2. **The builder.** A tiny function that opens your device (using the port/baud
   from the settings) and hands back the reader. Keep any special library import
   *inside* this function, so people who don't use your hardware don't need to
   install it.
3. **The registration.** One line naming your device so it appears in the app.

If your device can't be opened (wrong port, unplugged, a missing library),
Vanchor doesn't crash — it logs a warning (visible in **Settings → View logs**)
and simply runs without that device. You fix the setting and restart.

## Giving your device its own settings screen

Some devices need a few knobs — an update rate, a calibration option, a
"declination" mode. Your driver can describe those, and Vanchor builds the little
settings panel for you automatically. You just list the fields (a dropdown, a
number box, an on/off switch) and any buttons (like "Calibrate" or "Show status").

Two nice touches you get for free:

- **The panel appears the instant you pick your device** in the dropdown — you
  don't have to save and restart first to see its options.
- **Your choices are remembered.** They're saved to the config and re-applied
  every time the device starts, so you set them once.

Buttons that actually talk to the hardware (like reading live status) only work
while the device is running; everything else works from the moment you select it.

## Connection settings (serial port, baud rate)

Wired devices need to know *which* port they're on (e.g. `/dev/ttyUSB1`). As soon
as you pick a wired device, Vanchor shows the port and baud-rate fields for you to
fill in — no need to select "serial" separately.

## Trying it without hardware

You don't need the real device to develop or test a driver. Because everything is
just "read a value → emit a sentence", you write a stand-in that returns a fake
reading and check that your driver emits the right sentence. That's how the
built-in drivers are tested, and it means the whole test suite runs on a laptop.

## The worked example

The WitMotion **HWT901B** compass driver (`drivers/hwt901b.py`) shows all of this
in one place: it reads a 9-axis AHRS, emits heading, offers a settings panel
(declination mode, update rate, calibration + status buttons), remembers its
settings, and even **learns its own compass offset from GPS** while you drive
straight (see the notes in that file). Read it alongside
[the developer guide](llms/device-drivers.md) and you'll have a template for
almost any device.

## Where to look next

- Deep reference + the exact API: [docs/llms/device-drivers.md](llms/device-drivers.md)
- How the device layer fits the rest of the system: [docs/llms/backend.md](llms/backend.md)
- A real driver to copy: `src/vanchor/hardware/drivers/hwt901b.py`
