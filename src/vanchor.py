import sys
from vanchor.resources import Main
from time import sleep


if "-debug" in sys.argv:
    print("DEBUG is turned on")
    debug = True
else:
    debug = False

print("Loading Vanchor...")
vanchor = Main(debug=debug)
vanchor.run()

if __name__ == "__main__":
    while True:
        sleep(1)
