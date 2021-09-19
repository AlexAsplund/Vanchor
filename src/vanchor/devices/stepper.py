from time import time


class Stepper:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main

        self.emitter = main.event.emitter

        self.max_step = (
            1.2 * self.main.config.get("Stepper/StepsPerRevolution")
        ) * self.main.config.get("Stepper/Ratio")
        self.min_step = (
            (1.2 * self.main.config.get("Stepper/StepsPerRevolution"))
            * self.main.config.get("Stepper/Ratio")
        ) * -1

        self.emitter.emit("status.set", ["Stepper/Step", 0])

        self.emitter.emit("status.set", ["Stepper/Position", 0])

        ###########################
        # EventHandlers
        ###########################

        self.emitter.on("steering.stepper.get_pos", self.get_pos)
        self.emitter.on("stepper.calibrate", self.calibrate)
        self.emitter.on("steering.stepper.set_step", self.set_step)
        self.emitter.on("steering.stepper.set_pos", self.set_pos)

    def calibrate(self, arg):
        try:
            midpoint = (
                self.main.data.get("Stepper/CalibrationStart")
                + self.main.data.get("Stepper/CalibrationEnd")
            ) / 2
            step = midpoint + self.main.config.get("Stepper/CalibrationOffset")
            self.logger.info("Sending calibration command")
            command = "CAL {}".format(step)
            self.emitter.emit("controller.send", [time(), command])
        except Exception as e:
            self.logger.error("Failed to calibrate", e)

    def get_pos(self):
        raw_pos = 360 * (
            self.main.data.get("Stepper/Step")
            / self.main.config.get("Stepper/StepsPerRevolution")
            / self.main.config.get("Stepper/Ratio")
        )

        if raw_pos < 0:
            pos = (((360 - abs(raw_pos)) / 360) % 1) * 360
        else:
            pos = (((360 + abs(raw_pos)) / 360) % 1) * 360

        self.emitter.emit("status.set", ["Stepper/Position", pos])
        return pos

    def set_step(self, step):
        if self.main.config.get("Stepper/Reversed"):
            step = step * -1
        self.emitter.emit("status.set", ["Stepper/Step", round(step, 0)])

    def set_pos(self, pos):
        self.logger.debug("Setting position to {} deg".format(pos))
        if pos < 0:
            pos = (((360 - abs(pos)) / 360) % 1) * 360
        else:
            pos = (((360 + abs(pos)) / 360) % 1) * 360

        self.logger.debug("Setting stepper pos to {} steps".format(pos))

        self.emitter.emit("status.set", ["Stepper/Position", pos])
        self.emitter.emit("status.set", ["Stepper/Step", self.get_best_path(pos)])

    def get_best_path(self, pos_to):
        steps_per_degree = (
            self.main.config.get("Stepper/StepsPerRevolution")
            * self.main.config.get("Stepper/Ratio")
        ) / 360

        curr_step = self.main.data.get("Stepper/Step")

        if curr_step == None:
            curr_step = 0

        pos_raw = 360 * (
            curr_step
            / self.main.config.get("Stepper/StepsPerRevolution")
            / self.main.config.get("Stepper/Ratio")
        )

        pos_from = pos_raw % 360

        if self.main.config.get("Stepper/Reversed"):
            pos_to = 360 - pos_to

        steps = []
        choices = []

        # CW
        steps.append(pos_to * steps_per_degree)
        # CCW
        steps.append(((360 - pos_to) * steps_per_degree) * -1)

        if abs(curr_step - steps[0]) < abs(curr_step - steps[1]):
            choices.append(steps[0])
            choices.append(steps[1])
        else:
            choices.append(steps[1])
            choices.append(steps[0])

        for c in choices:
            if c >= self.min_step and c <= self.max_step:
                self.logger.debug(
                    "PosTo:{} PosFrom:{} Step:{} Choices:{}".format(
                        pos_to, pos_from, c, choices
                    )
                )
                return c
