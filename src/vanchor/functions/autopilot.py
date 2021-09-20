import simple_pid
from xml.dom import minidom
from os import listdir
from pynmea2 import APB
from pynmea2 import parse as pynmea_parse


class AutoPilot:
    name = "AutoPilot"

    def __init__(self, main, emitter):
        self.main = main
        self.emitter = emitter

        self.logger = main.logging.getLogger("Function:" + self.__class__.__name__)

        self.logger.info("Initializing function".format(self.name))

        self.emitter.on("function.{}.enable".format(self.name.lower()), self.enable)
        self.emitter.on("function.{}.disable".format(self.name.lower()), self.disable)
        self.emitter.on("function.status", self.auto_off)

        self.emitter.on("nmea.reading.apb", self.autopilot_handler)
        self.emitter.on("nmea.reading.rmc", self.route_handler)
        self.emitter.on("routes.new", self.save_route)
        self.emitter.on("routes.update", self.update_routes)
        self.emitter.on("autopilot.startroute", self.init_navigation)
        self.emitter.on("autopilot.waypoint.arrived", self.next_waypoint)
        self.emitter.emit("status.set", ["Functions/AutoPilot/LoadedRoute", {}])

        self.emitter.emit("steering.autosteer.register", "Functions/AutoPilot/Enabled")

        self.update_routes(None)

    def load_route(self, route: str, start: bool = False):
        route_list = {}
        n = 0
        for r in self.parse_gpx(route):
            route_list[f"wp{n}"] = r
            n += 1

        self.main.data.set("Functions/AutoPilot/LoadedRoute", route_list)
        self.main.data.set("Functions/AutoPilot/ActivePath", 0)
        if start:
            self.start_navigation()

    def init_navigation(self, arg):
        self.load_route(route=arg[0], start=arg[1])
        self.main.data.set("Functions/AutoPilot/Status", "LoadedRoute")
        self.set_waypoint(0)

    def start_navigation(self):
        self.main.data.set("Functions/AutoPilot/NavigationActive", True)
        self.main.data.set("Functions/AutoPilot/ActivePath", 0)
        self.main.data.set("Functions/AutoPilot/Status", "Navigating")

    def next_waypoint(self, arg=None):

        total_paths = (
            len(self.main.data.get("Functions/AutoPilot/LoadedRoute").keys()) - 1
        )

        current_coordinates = self.main.data.get("Navigation/Coordinates")

        active_path = self.main.data.get("Functions/AutoPilot/ActivePath")
        if active_path == None:
            self.start_navigation()
            return

        next_waypoint = self.main.data.get("Functions/AutoPilot/ActivePath") + 1

        if next_waypoint == total_paths:
            self.stop_navigation()
            self.logger.info("No more routes left, ending APB packet generation")
            self.main.data.set("Functions/AutoPilot/ActivePath", None)
            self.main.data.set("Functions/AutoPilot/Status", "Arrived")

            self.stop_navigation()
            return

        self.set_waypoint(next_waypoint)

    def set_waypoint(self, ix):
        route = self.main.data.get("Functions/AutoPilot/LoadedRoute")
        try:
            x = route["wp{}".format(ix)]
        except Exception as e:
            self.logger.error(
                "Error when setting next path index to {}/{}".format(
                    active, len(route) - 1
                ),
                e,
            )
            self.stop_navigation()
            return
        if ix == 0:
            last_pos = self.main.data.get("Navigation/Coordinates")
        else:
            last_pos = route["wp{}".format(ix - 1)]
        self.main.data.set("Functions/AutoPilot/WaypointStartCoordinates", last_pos)
        self.main.data.set("Functions/AutoPilot/ActivePath", ix)

    def stop_navigation(self):
        self.main.data.set("Functions/AutoPilot/NavigationActive", False)
        self.main.data.set("Functions/AutoPilot/Status", "Stopped")

    def route_handler(self, arg):

        active = self.main.data.get("Functions/AutoPilot/NavigationActive", False)
        index = self.main.data.get("Functions/AutoPilot/ActivePath", "EMPTY")

        if active == False:
            return
        if index == "EMPTY":
            self.logger.info(
                "APB generation is active but no path was loaded from route"
            )
            return

        arrival_radius = self.main.data.get("Functions/AutoPilot/ArrivalRadius", 5)

        rmc = pynmea_parse(arg[1])
        curr = [rmc.latitude, rmc.longitude]

        if index == 0:
            start = curr
        else:
            start = self.main.data.get("Functions/AutoPilot/WaypointStartCoordinates")

        route = self.main.data.get("Functions/AutoPilot/LoadedRoute")
        waypoint = route["wp{}".format(index)]
        waypoint_name = waypoint[0]
        self.logger.debug(
            "Generating APB package for waypoint {}".format(waypoint_name)
        )

        dest = route["wp{}".format(index)][1]
        curr_to_dest = self.main.tools.converter.get_bearing(curr, dest)
        start_to_dest = self.main.tools.converter.get_bearing(start, dest)

        if curr_to_dest["Distance"] <= arrival_radius:
            self.logger.info(
                "Arrived at waypoint '{}'. Selecting next waypoint (if it exists)".format(
                    waypoint_name
                )
            )
            self.main.emitter.emit("autopilot.waypoint.arrived", waypoint_name)
            return

        self.logger.debug(
            "Calculating distance and bearing. Start:{} Dest:{} Curr:{}".format(
                start, dest, curr
            )
        )
        xte = self.main.tools.geo.getCrossTrackDistance(start, dest, curr)

        self.logger.debug("StartToDest: {}".format(start_to_dest))
        self.logger.debug("CurrToDest: {}".format(curr_to_dest))
        if xte < 0:
            direction = "R"
        else:
            direction = "L"

        self.logger.debug("XTE is: {}".format(xte))

        apb_data = (
            "A",
            "A",
            str(round(abs(xte), 3)),
            direction,
            "M",
            "T",
            "T",
            str(start_to_dest["ForwardAzimuth"] % 360),
            "M",
            str(waypoint_name),
            str(curr_to_dest["ForwardAzimuth"] % 360),
            "M",
            direction,
            "M",
        )

        apb = APB("VA", "APB", apb_data).render()
        self.logger.debug("Sending APB: {}".format(apb))
        self.emitter.emit("nmea.reading.apb", apb)

        if curr_to_dest["Distance"] < self.main.config.get(
            "AutoPilot/ArrivalCircleRadius", 5
        ):
            self.emitter.emit("autopilot.waypoint.arrived", None)

    def parse_gpx(self, route):
        gpx_string = open("routes/{}".format(route)).read()
        gpx = minidom.parseString(gpx_string)
        points = []
        for p in gpx.getElementsByTagName("wpt"):
            points.append(
                [
                    p.TEXT_NODE,
                    [
                        float(p.attributes["lat"].value),
                        float(p.attributes["lon"].value),
                    ],
                ]
            )
        return points

    def save_route(self, arg):
        self.logger.debug(arg)
        name = arg["Name"]
        route = arg["Route"].replace("\n\n", "\n")
        try:
            self.parse_gpx(route)
        except Exception as e:
            self.logger.error("Failed to parse GPX - skipping")
            return

        self.logger.info("Saving new route {}".format(name))
        f = open(f"routes/{name}", "w")
        f.write(route)
        f.close()

    def update_routes(self, arg):

        routes = listdir("routes/")
        self.logger.info("Updating routes with {}".format(routes))
        self.main.emitter.emit(
            "status.set",
            ["Functions/AutoPilot/Routes", routes],
        )

    def create_pid(self):
        self.pid = simple_pid.PID(
            Kp=self.main.config.get("AutoPilot/PidKp"),
            Ki=self.main.config.get("AutoPilot/PidKi"),
            Kd=self.main.config.get("AutoPilot/PidKd"),
            setpoint=0,
            output_limits=[
                self.main.config.get("AutoPilot/PidOffsetLimitMin"),
                self.main.config.get("AutoPilot/PidOffsetLimitMax"),
            ],
        )

    def send_status(self, status):
        self.emitter.emit(
            "status.set", ["Functions/{}/Enabled".format(self.name), status]
        )
        self.emitter.emit("function.status", {"Name": self.name, "Enabled": status})

    def enable(self, arg):
        self.logger.info("Enabling {}".format(self.name))
        self.send_status(True)

    def disable(self, arg):
        self.logger.info("Disabling {}".format(self.name))
        self.send_status(False)

    def auto_off(self, arg):
        if arg["Name"] != self.name and arg["Enabled"] == True:
            self.logger.info(
                "Function {} turned on! Turning off {}".format(arg["Name"], self.name)
            )
            self.disable([])

    def autopilot_handler(self, arg):

        if self.main.data.get("Functions/AutoPilot/Enabled"):
            self.logger.info("Received APB package: {}".format(arg[0]))

            heading = self.data.get("Navigation/Compass/Heading")

            if arg.dir_steer == "L":
                m = -1
            else:
                m = 1

            active = self.main.data.get("Functions/AutoPilot/NavigationActive", False)
            index = self.main.data.get("Functions/AutoPilot/ActivePath", "EMPTY")
            if active == False or index == "EMPTY":
                arg[0].cross_track_err_mag

            else:
                current = self.main.data.get("Navigation/Coordinates")

                route = self.main.data.get("Functions/AutoPilot/LoadedRoute")
                destination = route["wp{}".format(index)]

                dest_info = self.main.tools.converter.get_bearing(current, destination)
                heading = dest_info["ForwardAzimuth"]

            correction = arg[0].cross_track_err_mag * m

            course = heading + correction

            self.logger.info("Setting new course to {} degrees".format(course))
            self.emitter.emit("status.set", ["Navigation/Heading", round(course, 3)])
        else:
            self.logger.info(
                "Received APB package: {} but skipped due to that AutoPilot is not enabled".format(
                    arg
                )
            )
