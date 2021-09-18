# Vanchor

For controlling speed and direction of an electrical trolling motor.
The goal here is to be able to use (almost) any cheap trolling motor while still having autopilot/follow route features.

The current version gets it's coordinates from my Humminbird plotter. But it's easy to add support for cheap GPS modules that you hook up to the Raspberry.

**Keep in mind that many aspects of this build can cause fires, chopped of fingers and/or other dangers.**

Also, I learned the hard way that it is a good idea to get a wrist-strap or something similar for your phone :)

<img src="./3d/gearbox/Render_2.png" width="50%" />

## Current features (Everything is not tested yet):

- Web interface
  - Control of trolling motor
  - Configuration
- NMEA 0184 (TX/RX)
- NMEA through TCP (TX/RX)
  - Connect Navionics app, iNavX etc
- Virtual Anchor (Vanchor) based on position (NMEA RMC)
- Autopilot (NMEA APB sentence)
- Autopilot (GPX files)
- Lock heading (NMEA RMC sentence and/or e-compass)
- PID steering

## Planned

## Wishlist

- Some kind of simulation software
- PID profiles for boats of different sizes

## Details

### Web interface

Flask handles the web application.
It's based on bootstrap4 and js (+jquery) and has a compass that visually shows the direction that you are heading, depth and so on.

The webinterface allows you to edit the config file and to update vanchor by uploading a ZIP so that you don't have to take your laptop with you to do changes.

<img src="./docs/webinterface.png" width="30%">

### [Function] Vanchor

**V**irtual **Anchor** holds the boat in the current coordinates.
It uses a PID regulator to controll trolling motor speed in case of current or wind.

### [Function] Lock Heading

Lock heading can lock heading with and without GPS.

If GPS is available it will take current heading and set a destination point 1000km further away.
After that it starts to generate NMEA APB sentences with Cross-Track-Error that it follows.

This will counteract drifting to the sides due to wind or current.

If GPS is not available it will just take your current heading and try to keep that.

### [Function] Autopilot

Autopilot has 2 modes:

1. APB from NMEA (ie. plotter)
2. APB from GPX files

APB from NMEA takes the coordinates of a GPX-file (with \<WP\> tags) and generates an NMEA APB-sentence with cross-track-error(XTE) / direction that it follows.

XTE magnitude is added to the feedback of the PID-controller.

## Hardware

While it's easily modifiable I used the following when it comes to hardware (NOT affiliate links):

- Raspberry Pi 4
- 6-24V step-down module /w USB (https://www.amazon.se/dp/B09DPJXNTP)
- Arduino Nano (https://www.amazon.se/dp/B01MS7DUEM)
  - GY-511 e-compass (https://www.amazon.se/dp/B07XXG8HNJ)
  - 60A DC Motor Controller (https://www.amazon.se/gp/product/B075FTL53W/)
  - L298N (for stepper) (https://www.amazon.se/gp/product/B077NY9RY6)
  - Stepper Motor (https://www.amazon.se/gp/product/B072LVXVKW)
  - A3144E Hall sensor (https://www.amazon.se/gp/product/B01M2WASFL)
  - 5mm neodynium magnet (for hall sensor)

### Gearbox

The gearbox is 3D-printed in PLA and seems to hold up. [You can find the STL's in 3d/gearbox ](./3d/gearbox).

<img src="./3d/Exploded.png" width="70%">

An exploded drawing is available [here.](./docs/drawing.pdf).

You mount an "hang-in"-style mount on the trolling motor that locks into place in the gearbox when the trolling motor is lowered.

#### What hardware you need:

| Pcs | Part                                                                     |
| --- | ------------------------------------------------------------------------ |
| 4   | 608ZZ bearing (Or similar bearing)                                       |
| 4   | 30mm M5 screws/bolts with a low profile head                             |
| 1   | 20mm bolt                                                                |
| 1   | A3144E sensor for calibrating                                            |
| 1   | ~5mm neodynium magnet </br>A3144E only reacts to 1 of the magnets poles. |
| 3   | ~10-20mm M3 screws                                                       |
| 4   | ~4-5mm self drilling screws                                              |
| -   | TP-cable for connecting the stepper and hall sensor                      |
| -   | CA-glue                                                                  |
| -   | Grease suitable for PLA                                                  |
| 1   | trolling motor with a 1" shaft                                           |

#### The parts you need to print

| Pcs | Part                                                                     | Comment                                                                                                                                                                       |
| --- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | [Trolling_Motor_Holder.stl](./3d/gearbox/TrollingMotorHolder-25.4mm.stl) | For 25.4mm/1in shaft diameter of the trolling motor. You might need to customize to make it fit your engine.</br> Though this size seems to be the most common one I've seen. |
| 4   | [BearingSpacer.stl](./3d/gearbox/BearingSpacer.stl)                      |                                                                                                                                                                               |
| 1   | [BoxHolder.stl](./3d/gearbox/BoxHolder.stl)                              |                                                                                                                                                                               |
| 1   | [CaseBottom.stl](./3d/gearbox/CaseBottom.stl)                            |                                                                                                                                                                               |
| 1   | [CaseTop.stl](./3d/gearbox/CaseTop.stl)                                  |                                                                                                                                                                               |
| 1   | [Gear1_12T.stl](./3d/gearbox/Gear1_12T.stl)                              |                                                                                                                                                                               |
| 2   | [Gear1_2_6T.stl](./3d/gearbox/Gear1_2_6T.stl)                            |                                                                                                                                                                               |
| 1   | [Gear2_18T.stl](./3d/gearbox/Gear2_18T.stl)                              |                                                                                                                                                                               |
| 1   | [Gear3_36T_Shaft.stl](./3d/gearbox/Gear3_36T_Shaft.stl)                  |                                                                                                                                                                               |
| 1   | [Gear3_36T.stl](./3d/gearbox/Gear3_36T.stl)                              |                                                                                                                                                                               |
| 1   | [JunctionBox.stl](./3d/gearbox/JunctionBox.stl)                          |                                                                                                                                                                               |
| 1   | [JunctionBoxLid.stl](./3d/gearbox/JunctionBoxLid.stl)                    |                                                                                                                                                                               |
| 1   | [StepperCover.stl](./3d/gearbox/StepperCover.stl)                        |                                                                                                                                                                               |
| 1   | [StepperGear.stl](./3d/gearbox/StepperGear.stl)                          |                                                                                                                                                                               |
