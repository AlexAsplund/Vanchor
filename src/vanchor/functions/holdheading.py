class HoldHeading:
    name = "HoldHeading"

    def __init__(self, main, emitter):
        self.main = main
        self.emitter = emitter
        self.logger = main.logging.getLogger("Function:" + self.__class__.__name__)

        self.logger.info("Initializing function {}".format(self.name))

        self.emitter.emit(
            "status.set", ["Functions/HoldHeading/XTE".format(self.name), False]
        )
        self.emitter.emit(
            "status.set", ["Functions/{}/GPSMode".format(self.name), True]
        )
        self.emitter.emit(
            "status.set",
            ["Functions/{}/StartCoordinates".format(self.name), [0, 0]],
        )

        # listen for events
        self.emitter.on("function.{}.enable".format(self.name.lower()), self.enable)
        self.emitter.on("function.{}.disable".format(self.name.lower()), self.disable)
        self.emitter.on("status.set.navigation.coordinates", self.set_xte)
        self.emitter.on("function.status", self.auto_off)

        self.emitter.emit(
            "steering.autosteer.register", "Functions/HoldHeading/Enabled"
        )

    def send_status(self, status):
        self.emitter.emit(
            "status.set", ["Functions/{}/Enabled".format(self.name), status]
        )
        self.emitter.emit("function.status", {"Name": self.name, "Enabled": status})

    def enable(self, arg):
        self.logger.info("Enabling {}".format(self.name))
        self.emitter.emit(
            "status.set.navigation.holdheading",
            [
                "Functions/HoldHeading/Heading",
                self.main.data.get("Navigation/Compass/Heading"),
            ],
        )
        heading = self.main.data.get("Navigation/Compass/Heading")

        self.emitter.emit(
            "status.set.navigation.heading", ["Navigation/Heading", heading]
        )

        coordinates = self.main.data.get("Navigation/Coordinates")
        if (
            coordinates != [0, 0]
            or self.main.config.get("HoldHeading/GPSMode") == False
        ):
            self.emitter.emit(
                "status.set",
                ["Functions/{}/StartCoordinates".format(self.name), coordinates],
            )
            self.emitter.emit(
                "status.set", ["Functions/{}/GPSMode".format(self.name), True]
            )
        else:
            self.emitter.emit(
                "status.set", ["Functions/{}/GPSMode".format(self.name), False]
            )

        self.send_status(True)

    def disable(self, arg):
        self.logger.info("Disabling {}".format(self.name))
        self.main.data.set("Functions/HoldHeading/StartCoordinates", [0, 0])
        self.send_status(False)

    def auto_off(self, arg):
        if arg["Name"] != self.name and arg["Enabled"] == True:
            self.logger.info(
                "Function {} turned on! Turning off {}".format(arg["Name"], self.name)
            )
            self.disable([])

    def set_xte(self, arg):
        if (
            self.main.data.get("Functions/HoldHeading/Enabled") == False
            or self.main.data.get("Functions/HoldHeading/GPSMode") == False
        ):
            return

        heading = self.main.data.get("Navigation/Compass/Heading")
        start = self.main.data.get("Functions/HoldHeading/StartCoordinates")

        if self.main.data.get("Functions/HoldHeading/StartCoordinates") == [0, 0]:
            self.main.data.set("Functions/HoldHeading/StartCoordinates", arg[1])
            start = arg[1]

        distant_coordinates = self.main.tools.geo.get_coordinates_from_heading(
            start, (10000 * 100), heading
        )
        self.logger.debug(
            "Start:{} Distant:{} Current:{}".format(start, distant_coordinates, arg[1])
        )
        xte = self.main.tools.geo.getCrossTrackDistance(
            start, distant_coordinates, arg[1]
        )

        self.logger.debug("HoldHeading XTE is {}".format(xte))

        self.main.data.set("Functions/HoldHeading/XTE", xte)
