from datetime import datetime


class Metrics:
    def __init__(self, main):
        self.main = main
        self.emitter = main.event.emitter

        self.logger = main.logging.getLogger("Function:" + self.__class__.__name__)

        timestamp = datetime.now().isoformat().replace(":", "-")
        default_metrics_path = "metrics-{}.log".format(timestamp)
        self.logger.info("Logging metrics to {}".format(default_metrics_path))
        self.file = open(
            self.main.config.get("Logging/MetricsPath", default_metrics_path),
            "w",
        )
        self.status_dict = {}
        self.emitter.on("status.set", self.log)

    def log(self, arg):
        name, value = arg

        if (
            name in self.status_dict.keys() and self.status_dict[name] != value
        ) or name not in self.status_dict.keys():
            self.file.write(
                "{},{},{}\n".format(datetime.now().isoformat(), name, value)
            )
            self.status_dict[name] = value
