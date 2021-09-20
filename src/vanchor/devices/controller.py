import queue
import serial
import re
from time import time
from time import sleep


class Controller:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.emitter = main.event.emitter

        if self.main.debug != True:
            self.serial = serial.Serial(
                main.config.get("Serial/Controller/Device"),
                main.config.get("Serial/Controller/Baudrate"),
            )

        sleep(0.25)
        self.last_command = ""
        self.emitter.emit("controller.initialized")

        self.queue = queue.Queue()

        main.work_manager.start_worker(self.input_listener, **{"timer": 10})
        main.work_manager.start_worker(self.output_worker)
        main.work_manager.start_worker(
            self.command_stream_worker,
            **{"timer": main.config.get("Serial/Controller/OutputInterval")},
        )
        ###########################
        # EventHandlers
        ###########################
        self.emitter.on("controller.send", self.send)
        self.emitter.on("controller.reading", self.controller_status_reading)

    def send(self, command):
        self.logger.debug("Adding command {} to queue".format(command))
        self.queue.put(command)

    def output_worker(self, main):
        try:
            msg = self.queue.get()

            timestamp = msg[0]
            command = msg[1]
            if self.last_command != command:
                self.logger.debug("Got command: {}".format(msg))
            self.last_command = msg[1]
            sent = time()
            delay = sent - timestamp
            if self.main.debug != True:
                if self.serial.isOpen():
                    self.serial.write(bytes((command + "\n"), "utf-8"))
                    if self.last_command != command:
                        self.logger.debug(
                            f"Sending command: {command} | Received:{timestamp} Sent:{sent} Delay:{delay}"
                        )
                else:
                    self.logger.error("Controller serial was not available")
            else:
                if self.last_command != command:
                    self.logger.debug(
                        f"Sending command: {command} | Received:{timestamp} Sent:{sent} Delay:{delay}"
                    )

            self.queue.task_done()

            self.emitter.emit("status.set", ["Devices/Controller/Delay", delay])
        except Exception as e:
            self.logger.error(
                "Failed to send command to Controller serial device due to: {}".format(
                    e
                )
            )

    def controller_status_reading(self, reading):
        values = re.findall("[^ ]+:[^ $]+", reading)
        d = {}
        for x in re.findall("[^ ]+:[^ $]+", values):
            d[x.split(":")[0]] = x.split(":")[1]

        self.emitter.emit(
            "status.set.stepper.realposition", ["Stepper/RealPosition", d["SSP"]]
        )
        self.emitter.emit(
            "status.set.stepper.distancetogo", ["Stepper/DistanceToGo", d["SDTG"]]
        )
        self.emitter.emit("status.set.motor.realspeed", ["Motor/RealSpeed", d["MS"]])
        self.emitter.emit(
            "status.set.stepper.calibrationstart", ["Stepper/CalibrationStart", d["CB"]]
        )
        self.emitter.emit(
            "status.set.stepper.calibrationend", ["Stepper/CalibrationEnd", d["CE"]]
        )

    def input_listener(self, main):
        if self.main.debug != True:
            if self.serial.in_waiting > 0:
                try:
                    reading = self.serial.readline().decode()
                    if reading.startswith("STATUS "):
                        self.emitter.emit("controller.status.reading", reading)
                    else:
                        self.emitter.emit("controller.reading", reading)

                except Exception as e:
                    self.log.error("Error reading controller serial input", e)

    def command_stream_worker(self, main, **kwargs):

        step = self.main.data.get("Stepper/Step")
        speed = self.main.config.get("Stepper/Speed")
        acceleration = self.main.config.get("Stepper/Acceleration")
        motor_speed = self.main.data.get("Motor/Speed")
        motor_rev = 0

        msg = f"UPD {step} {speed} {acceleration} {motor_speed} {motor_rev}"

        if self.queue.qsize() < 10:
            self.queue.put([time(), msg])
        else:
            self.logger.error("Queue is full")
