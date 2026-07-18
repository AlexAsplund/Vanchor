#!/bin/bash
# If no non-loopback connection is active, raise the setup hotspot.
# BENCH-VERIFY: NM AP autoconnect timing is unverifiable without hardware.
sleep 25
if ! nmcli -t -f NAME,TYPE connection show --active | grep -qv '^lo:'; then
    nmcli connection up vanchor-setup || true
fi
