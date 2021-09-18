

apt-get remove --purge hostapd -yqq
apt-get update -yqq
apt-get install hostapd dnsmasq -y

cat > /etc/dnsmasq.conf <<EOF
interface=wlan0
dhcp-range=10.0.0.2,10.0.0.5,255.255.255.0,12h
EOF


APPASS="Vanchor2021"
APSSID="Vanchor"

cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
hw_mode=g
channel=10
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
wpa_passphrase=$APPASS
ssid=$APSSID
ieee80211n=1
wmm_enabled=1
ht_capab=[HT40][SHORT-GI-20][DSSS_CCK-40]
EOF

sed -i -- 's/allow-hotplug wlan0//g' /etc/network/interfaces
sed -i -- 's/iface wlan0 inet manual//g' /etc/network/interfaces
sed -i -- 's/    wpa-conf \/etc\/wpa_supplicant\/wpa_supplicant.conf//g' /etc/network/interfaces
sed -i -- 's/#DAEMON_CONF=""/DAEMON_CONF="\/etc\/hostapd\/hostapd.conf"/g' /etc/default/hostapd

cat >> /etc/network/interfaces <<EOF
iface wlan0 inet static
    address 10.0.0.1
    netmask 255.255.255.0
    network 10.0.0.0
    broadcast 10.0.0.255

auto eth0
allow-hotplug eth0
EOF

echo "denyinterfaces wlan0" >> /etc/dhcpcd.conf


systemctl unmask hostapd
systemctl enable hostapd
systemctl enable dnsmasq

sudo service hostapd start
sudo service dnsmasq start


echo "" >> /etc/rc.local
echo "ifup eth0" >> /etc/rc.local
echo "ifup wlan0" >> /etc/rc.local
echo "sudo iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE" >> /etc/rc.local


echo "All done! Rebooting"



reboot