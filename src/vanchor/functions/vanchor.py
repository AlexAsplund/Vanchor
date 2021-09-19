import simple_pid


class Vanchor:
    name = "Vanchor"

    def __init__(self, main, emitter):
        self.main = main
        self.main.event.emitter = emitter
        self.logger = main.logging.getLogger("Function:" + self.__class__.__name__)
        self.logger.info("Initializing function {}".format(self.name))

        self.main.event.emitter.on(
            "function.{}.enable".format(self.name.lower()), self.enable
        )
        self.main.event.emitter.on(
            "function.{}.disable".format(self.name.lower()), self.disable
        )
        self.main.event.emitter.on(
            "status.set.navigation.coordinates", self.vanchor_handler
        )
        self.main.event.emitter.on("function.status", self.auto_off)

        self.create_pid()

    def enable(self, arg):
        self.logger.info("Enabling vanchor")
        self.create_pid()
        self.pid.reset()

        self.send_status(True)

        self.main.event.emitter.emit(
            "function.status", {"Name": self.name, "Enabled": True}
        )

        self.main.event.emitter.emit(
            "status.set",
            [
                "Functions/Vanchor/Coordinates",
                self.main.data.get("Navigation/Coordinates"),
            ],
        )

    def send_status(self, status):
        self.logger.info("Enabling {}".format(self.name))
        self.main.event.emitter.emit(
            "status.set", ["Functions/{}/Enabled".format(self.name), status]
        )
        self.main.event.emitter.emit(
            "function.status", {"Name": self.name, "Enabled": status}
        )

    def disable(self, arg):
        self.logger.info("Disabling {}".format(self.name))
        self.send_status(False)

        self.main.event.emitter.emit(
            "status.set", ["Functions/Vanchor/Coordinates", [0, 0]]
        )

    def auto_off(self, arg):
        if arg["Name"] != self.name and arg["Enabled"] == True:
            self.logger.info(
                "Function {} turned on! Turning off {}".format(arg["Name"], self.name)
            )
            self.disable([])

    def create_pid(self):
        self.pid = simple_pid.PID(
            Kp=self.main.config.get("Vanchor/PidKp"),
            Ki=self.main.config.get("Vanchor/PidKi"),
            Kd=self.main.config.get("Vanchor/PidKd"),
            setpoint=self.main.config.get("Vanchor/Radius"),
            output_limits=[
                self.main.config.get("Vanchor/PidOffsetLimitMax") * -1,
                self.main.config.get("Vanchor/PidOffsetLimitMax"),
            ],
        )

    def vanchor_handler(self, arg):
        if self.main.data.get("Functions/Vanchor/Enabled") == True:
            current_coordinates = arg[1]
            self.logger.debug("Vanchor received coordinate event")
            try:
                set_coordinates = self.main.data.get("Functions/Vanchor/Coordinates")
            except:
                self.logger.warning(
                    "Failed to fetch coordinates, skipping vanchor_handler"
                )
                return

            if set_coordinates == [0, 0]:
                self.logger.debug("No coordinates set -exiting")
                self.main.event.emitter.emit(
                    "function.status", {"Name": self.name, "Enabled": False}
                )
                return
            self.logger.debug(
                "Get bearing from {}  to {}".format(
                    set_coordinates, current_coordinates
                )
            )
            target = self.main.tools.converter.get_bearing(
                current_coordinates, set_coordinates
            )

            self.logger.debug("Drift from target: {} meters".format(target["Distance"]))

            speed = abs(self.pid(target["Distance"]))
            heading = target["ForwardAzimuth"]

            self.main.event.emitter.emit(
                "status.set.motor.speed", ["Motor/Speed", speed]
            )
            self.main.event.emitter.emit(
                "status.set.navigation.heading", ["Navigation/Heading", heading]
            )

            self.logger.debug("Set speed: {}".format(speed))
            self.logger.debug("Set heading: {}".format(heading))
