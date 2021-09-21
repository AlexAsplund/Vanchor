import yaml

from time import sleep
from time import time
from math import radians, cos, sin, asin, sqrt, pi, atan2, degrees
from pynmea2 import HDM
from requests import get
from geomag import geomag

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

        wmm_path = self.main.config.get("Compass/WmmFile", False)
        if wmm_path != False:
            self.geomag = geomag.GeoMag(wmm_filename=wmm_path)
        else:
            self.geomag = None

        self.emitter = self.main.event.emitter
        self.max_length = self.main.config.get("Compass/HeadingHistoryLength")
        self.max_diff = 3  # Max diff allowed from average
        self.ls = []
        self.interval = 0
        self.cal = None
        self.is_calibrating = False
        self.logger.info("Starting compass worker")

        # Fetch magnetic declination if it does not exist

        self.main.work_manager.start_worker(
            self.heading_update_worker,
            **{"timer": self.main.config.get("Compass/UpdateInterval")},
        )

        self.emitter.on("compass.calibrate", self.calibrate)

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

        heading + self.get_declination()

        if heading < 0:
            heading += 360
        return heading

    def heading(self):
        if self.is_calibrating == False:
            self.push(self.get_compensated_heading())
        else:
            self.logger.debug("Get heading skipped returning last value - Calibrating")

        return self.avg()

    def heading_update_worker(self, main):
        self.interval += 1

        if self.interval == self.main.config.get("Compass/SendInterval"):
            self.interval = 0
            self.logger.debug("Sending compass heading nmea sentence as event")
            heading = round(self.heading(), 3) + self.main.config.get(
                "Compass/Declination", 0
            )

            if heading < 0:
                heading = 360 + heading

            nmea_sentence = HDM(
                talker="VA", sentence_type="HDM", data=[str(heading), "M"]
            )
            self.emitter.emit("nmea.parse", str(nmea_sentence))

    def calibrate(self, duration=15):
        self.logger.info("Starting calibration")
        self.is_calibrating = True
        mag_min = (32767, 32767, 32767)
        mag_max = (-32768, -32768, -32768)
        acc_min = (32767, 32767, 32767)
        acc_max = (-32768, -32768, -32768)

        self.logger.info("Reading magnetometer")
        while time() < time() + duration:
            start = time()

            acc = self.accel.acceleration
            mag = self.mag.magnetic
            if self.cal != None:
                cal = self.adjust(acc, mag)
                mag_x, mag_y, mag_z = cal[0]
                acc_x, acc_y, acc_z = cal[1]
            else:
                mag_x, mag_y, mag_z = mag
                acc_x, acc_y, acc_z = acc

            mag_min = tuple(map(lambda x, y: min(x, y), mag_min, mag))
            mag_max = tuple(map(lambda x, y: max(x, y), mag_max, mag))

            self.logger.info(
                "Mag x,y,z:{},{},{} mag_min,mag_max={},{}".format(
                    mag_x, mag_y, mag_z, mag_min, mag_max
                )
            )
            sleep(0.05)

        self.logger.info("Reading accelerometer")
        while time() < time() + duration:
            acc = self.accel.acceleration
            mag = self.mag.magnetic
            mag_x, mag_y, mag_z = mag
            acc_x, acc_y, acc_z = acc

            acc_min = tuple(map(lambda x, y: min(x, y), acc_min, acc))
            acc_max = tuple(map(lambda x, y: max(x, y), acc_max, acc))

            self.logger.info(
                "Acc x,y,z:{},{},{} acc_min,acc_max={},{}".format(
                    acc_x, acc_y, acc_z, acc_min, acc_max
                )
            )
            sleep(0.05)

        self.logger.info("Calculating calibration values")
        mag_offset = tuple(map(lambda x1, x2: (x1 + x2) / 2.0, mag_min, mag_max))
        avg_mag_delta = tuple(map(lambda x1, x2: (x2 - x1) / 2.0, mag_min, mag_max))
        combined_avg_mag_delta = (
            avg_mag_delta[0] + avg_mag_delta[1] + avg_mag_delta[2]
        ) / 3.0
        scale_mag_x = combined_avg_mag_delta / avg_mag_delta[0]
        scale_mag_y = combined_avg_mag_delta / avg_mag_delta[1]
        scale_mag_z = combined_avg_mag_delta / avg_mag_delta[2]

        acc_offset = tuple(map(lambda x1, x2: (x1 + x2) / 2.0, acc_min, acc_max))
        avg_acc_delta = tuple(map(lambda x1, x2: (x2 - x1) / 2.0, acc_min, acc_max))
        combined_avg_acc_delta = (
            avg_acc_delta[0] + avg_acc_delta[1] + avg_acc_delta[2]
        ) / 3.0
        scale_acc_x = combined_avg_acc_delta / avg_acc_delta[0]
        scale_acc_y = combined_avg_acc_delta / avg_acc_delta[1]
        scale_acc_z = combined_avg_acc_delta / avg_acc_delta[2]

        calibration_dict = {}
        calibration_dict["mag_offset_x"] = mag_offset[0]
        calibration_dict["mag_offset_y"] = mag_offset[1]
        calibration_dict["mag_offset_z"] = mag_offset[2]
        calibration_dict["scale_mag_x"] = scale_mag_x
        calibration_dict["scale_mag_y"] = scale_mag_y
        calibration_dict["scale_mag_z"] = scale_mag_z
        calibration_dict["acc_offset_x"] = acc_offset_[0]
        calibration_dict["acc_offset_y"] = acc_offset_[1]
        calibration_dict["acc_offset_z"] = acc_offset_[2]
        calibration_dict["scale_acc_x"] = scale_acc_x
        calibration_dict["scale_acc_y"] = scale_acc_y
        calibration_dict["scale_acc_z"] = scale_acc_z

        self.logger.info("Saving calibration values to compass.yml")
        with open(r"compass.yml", "w") as config_file:
            documents = yaml.dump(calibration_dict, config_file)

        self.cal = calibration_dict
        self.is_calibrating = False

    def read_calibration(self):
        try:
            with open(r"compass.yml", "w") as config_file:
                self.cal = yaml.loads(config_file.read())
        except Exception as e:
            self.logger.warning("Couldn't load magnetometer calibration settings!", e)

    def adjust(self, acc, mag):
        mag_x, mag_y, mag_z = mag
        acc_x, acc_y, acc_z = acc

        m = []
        a = []
        m[0] = (mag_x - self.cal["mag_offset_x"]) * scale_mag_x
        m[1] = (mag_y - self.cal["mag_offset_y"]) * scale_mag_y
        m[2] = (mag_z - self.cal["mag_offset_z"]) * scale_mag_z
        a[0] = (acc_x - self.cal["acc_offset_x"]) * scale_acc_x
        a[1] = (acc_y - self.cal["acc_offset_y"]) * scale_acc_y
        a[2] = (acc_z - self.cal["acc_offset_z"]) * scale_acc_z

        return [m, a]

    def get_declination(self):
        c = self.main.data.get("Navigation/Coordinates")
        if (c[0] + c[1] == 0) or self.geomag == None:
            self.logger.debug(
                "Coordinates / GeoMag not available - fallback to Navigation/Declination in config"
            )
            return self.main.config.get("Navigation/Declination", 0)
        else:

            return self.geomag.GeoMag(c[0], c[1]).dec


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

        wmm_path = self.main.config.get("Compass/WmmFile", False)
        if wmm_path != False:
            self.geomag = geomag.GeoMag(wmm_filename=wmm_path)
        else:
            self.geomag = None

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

        dec = self.get_declination()
        out = self.s + dec

        return out % 360

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

    def get_declination(self):
        c = self.main.data.get("Navigation/Coordinates")
        if (c[0] + c[1] == 0) or self.geomag == None:
            self.logger.debug(
                "Coordinates / GeoMag not available - fallback to Navigation/Declination in config"
            )
            dec = self.main.config.get("Navigation/Compan/Declination", 0)
        else:
            dec = self.geomag.GeoMag(c[0], c[1]).dec

        self.main.data.set("Navigation/Compass/Declination", dec)
        return dec
