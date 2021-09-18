# Config

## AutoPilot

### PidKp

AutoPilot PID P-value

### PidKi

AutoPilot PID I-value

### PidKd

AutoPilot PID D-value

### PidOffsetLimitMax

AutoPilot PID maximum offset from target

### PidOffsetLimitMin

AutoPilot PID minimum offset from target

---

## Compass

### UpdateInterval

How often to poll the compass (in milliseconds)

### HeadingHistoryLength

How many readings to average compass-readings on, to avoid jitter.

---

## Flask

### Port

Web-server port

### Debug

Set debug-mode on flask server on or off

---

## Logging

### LogFile

Path to logfile

### Format

Log format (python logging module)

### LogToFile

Bool - Can destroy SD cards

### Level

Level to log. Available logs:

- DEBUG
- INFO
- WARN
- ERROR

---

## Motor

### RampTime

NOT IMPLEMENTED.

Ramptime of trolling motor

### RampDelay

NOT IMPLEMENTED.

Ramptime of trolling motor

---

## NmeaNet

### Port

Port for Nmea service to use

---

## Serial.Controller

### Device

Path to controller serial

### Baudrate

Baudrate of controller serial

### InputInterval

Interval to read data

### OutputInterval

Interval to send data

### Timeout

Device timeout in S

## Serial.NmeaInput

### Device

Path do serial device for NmeaInput

### Baudrate

Baudrate of serial device

### Timeout

## Timeout of serial device

## Steering

### PidKp

Steering PID P-value

### PidKi

Steering PID I-value

### PidKd

Steering PID D-value

### PidOffsetLimitMax

Steering PID maximum offset from target

### PidOffsetLimitMin

Steering PID minimum offset from target

### Slack

NOT IMPLEMENTED

Allowed slack before a correction is made

### ChokeMotor

NOT IMPLEMENTED

Slow down motor before turning

### CalibrationOffset

NOT IMPLEMENTED

Offset from calibration-point in steps

### CorrectionInterval

Time between corrections

---

## Stepper

### Ratio

Gear ratio.
Ie. 36 implies that 36 rotations has to be made on the stepper for one turn of the trolling motor

### Speed

Stepper speed

### Acceleration

Stepper acceleration

### StepsPerRevolution

Stepper steps per revolution

### Reversed

If true the step-value will be reversed.
Ie. 1800 steps will become 1800 steps.

---

## Functions

### Enabled

A list of the class names of enabled versions located in `vanchor.functions`.

---

## Vanchor

### PidKp

Vanchor PID P-value

### PidKi

Vanchor PID I-value

### PidKd

Vanchor PID D-value

### PidOffsetLimitMax

Vanchor PID maximum offset from target

### PidOffsetLimitMin

Vanchor PID minimum offset from target
