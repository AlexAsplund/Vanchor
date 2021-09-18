from ..functions import *


class Functions:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.emitter = main.event.emitter

        self.functions = {}
        self.load_functions()

    def load_functions(self):
        for f in self.main.config.get("Functions/Enabled"):
            self.logger.info(f"Loading function {f}")
            __class = self.import_class("vanchor.functions.{}".format(f))
            self.functions[f] = __class(self.main, self.emitter)

    def import_class(self, name):
        components = name.split(".")
        mod = __import__(components[0])
        for comp in components[1:]:
            mod = getattr(mod, comp)
        return mod
