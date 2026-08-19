#!/bin/bash
set -Eeuo pipefail

trap 'status=$?; printf "awg3-start: command failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2; exit "$status"' ERR

iptables -F
iptables -t nat -F

ip link delete dev awg0 2>/dev/null || true
awg-quick down /opt/amnezia/awg/awg0.conf 2>/dev/null || true

chmod 0600 /opt/amnezia/awg/awg0.conf
if [ -f /opt/amnezia/awg/awg0.conf ]; then (awg-quick up /opt/amnezia/awg/awg0.conf); fi

# Allow traffic on the TUN interface.
iptables -A INPUT -i awg0 -j ACCEPT
iptables -A FORWARD -i awg0 -j ACCEPT
iptables -A OUTPUT -o awg0 -j ACCEPT

# Allow forwarding traffic only from the VPN.
iptables -A FORWARD -i awg0 -o eth0 -s $AWG_SUBNET_IP/$WIREGUARD_SUBNET_CIDR -j ACCEPT
iptables -A FORWARD -i awg0 -o eth1 -s $AWG_SUBNET_IP/$WIREGUARD_SUBNET_CIDR -j ACCEPT

iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

iptables -t nat -A POSTROUTING -s $AWG_SUBNET_IP/$WIREGUARD_SUBNET_CIDR -o eth0 -j MASQUERADE
iptables -t nat -A POSTROUTING -s $AWG_SUBNET_IP/$WIREGUARD_SUBNET_CIDR -o eth1 -j MASQUERADE

if [ ! -x /opt/amnezia/traffic-limit.sh ]; then
  echo "Mandatory traffic-limit.sh is missing or not executable" >&2
  exit 1
fi
/opt/amnezia/traffic-limit.sh sync awg0 \
  "$AWG_SUBNET_IP/$WIREGUARD_SUBNET_CIDR" \
  "$AWG_DOWNLOAD_LIMIT_MBIT" "$AWG_UPLOAD_LIMIT_MBIT" || {
    echo "Failed to apply mandatory per-device traffic limits" >&2
    exit 1
  }

tail -f /dev/null
