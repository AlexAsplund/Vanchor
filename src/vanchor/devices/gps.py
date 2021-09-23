from geomag import geomag
from time import time, sleep


class GPSBase:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.emitter = main.event.emitter

        wmm_path = self.main.config.get("Compass/WmmFile", False)

        if wmm_path != False:
            self.geomag = geomag.GeoMag(wmm_filename=wmm_path)
        else:
            self.geomag = None

        init = False
        while init == False:
            try:
                self.init_gps()
                init = True
            except Exception as e:
                self.logger.error("Failed to init GPS - retrying in 5 seconds", e)
                sleep(5)

        main.work_manager.start_worker(self.handler)

    def init_gps(self):
        None

    def send(self, message):
        self.emitter.emit("nmea.parse", message)

    def send_coordinates(self, lat, lon, talker="VA"):
        nmea_format = self.main.tools.converter.from_dd_to_dms([lat, lon])
        nmea_data = [
            str(time()),
            "V",
            nmea_format[0]["pos"],
            nmea_format[0]["dir"],
            nmea_format[1]["pos"],
            nmea_format[1]["dir"],
            "",
            "",
            "",
        ]

        nmea_obj = pynmea2.RMC(talker, "RMC", nmea_data)

        self.emitter.emit("nmea.parse", nmea_obj.render())

    def get_declination(self, c):
        if (c[0] + c[1] == 0) or self.geomag == None:
            return self.main.config.get("Navigation/Declination", 0)
        else:

            return self.geomag.GeoMag(c[0], c[1]).dec
