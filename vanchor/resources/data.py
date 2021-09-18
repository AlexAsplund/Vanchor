from functools import reduce


class DataNode:
    data = {}

    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.emitter = main.event.emitter

        # EventHandlers
        self.logger.debug("Registering status.set")
        self.emitter.on("status.set", self.status_set)

        self.data = {
            "Navigation": {
                "Compass": {"Heading": 0},
                "Coordinates": [0, 0],
                "Speed": 0,
                "IsRelative": False,
            },
            "Steering": {
                "RelativeSteering": True,
                "PidSetpoint": 0,
                "PidHeading": 0,
                "HoldHeading": 0,
            },
            "Stepper": {"Step": 0, "CalibrationStart": 0, "CalibrationEnd": 0},
            "Motor": {"Speed": 0},
            "Functions": {
                "Vanchor": {
                    "Enabled": False,
                    "DistanceFromTarget": 0,
                    "TargetBearing": 0,
                },
                "HoldHeading": {"Enabled": False, "Heading": 0},
                "AutoPilot": {
                    "Enabled": False,
                    "Routes": [],
                },
            },
            "Devices": {"Controller": {}},
        }

    def status_set(self, arg):
        self.logger.debug(f"Received status.set event with arg {arg}")
        self.main.data.set(arg[0], arg[1])
        event = "status.set." + arg[0].lower().replace("/", ".")

        self.logger.debug("Sending event {}".format(event))

        self.emitter.emit(event, arg)

    def get(self, path=None, default=None):
        try:
            if path == None:
                return self.data
            else:
                dict_path = path.split("/")[0:-1]
                value_name = path.split("/")[-1]

                return reduce(dict.get, dict_path, self.data)[value_name]
        except KeyError as e:
            if default != None:
                return default
            else:
                self.logger.error("No key found in data, try using default", e)

    def set(self, path, value):
        dict_path = path.split("/")[0:-1]
        value_name = path.split("/")[-1]

        if isinstance(value, str):
            if value.lower() == "true":
                value = True
            if value.lower() == "false":
                value = False

        self.logger.debug(f"Setting status {path} to '{value}'")
        try:
            if isinstance(reduce(dict.get, dict_path, self.data)[value_name], int):
                value = int(value)
        except:
            None
        reduce(dict.get, dict_path, self.data)[value_name] = value
