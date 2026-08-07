#!/usr/bin/env bash
set -Eeuo pipefail

trap 'status=$?; printf "traffic-limit: command failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2; exit "$status"' ERR

die() {
  printf 'traffic-limit: %s\n' "$*" >&2
  exit 1
}

validate_interface() {
  [[ "$1" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "invalid interface: $1"
  ip link show dev "$1" >/dev/null 2>&1 || die "interface does not exist: $1"
}

validate_ipv4() {
  local address="$1" first second third fourth extra octet
  IFS='.' read -r first second third fourth extra <<< "$address"
  [[ -n "$first" && -n "$second" && -n "$third" && -n "$fourth" && -z "$extra" ]] \
    || die "invalid IPv4 address: $address"
  for octet in "$first" "$second" "$third" "$fourth"; do
    [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] && ((10#$octet <= 255)) \
      || die "invalid IPv4 address: $address"
  done
}

validate_minor() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 2 && 10#$1 <= 65534)) \
    || die "class minor must be between 2 and 65534"
}

validate_rate() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 10000)) \
    || die "rate must be between 1 and 10000 Mbit/s"
}

ipv4_to_int() {
  local first second third fourth
  IFS='.' read -r first second third fourth <<< "$1"
  printf '%s\n' "$(((10#$first << 24) + (10#$second << 16) + (10#$third << 8) + 10#$fourth))"
}

init_qdiscs() {
  local interface="$1"
  if ! tc qdisc show dev "$interface" root | grep -Eq '^qdisc htb 1:'; then
    tc qdisc del dev "$interface" root 2>/dev/null || true
    tc qdisc add dev "$interface" root handle 1: htb default 1
  fi
  if ! tc class show dev "$interface" | grep -Eq '^class htb 1:1([[:space:]]|$)'; then
    tc class add dev "$interface" parent 1: classid 1:1 \
      htb rate 10gbit ceil 10gbit
  fi
  if ! tc qdisc show dev "$interface" ingress | grep -Eq '^qdisc ingress ffff:'; then
    tc qdisc add dev "$interface" handle ffff: ingress
  fi
}

apply_limit() {
  local interface="$1" address="$2" minor="$3" download="$4" upload="$5"
  local class_minor
  validate_interface "$interface"
  validate_ipv4 "$address"
  validate_minor "$minor"
  validate_rate "$download"
  validate_rate "$upload"
  printf -v class_minor '%x' "$((10#$minor))"

  init_qdiscs "$interface"

  tc filter del dev "$interface" parent 1: protocol ip pref "$minor" \
    2>/dev/null || true
  tc qdisc del dev "$interface" parent "1:$class_minor" 2>/dev/null || true
  tc class del dev "$interface" parent 1: classid "1:$class_minor" \
    2>/dev/null || true
  tc class add dev "$interface" parent 1: classid "1:$class_minor" \
    htb rate "${download}mbit" ceil "${download}mbit" burst 256k
  tc qdisc add dev "$interface" parent "1:$class_minor" \
    handle "${class_minor}:" fq_codel
  tc filter add dev "$interface" parent 1: protocol ip pref "$minor" \
    flower dst_ip "$address/32" classid "1:$class_minor"

  tc filter del dev "$interface" parent ffff: protocol ip pref "$minor" \
    2>/dev/null || true
  tc filter add dev "$interface" parent ffff: protocol ip pref "$minor" \
    flower src_ip "$address/32" \
    action police rate "${upload}mbit" burst 256k conform-exceed drop/ok
}

remove_limit() {
  local interface="$1" minor="$2" class_minor
  validate_interface "$interface"
  validate_minor "$minor"
  printf -v class_minor '%x' "$((10#$minor))"

  tc filter del dev "$interface" parent 1: protocol ip pref "$minor" \
    2>/dev/null || true
  tc filter del dev "$interface" parent ffff: protocol ip pref "$minor" \
    2>/dev/null || true
  tc class del dev "$interface" parent 1: classid "1:$class_minor" \
    2>/dev/null || true
}

sync_limits() {
  local interface="$1" subnet="$2" download="$3" upload="$4"
  local subnet_address prefix base_int max_offset address cidr address_int minor
  local peer_output allowed_values
  local -a values
  validate_interface "$interface"
  subnet_address="${subnet%/*}"
  prefix="${subnet#*/}"
  [[ "$subnet" == */* ]] || die "subnet must use CIDR notation"
  validate_ipv4 "$subnet_address"
  [[ "$prefix" =~ ^[0-9]+$ ]] && ((10#$prefix >= 16 && 10#$prefix <= 30)) \
    || die "subnet prefix must be between /16 and /30"
  validate_rate "$download"
  validate_rate "$upload"
  peer_output="$(awg show "$interface" allowed-ips)" \
    || die "cannot read peers from $interface"

  # Recreating the root and ingress qdiscs makes synchronization idempotent and
  # removes stale classes belonging to peers that no longer exist.
  tc qdisc del dev "$interface" root 2>/dev/null || true
  tc qdisc add dev "$interface" root handle 1: htb default 1
  tc qdisc del dev "$interface" ingress 2>/dev/null || true
  tc qdisc add dev "$interface" handle ffff: ingress
  tc class add dev "$interface" parent 1: classid 1:1 \
    htb rate 10gbit ceil 10gbit

  base_int="$(ipv4_to_int "$subnet_address")"
  max_offset=$(((1 << (32 - 10#$prefix)) - 1))
  while IFS=$'\t' read -r _ allowed_values; do
    IFS=',' read -ra values <<< "$allowed_values"
    for cidr in "${values[@]}"; do
      cidr="${cidr#"${cidr%%[![:space:]]*}"}"
      cidr="${cidr%"${cidr##*[![:space:]]}"}"
      [[ "$cidr" == */32 ]] || continue
      address="${cidr%/32}"
      validate_ipv4 "$address"
      address_int="$(ipv4_to_int "$address")"
      minor=$((address_int - base_int))
      if ((minor < 2 || minor >= max_offset || minor > 65534)); then
        die "peer address $address is outside the supported client range $subnet"
      fi
      apply_limit "$interface" "$address" "$minor" "$download" "$upload"
    done
  done <<< "$peer_output"
}

command -v tc >/dev/null 2>&1 || die "tc is unavailable; install iproute2"
command -v ip >/dev/null 2>&1 || die "ip is unavailable; install iproute2"

action="${1:-}"
case "$action" in
  apply)
    (($# == 6)) || die "usage: $0 apply INTERFACE IPV4 CLASS DOWNLOAD_MBIT UPLOAD_MBIT"
    apply_limit "$2" "$3" "$4" "$5" "$6"
    ;;
  remove)
    (($# == 3)) || die "usage: $0 remove INTERFACE CLASS"
    remove_limit "$2" "$3"
    ;;
  sync)
    (($# == 5)) || die "usage: $0 sync INTERFACE SUBNET DOWNLOAD_MBIT UPLOAD_MBIT"
    command -v awg >/dev/null 2>&1 || die "awg is unavailable"
    sync_limits "$2" "$3" "$4" "$5"
    ;;
  *)
    die "expected action: apply, remove, or sync"
    ;;
esac
