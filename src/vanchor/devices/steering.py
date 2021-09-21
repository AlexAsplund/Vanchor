from ..devices import Stepper
import simple_pid
from math import pi


class Steering:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.emitter = main.event.emitter
        self.stepper = Stepper(main)

        self.pid = simple_pid.PID(
            Kp=main.config.get("Steering/PidKp"),
            Ki=main.config.get("Steering/PidKi"),
            Kd=main.config.get("Steering/PidKd"),
            output_limits=[
                main.config.get("Steering/PidOffsetLimitMin"),
                main.config.get("Steering/PidOffsetLimitMax"),
            ],
            setpoint=0,
            auto_mode=True,
        )
        self.emitter.emit("status.set", ["Navigation/Heading", 0])

        self.main.data.set("Steering/RelativeSteering", True)
        self.main.data.set("Steering/PidEnabled", True)

        # Main
        self.main = main

        # Steering handlers
        self.handlers = []

        # Autosteer functions
        self.emitter.emit("status.set", ["Steering/AutoSteerFunctions", []])
        self.registered_functions = []
        self.emitter.on("steering.autosteer.register", self.register_autosteer_function)

        # EventHandlers
        @self.emitter.on("steering.set.position")
        def set_position(arg):
            self.logger.debug("Setting position to {}".format(arg))
            self.set_position(arg)

        @self.emitter.on("steering.set.heading")
        def set_heading(arg):
            self.logger.debug("Setting heading to {}".format(arg))
            self.set_heading(arg)

        self.emitter.on("status.set.navigation.heading", self.set_pid_setpoint)
        self.emitter.on("config.reload.steering", self.reload)
        self.emitter.on("steering.handler.register", self.register_handler)

        self.emitter.on("status.set.navigation.compass.heading", self.pid_updater)

    def set_position(self, arg):
        self.emitter.emit("steering.stepper.set_pos", arg)
        self.emitter.emit("status.set", ["Navigation/IsRelative", True])

    def set_heading(self, arg):
        self.logger.debug(
            f"Steering received steering.set.heading event with arg {arg}"
        )

        heading = arg

        azi = int(self.main.data.get("Navigation/Compass/Heading"))

        new_pos = heading - azi
        if new_pos < 0:
            new_pos = 360 + new_pos

        self.logger.debug(
            "Relative Heading set to {}. Heading: {} Azi:{}".format(
                new_pos, heading, azi
            )
        )

        self.emitter.emit("status.set", ["Navigation/IsRelative", False])
        self.emitter.emit("status.set", ["Navigation/Heading", arg])
        self.emitter.emit("steering.stepper.set_pos", new_pos)

    def pid_updater(self, arg):
        pid_enabled = self.main.data.get("Steering/PidEnabled")

        self.logger.debug("PID update received with val {}".format(arg))

        angle = self.main.tools.get_angle(
            arg[1], self.main.data.get("Steering/SetHeading", 0)
        )

        if angle[0] < angle[1]:
            self.pid_value = self.pid(angle[0])
        else:
            self.pid_value = self.pid(-angle[1])

        heading = self.main.config.get("Navigation/Compass/Heading", 0) + self.pid_value

        self.logger.debug(
            "PID Value is:{} CorrectedHeading:{}".format(self.pid_value, heading)
        )

        if self.autosteer_enabled() == True:
            self.emitter.emit("steering.set.heading", heading)

        self.logger.debug(
            "Got pid_value:{} from {} degrees pidheading:{}".format(
                self.pid_value, arg, heading
            )
        )
        self.emitter.emit("status.set", ["Steering/PidHeading", heading])

    def set_pid_setpoint(self, arg):
        self.logger.debug("Got arg: {}".format(arg))
        if self.main.data.get("Steering/SetHeading") != arg[1]:
            self.emitter.emit("status.set", ["Steering/SetHeading", arg[1]])
            self.pid.reset()

    def reload(self, arg):
        self.pid.Kp = self.main.config.get("Steering/PidKp")
        self.pid.Ki = self.main.config.get("Steering/PidKi")
        self.pid.Kd = self.main.config.get("Steering/PidKd")

    def register_handler(self, handler):
        self.logger.info("Registering handler: {}".format(handler.__name__))
        self.handlers.append(handler)

    def autosteer_enabled(self):
        status = False
        for f in self.main.data.get("Steering/AutoSteerFunctions"):
            if self.main.data.get(f) == True:
                status = True

        self.emitter.emit("status.set", ["Steering/AutoSteerEnabled", status])

        return status

    def register_autosteer_function(self, path):
        self.logger.debug("Registering autosteer lookup value: {}".format(path))

        self.registered_functions.append(path)

        self.emitter.emit(
            "status.set", ["Steering/AutoSteerFunctions", self.registered_functions]
        )
