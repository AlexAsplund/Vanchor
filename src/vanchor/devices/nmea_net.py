import socketserver
from threading import Thread


class NmeaNet:
    def __init__(self, main):
        self.main = main
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.emitter = main.event.emitter

        self.main.work_manager.start_worker(self.start)

    def start(self, main):
        self.logger.info("Starting NmeaNet server")
        host = "0.0.0.0"
        port = 10000

        self.server = NmeaNetServer((host, port), NmeaTCPHandler, nmea_net=self)
        self.server.serve_forever()


class NmeaTCPHandler(socketserver.BaseRequestHandler):
    def __init__(self, nmea_net, *args, **kwargs):
        self.main = nmea_net.main
        self.logger = self.main.logging.getLogger(self.__class__.__name__)
        self.emitter = self.main.event.emitter
        super().__init__(*args, **kwargs)

    def send_event(self, message):
        self.logger.info("Sending NMEA")
        msg = message[0]
        self.request.sendall(bytes(msg, "utf-8"))

    def handle(self):
        while 1:
            try:
                rec = self.request.recv(1024)
                try:
                    self.data = rec.decode("ascii")
                except:
                    self.data = rec.decode("utf-8")

                self.data.replace("\r\n", "")

                if self.data and self.data[0] == "$":
                    self.logger.info("Sending raw NMEA message: {}".format(self.data))
                    self.emitter.emit("nmea.parse", self.data)
                elif self.data == "" or self.data == "\r\n":
                    None
                else:

                    self.logger.info(
                        "Invalid data received: {} | {}".format(self.data, rec)
                    )
                    self.request.sendall(bytes(f"ERROR: Invalid data\r\n", "ascii"))
            except Exception as e:
                self.logger.warning("Failed to process message", e)

    def finish(self):
        self.logger.debug("{}".format(self.client_address))
        return socketserver.BaseRequestHandler.finish(self)


class NmeaNetServer(socketserver.TCPServer):
    def __init__(self, *args, nmea_net, **kwargs):
        super().__init__(*args, **kwargs)
        self.nmea_net = nmea_net
        self.main = nmea_net.main
        self.logger = self.main.logging.getLogger(self.__class__.__name__)
        self.emitter = self.main.event.emitter

    def finish_request(self, request, client_address):
        """Finish one request by instantiating RequestHandlerClass."""
        self.logger.info("{} request finished".format(client_address[0]))
        self.RequestHandlerClass(self.nmea_net, request, client_address, self)

    def verify_request(self, request, client_address):
        self.logger.debug("verify_request(%s, %s)", request, client_address)
        return socketserver.TCPServer.verify_request(
            self,
            request,
            client_address,
        )
