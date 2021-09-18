class EventManager:
    def __init__(self, main, emitter):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.emitter = emitter
        self.handlers = EventHandlers(self.main, emitter)

    def emit(self, event, arg):
        self.emitter.emit(event, arg)


class EventHandlers:
    def __init__(self, main, emitter):
        self.emitter = emitter

        @self.emitter.on("function.enable.*")
        def enable_function(arg):
            self.logger.debug(f"Received function.enable event with arg {arg}")

        @self.emitter.on("function.disable.*")
        def disable_function(arg):
            self.logger.debug(f"Received function.disable event with arg {arg}")

        @self.emitter.on("web.set")
        def web_event(arg):
            self.logger.debug(f"Received web.event event with arg {arg}")
