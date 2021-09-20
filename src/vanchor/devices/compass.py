from time import sleep
from time import time
from math import radians, cos, sin, asin, sqrt, pi, atan2, degrees
from pynmea2 import HDM

try:
    import board
    import adafruit_lsm303dlh_mag
    import adafruit_lsm303_accel
except:
    None


class Compass:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.logger.info("Loading mock compass")
        self.i2c = board.I2C()  # uses board.SCL and board.SDA
        self.accel = adafruit_lsm303_accel.LSM303_Accel(self.i2c)
        self.mag = adafruit_lsm303dlh_mag.LSM303DLH_Mag(self.i2c)
        self.emitter = self.main.event.emitter
        self.max_length = self.main.config.get("Compass/HeadingHistoryLength")
        self.max_diff = 5  # Max diff allowed from average
        self.ls = []
        self.interval = 0
        self.logger.info("Starting compass worker")
        self.main.work_manager.start_worker(
            self.heading_update_worker,
            **{"timer": self.main.config.get("Compass/UpdateInterval")},
        )

    def push(self, st):
        if len(self.ls) == self.max_length:
            self.ls.pop(0)
        self.ls.append(st)

    def avg(self):
        n = 0
        temp = []
        last = self.ls[0]

        for i in self.ls:
            angle = self.main.tools.get_angle(last, i)

            if angle[-1] < self.max_diff:
                if angle[0] > angle[1]:
                    v = angle[-1] * -1
                else:
                    v = angle[0]
                temp.append(radians(v))
                n += i
                last = i
            else:
                self.logger.debug("Diff was {} - skipping".format(angle[-1]))
        try:
            average_angle = (n / len(temp) % 360) + (sum(temp) / len(temp))
            return average_angle
        except Exception as e:
            self.logger.warning(
                "Failure occured when averaging the list {} with {}. The list is: {}".format(
                    temp, len(temp), self.ls
                ),
                e,
            )

            return round(sum(self.ls) / len(self.ls), 2)

    def get(self):
        return self.ls

    def vector_2_degrees(self, x, y):
        angle = degrees(atan2(y, x))
        if angle < 0:
            angle += 360
        return angle

    def get_heading(self):
        magnet_x, magnet_y, _ = self.mag.magnetic
        return self.vector_2_degrees(magnet_x, magnet_y)

    def get_compensated_heading(self):
        accRaw = self.accel.acceleration
        accXnorm = accRaw[0] / sqrt(
            accRaw[0] * accRaw[0] + accRaw[1] * accRaw[1] + accRaw[2] * accRaw[2]
        )
        accYnorm = accRaw[1] / sqrt(
            accRaw[0] * accRaw[0] + accRaw[1] * accRaw[1] + accRaw[2] * accRaw[2]
        )
        pitch = asin(accXnorm)
        roll = -asin(accYnorm / cos(pitch))
        mag_raw = self.get_heading()
        mag_x, mag_y, mag_z = self.mag.magnetic
        magXcomp = mag_x * cos(pitch) + mag_z * sin(pitch)
        magYcomp = mag_x * sin(roll) * sin(pitch) + mag_y * cos(roll)
        magYcomp = magYcomp - mag_z * sin(roll) * cos(pitch)
        heading = 180 * atan2(magYcomp, magXcomp) / pi
        if heading < 0:
            heading += 360
        return heading

    def heading(self):
        self.push(self.get_compensated_heading())
        return self.avg()

    def heading_update_worker(self, main):
        self.interval += 1

        if self.interval == self.main.config.get("Compass/SendInterval"):
            self.interval = 0
            heading = round(self.heading(), 3)
            nmea_sentence = HDM(
                talker="VA", sentence_type="HDM", data=[str(heading), "M"]
            )

            self.logger.info(
                "Sending compass heading nmea sentence as event: {}".format(
                    str(nmea_sentence)
                )
            )
            self.emitter.emit("nmea.reading.hdm", [nmea_sentence, str(nmea_sentence)])


class MockCompass:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.logger.info("Loading mock compass")
        self.logger.warning(
            "Loading mock compass since regular compass couldn't be loaded due to import errors"
        )
        self.main = main
        self.emitter = main.event.emitter
        self.max_length = self.main.config.get("Compass/HeadingHistoryLength")
        self.max_diff = 15  # Max diff allowed from average
        self.ls = []
        self.s = 3
        self.up = True

        self.interval = 0

        self.seq_count = 0
        self.seq_part = 0
        self.test_seq = [
            [-0.1, 30],
            [0.1, 30],
            [0, 1000],
            [0.05, 100],
            [-0.05, 100],
        ]
        self.logger.info("Starting compass worker")
        self.main.work_manager.start_worker(
            self.heading_update_worker,
            **{"timer": self.main.config.get("Compass/UpdateInterval")},
        )

    def push(self, st):
        if len(self.ls) == self.max_length:
            self.ls.pop(0)
        self.ls.append(st)

    def avg(self):

        n = 0

        temp = []

        last = self.ls[0]
        for i in self.ls:
            angle = self.main.tools.get_angle(last, i)

            if angle[-1] < self.max_diff:
                if angle[0] > angle[1]:
                    v = angle[-1] * -1
                else:
                    v = angle[0]
                temp.append(radians(v))
                n += i
                last = i
            else:
                self.logger.debug("Diff was {} - skipping".format(angle[-1]))
        try:
            average_angle = (n / len(temp) % 360) + (sum(temp) / len(temp))
            return average_angle
        except Exception as e:
            self.logger.warning(
                "Failure occured when averaging the list {} with {}. The list is: {}".format(
                    temp, len(temp), self.ls
                ),
                e,
            )

            return round(sum(self.ls) / len(self.ls), 2)

    def get(self):
        return self.ls

    def get_compensated_heading(self):

        if self.seq_count < 0:
            self.s -= 0.1
        else:
            self.s += 0.1
        self.seq_count += 1
        if self.seq_count == 10:
            self.seq_count = -10

        return self.s % 360

    def heading(self):
        self.push(self.get_compensated_heading())
        return self.avg()

    def heading_update_worker(self, main):
        self.interval += 1

        if self.interval == self.main.config.get("Compass/SendInterval"):
            self.interval = 0
            self.logger.debug("Sending compass heading nmea sentence as event")
            heading = round(self.heading(), 3)
            nmea_sentence = HDM(
                talker="VA", sentence_type="HDM", data=[str(heading), "M"]
            )
            self.emitter.emit("nmea.parse", str(nmea_sentence))
