import serial
from time import sleep
from ..gps import *


class Device(GPSBase):
    """
    It outputs NMEA by default
    """

    def init_gps(self):
        # Todo: Add UBX support for auto configuration of this
        self.blacklist = self.main.config.get("GPS/Device/Blacklist")
        self.open_serial()

    def open_serial(self):
        self.serial = serial.Serial(
            self.main.config.get("GPS/Device/Port"),
            self.main.config.get("GPS/Device/BaudRate"),
            timeout=None,
        )

    def handler(self, main):
        if self.serial.isOpen():
            try:
                if self.serial.in_waiting > 0:
                    reading = str(self.serial.readline().decode().replace("\r\n", ""))
                    if reading[0] == "$":
                        if reading[3:6] not in self.blacklist:
                            self.send(reading)
            except Exception as e:
                self.logger.error("Failed to read from serial", e)

        else:
            self.logger.warning(
                "Could not open serial port {} for device {} - Retrying in 5 seconds".format(
                    self.main.config.get("GPS/Device/Port"), self.__name__
                )
            )
            sleep(5)
            self.open_serial()
