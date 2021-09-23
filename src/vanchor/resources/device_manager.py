import importlib
from re import sub
from ..devices import *


class DeviceManager:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main

        self.load_devices()

        gps_type = self.main.config.get("GPS/Device/Type")
        if gps_type != False:
            # gps_device = self.import_gps_device(gps_type)

            device_import = importlib.import_module(
                ".devices.gps_devices.{}".format(gps_type), package="vanchor"
            )

            self.gps = device_import.Device(main)
            print(device_import)

    def import_gps_device(self, name: str):
        components = "vanchor.devices.gps_devices.{}".format(name).split(".")
        mod = __import__(components[0])
        for comp in components[1:]:
            mod = getattr(mod, comp)
        return mod

    def load_devices(self):
        for f in self.main.config.get("Devices/Enabled"):
            self.logger.info(f"Loading device {f}")
            __class = self.import_class("vanchor.devices.{}".format(f))
            name = re.sub("([^^])([A-Z])", r"\g<1>_\g<2>", f).lower()
            locals()[name] = __class(self.main)

    def import_class(self, name):
        components = name.split(".")
        mod = __import__(components[0])
        for comp in components[1:]:
            mod = getattr(mod, comp)
        return mod
