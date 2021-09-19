from ..devices import *


class DeviceManager:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.nmea_reader = Nmea(self.main)
        try:
            self.compass = Compass(self.main)
        except Exception as e:
            self.logger.warning(
                "Failed to load regular compass due to an import error. Loading MockCompass instead.",
                e,
            )
            self.compass = MockCompass(self.main)
        self.steering = Steering(self.main)
        self.controller = Controller(self.main)

        self.nmea_net = NmeaNet(self.main)
