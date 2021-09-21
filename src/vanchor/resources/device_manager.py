from ..devices import *


class DeviceManager:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.nmea_reader = Nmea(self.main)
        if self.main.debug == False:
            self.compass = Compass(self.main)
        else:
            self.compass = MockCompass(self.main)

        self.steering = Steering(self.main)
        self.controller = Controller(self.main)

        self.nmea_net = NmeaNet(self.main)
