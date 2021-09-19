import yaml
import re
import os
from functools import reduce
from datetime import datetime
from shutil import copyfile


class Config:
    def __init__(self, main, config_file):
        self.logger = main.logging.getLogger(self.__class__.__name__)

        self.main = main

        self.emitter = main.event.emitter

        self.data = yaml.load(open(config_file, "r").read(), yaml.CLoader)
        self.config_file = config_file

        # Eventhandlers
        @self.emitter.on("config.set")
        def config_set(self, arg):
            self.logger.debug(f"Received config.set event with arg {arg}")
            main.config.set(arg[0], arg[1])

        @self.emitter.on("config.set")
        def set_config_value(arg):
            path = arg[0]
            value = arg[1]
            self.logger.info(f"Setting {path} to {value}")
            self.set(path, value)

        self.emitter.on("config.reload", self.reload())

    def get(self, path=None, default=None):
        self.logger.debug("Fetching config value of: {}".format(path))
        if path == None:
            return self.data
        else:
            dict_path = path.split("/")[0:-1]
            value_name = path.split("/")[-1]
            try:
                return reduce(dict.get, dict_path, self.data)[value_name]
            except KeyError:
                if default != None:
                    return None
                else:
                    self.logger.error(
                        "{} does not exist in config and default was not set"
                    )

    def set(self, path, value):
        dict_path = path.split("/")[0:-1]
        value_name = path.split("/")[-1]

        if isinstance(value, str):
            if value.lower() == "true":
                value = True
            if value.lower() == "false":
                value = False

        self.logger.info(f"Setting config {path} to value")

        if isinstance(reduce(dict.get, dict_path, self.config)[value_name], int):
            value = int(value)

        if re.match("^\d+$", value_name) != None:
            i = int(value_name)
            value_name = path.split("/")[-2]
            reduce(dict.get, dict_path, self.config)[value_name] = value
        else:
            reduce(dict.get, dict_path, self.config)[value_name] = value

    def backup(self):
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        backup_name = f"config-{date_str}.yml.bak"

        self.logger.info(f"Backing up config to {backup_name}")

        copyfile("config.yml", backup_name)

    def save(self):
        self.logger.info(f"Saving config")
        self.backup()

        self.logger.info("Saving config file to config.yml")
        with open(r"config.yml", "w") as config_file:
            documents = yaml.dump(self.data, config_file)

    def reload(self):
        self.logger.info("Reloading config from config.yml")
        self.data = yaml.load(open(self.config_file, "r").read(), yaml.CLoader)

        self.main.event.emit("config.reloaded", "*")

    def get_backups(self):
        files = os.listdir()
        r = re.compile("config.*yml.bak")
        return list(filter(r.match, os.listdir()))

    def restore_backup(self, file, reload=True):
        if file in self.get_backups():
            self.backup()
            copyfile(file, "config.yml")

            if reload:
                self.reload()
        else:
            self.logger.error("Backup does not exist")
