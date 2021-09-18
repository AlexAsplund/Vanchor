SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

echo "Installing packages..."
sudo apt-get install -y python-smbus
sudo apt-get install -y i2c-tools
sudo apt-get install python3 python3-flask python3-smbus python3-pyproj python3-pip python3-yaml python3-numpy -y
sudo pip3 install adafruit-circuitpython-lsm303_accel adafruit-circuitpython-lsm303dlh_mag pynmea2
sudo apt install libblas3 liblapack3 liblapack-dev libblas-dev


echo "Configuring rc.local"
echo "">>/etc/rc.local
echo "ifup eth0">>/etc/rc.local

echo "Creating service"
cp "$SCRIPT_DIR/vanchor.service" "/etc/systemd/system/vanchor.service"

echo "Reloading daemon"
sudo systemctl daemon-reload

echo "Enabling service"
sudo systemctl enable vanchor.service