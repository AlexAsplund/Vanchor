import importlib
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

        gps_type = self.main.config.get("GPS/Device/Type")
        if gps_type != False:
            # gps_device = self.import_gps_device(gps_type)

            device_import = importlib.import_module(
                ".devices.gps_devices.{}".format(gps_type), package="vanchor"
            )

            self.gps = device_import.Device(main)
            print(device_import)

        self.nmea_net = NmeaNet(self.main)

    def import_gps_device(self, name: str):
        components = "vanchor.devices.gps_devices.{}".format(name).split(".")
        mod = __import__(components[0])
        for comp in components[1:]:
            mod = getattr(mod, comp)
        return mod
