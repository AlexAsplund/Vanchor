import os
import random
import string
from re import match
from shutil import copyfile
from flask import Flask, render_template, request, jsonify


class WebApplication:
    def __init__(self, main, debug=False):
        self.logger = main.logging.getLogger(self.__class__.__name__)

        self.main = main
        self.emitter = main.event.emitter
        self.debug = debug

        template_dir = os.path.abspath("./www/templates/")
        static_dir = os.path.abspath("./www/static")

        app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

        @app.route("/")
        def index():
            return render_template("index.html")

        @app.route("/config")
        def config():
            return render_template("config.html")

        @app.route("/menu")
        def menu():
            return render_template("menu.html")

        @app.route("/command", methods=["POST"])
        def command():

            request_data = request.get_json(force=True)
            self.logger.debug(request_data)

            self.emitter.emit(request_data["Event"], request_data["Argument"])

            return jsonify({"Status": "OK"})

        @app.route("/getConfig", methods=["GET"])
        def get_config():
            return jsonify(self.main.config.get())

        @app.route("/setConfig", methods=["POST"])
        def set_config_file():

            request_data = request.get_json(force=True)
            self.logger.info(
                "Sending {} {}".format(request_data["Path"], request_data["Value"])
            )
            main.event.emit(
                "set.config",
                [request_data["Path"], request_data["Value"]],
            )

            return jsonify({"Status": "OK"})

        @app.route("/saveConfig", methods=["POST"])
        def save_config():

            self.emitter.emit("config.save")

            return jsonify({"Status": "OK"})

        @app.route("/status")
        def status():

            status_data = self.main.data.get()

            return jsonify(status_data)

        # Routes
        @app.route("/status/<path:path>")
        def get_status_path(path):
            return jsonify(self.main.data.get(path))

        #
        self.app = app

        main.work_manager.start_worker(self.web_worker)

        @app.route("/upload/update", methods=["GET", "POST"])
        def upload_file():
            if request.method == "POST":
                try:
                    a = os.system("mkdir uploads")
                except:
                    None
                # check if the post request has the file part
                if "zip" not in request.files:
                    return jsonify({"Status": "Failed", "Data": request.files})
                file = request.files["zip"]
                # If the user does not select a file, the browser submits an
                # empty file without a filename.
                if file.filename == "":
                    return jsonify({"Status": "FileDoesNotExistError"})
                if match("vanchor.*\.zip", file.filename) == None:
                    return jsonify({"Status": "FileDoesNotMatchError"})
                if file:
                    filename = (
                        "".join(
                            random.choices(string.ascii_uppercase + string.digits, k=10)
                        )
                        + ".zip"
                    )
                    path = os.path.join("uploads/", filename)
                    self.logger.info("Saving file to {}".format(path))
                    file.save(path)
                    self.emitter.emit("main.update.uploaded", filename)
                    return jsonify({"Status": "OK"})

    def web_worker(self, *arg):
        self.logger.info("Starting web worker")
        self.server = self.app.run(
            host="0.0.0.0",
            port=self.main.config.get("Flask/Port"),
            threaded=True,
            debug=self.debug,
        )
