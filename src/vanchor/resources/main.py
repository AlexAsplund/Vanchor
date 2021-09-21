import logging
import os

from ..devices import *
from ..functions import *
from ..resources import *
from ..web import *

from pymitter import EventEmitter
from datetime import datetime


class Main:
    def __init__(self, emitter=None, debug=False, config_file="config.yml"):

        self.logging = logging
        if emitter == None:
            self.emitter = EventEmitter(wildcard=True)
        else:
            self.emitter = emitter
        self.debug = debug

        self.logger = self.logging.getLogger(self.__class__.__name__)

        self.config_file = config_file

        self.emitter.on("main.update.uploaded", self.update)

        try:
            self.version = open("version.txt", "r").read()
        except:
            self.version = "N/A"

    def run(self, config_file="config.yml"):

        self.event = EventManager(self, self.emitter)
        self.config = Config(self, config_file)

        self.tools = Tools(self)

        self.logging.basicConfig(
            format=self.config.get("Logging/Format"),
            level=self.logging.getLevelName(self.config.get("Logging/Level")),
            handlers=self.get_logging_handlers(),
        )

        self.work_manager = WorkerManager(self)

        self.data = DataNode(self)
        self.devices = DeviceManager(self)

        self.web = WebApplication(self)

        self.workers = Workers(self)

        self.functions = Functions(self)

    def get_logging_handlers(self):
        handlers = []
        root = self.logging.getLogger()
        formatter = self.logging.Formatter(self.config.get("Logging/Format"))
        if self.config.get("Logging/LogToFile"):
            file_handler = self.logging.FileHandler(self.config.get("Logging/LogFile"))
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

        return handlers

    def stop(self, code):
        exit(code)

    def update(self, arg):
        date = datetime.now().isoformat().replace(":", "-")
        try:
            os.system("rm -rf /tmp/vanchor_update")

        except:
            None

        os.system("mkdir -p backup/{}".format(date))
        os.system("mkdir /tmp/vanchor_update")
        os.system("unzip uploads/{} -d /tmp/vanchor_update".format(arg))
        os.system("cp -f config.yml /tmp/vanchor_update/config.yml")
        try:
            os.system("cp -f routes/* /tmp/vanchor_update/routes/")
        except:
            self.logger.warning("Error copying routes")

        try:
            os.system("mv ./* backup/{}".format(date))
        except Exception as e:
            self.logger.warning("Error when backing up config", e)
        os.system("cp -rf /tmp/vanchor_update/* .")
        os.system("rm -rf /tmp/vanchor_update")

        self.logger.info("Copying vanchor.service")
        os.system("cp scripts/vanchor.service /etc/systemd/system/vanchor.service")

        self.logger.info("Reloading systemctl daemon")
        os.system("sudo systemctl daemon-reload")

        self.logger.info("Enabling vanchor.service")
        os.system("sudo systemctl enable vanchor.service")

        self.logger.info("Restarting Vanchor")
        os.system("systemctl restart vanchor")
