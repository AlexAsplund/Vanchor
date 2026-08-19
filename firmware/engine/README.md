# engine.ino — legacy Arduino thrust firmware

> [!IMPORTANT]
> **This is not the preferred path for new builds.** One Raspberry Pi
> **Pico 2 / Pico** now replaces BOTH Arduino boards (engine + steering) with a
> single firmware: [`helm-pico` in the vanchor-pcb repo](https://github.com/AlexAsplund/vanchor-pcb/tree/master/firmware/helm-pico).
> It speaks the same protocol, adds an AS5600 magnetic encoder instead of the
> feedback pot, ships as drag-and-drop UF2 files, and can be flashed from the
> vanchor app (Devices → Motor → Helm firmware). It is where new features land.
>
> `engine.ino` remains maintained for existing boats (it is what runs on the
> author's boat today) and as the reference for the hijacked-speed-controller
> approach, but new functionality (NMEA2000 etc.) will target the Pico.

Wiring, pin map, BOM and bring-up for this sketch: see the
["Engine board" section of ../README.md](../README.md#2-engine-board--hijacked-commercial-speed-controller).
