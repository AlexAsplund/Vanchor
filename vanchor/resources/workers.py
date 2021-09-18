import threading
from time import time
from time import sleep


class WorkerManager:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main
        self.workers = {}
        self.stop = False

    def start_worker(self, worker, args=[], **kwargs):
        worker_name = worker.__name__
        self.logger.info("Starting worker {}".format(worker.__name__))
        worker_thread = threading.Thread(
            name=worker_name,
            target=self.worker_loop,
            daemon=True,
            kwargs=kwargs,
            args=[worker] + args,
        )
        self.workers[worker_name] = {"Thread": worker_thread, "Run": True}
        worker_thread.start()

    def stop_worker(self, worker):
        worker_name = worker.__name__
        self.logger.info("Stopping worker {}".format(worker_name))
        self.workers[worker_name]["Run"] = False

    def worker_loop(self, *arg, **kwargs):
        self.logger.info("Starting worker function {}".format(arg[0].__name__))
        main = self.main
        func = arg[0]
        if "timer" in kwargs:
            self.logger.info("Starting worker {} with timer".format(func.__name__))
            timer = True
            millis = time() * 1000
        else:
            self.logger.info("Starting worker {}".format(func.__name__))
            timer = False

        while self.workers[arg[0].__name__]["Run"] and self.stop != True:
            if timer:
                if ((time() * 1000) - millis) > kwargs["timer"]:
                    func(*arg)
                    millis = time() * 1000
                else:
                    sleep(0.005)
            else:
                func(*arg)

        self.logger.info("Worker {} stopped".format(func.__name__))


class Workers:
    def __init__(self, main):
        self.logger = main.logging.getLogger(self.__class__.__name__)
        self.main = main

    def test_worker(self, **kwarg):
        print("hello")
        sleep(5)
