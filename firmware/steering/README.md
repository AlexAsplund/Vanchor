# steering.ino — legacy Arduino steering firmware

> [!IMPORTANT]
> **This is not the preferred path for new builds.** One Raspberry Pi
> **Pico 2 / Pico** now replaces BOTH Arduino boards (steering + engine) with a
> single firmware: [`helm-pico` in the vanchor-pcb repo](https://github.com/AlexAsplund/vanchor-pcb/tree/master/firmware/helm-pico).
> It speaks the same protocol, uses an AS5600 magnetic encoder instead of the
> feedback pot (no pot wear, multi-turn), ships as drag-and-drop UF2 files, and
> can be flashed from the vanchor app (Devices → Motor → Helm firmware). It is
> where new features land.
>
> `steering.ino` remains maintained for existing boats (it is what runs on the
> author's boat today), but new functionality will target the Pico.

Wiring, pin map, BOM and bring-up for this sketch: see the
["Steering board" section of ../README.md](../README.md#3-steering-board--closed-loop-azimuth).
The host-side protocol tests in `tests/` cover the shared parser used by BOTH
the Arduino and Pico firmwares — they stay authoritative regardless of path.
