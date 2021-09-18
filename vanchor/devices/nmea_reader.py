import pynmea2
import serial
from time import sleep


class NmeaReader:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main

        self.main.event.emitter.on("nmea.parse", self.parse_nmea)

        self.main.event.emitter.on("nmea.reading.rmc", self.add_coordinates)

        main.work_manager.start_worker(self.input_listener, **{"timer": 10})

    def parse_nmea(self, message):
        try:
            nmea_packet = pynmea2.parse(message, check=False)
            packet_type = nmea_packet.__class__.__name__.lower()
            self.logger.debug(
                "Sending nmea packet: {}, sending nmea.reading.{} event".format(
                    message, packet_type
                )
            )
            self.main.event.emitter.emit(
                "nmea.reading.{}".format(packet_type),
                [nmea_packet, message],
            )

        except pynmea2.ParseError as e:
            self.logger.error("Error parsing NMEA string: {}".format(message), e)

    def input_listener(self, main):
        try:
            if self.main.debug != True:
                if self.serial.in_waiting > 0:
                    reading = self.serial.readline().decode()
                    self.main.event.emitter.emit("nmea.parse", reading)
            elif self.main.config.get("Serial/Controller/SimulateNMEA"):
                self.logger.info("DEBUG activated - NMEA test mode")
                for l in open(
                    self.main.config.get("Serial/Controller/NmeaTestFile"), "r"
                ).readlines():
                    l = l.replace("\n", "")
                    self.logger.debug("Emitting {}".format(l))
                    self.parse_nmea(l)
                    sleep(1)

        except Exception as e:
            self.logger.error("Error reading controller serial input", e)

    def add_coordinates(self, arg):
        nmea_packet = arg[0]
        self.main.emitter.emit(
            "status.set.navigation.coordinates",
            ["Navigation/Coordinates", [nmea_packet.latitude, nmea_packet.longitude]],
        )
        self.logger.debug(
            "Adding Coordinates from NMEA {}, {}".format(
                nmea_packet.latitude, nmea_packet.longitude
            )
        )
