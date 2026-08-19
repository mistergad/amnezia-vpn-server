#!/usr/bin/env bash
set -Eeuo pipefail

readonly TRAFFIC_LIMIT_BINARY="/opt/amnezia/traffic-limit.sh"
readonly LOCK_DIR="${AWG_PEER_MANAGER_LOCK_DIR:-/tmp/amnezia-peer-manager.lock}"

LOCK_ACQUIRED=false
PSK_FILE=""
declare -a APPLIED_KEYS=()
declare -a APPLIED_MINORS=()

die() {
  printf 'peer-manager: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local owner=""
  if [[ -n "$PSK_FILE" ]]; then
    rm -f -- "$PSK_FILE"
  fi
  if [[ "$LOCK_ACQUIRED" == true ]]; then
    if [[ -r "$LOCK_DIR/pid" ]]; then
      read -r owner < "$LOCK_DIR/pid" || true
    fi
    if [[ "$owner" == "$$" ]]; then
      rm -f -- "$LOCK_DIR/pid"
      rmdir -- "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
}

trap cleanup EXIT

acquire_lock() {
  local attempt=0 owner=""
  while ((attempt < 300)); do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      LOCK_ACQUIRED=true
      return 0
    fi
    if [[ -r "$LOCK_DIR/pid" ]]; then
      read -r owner < "$LOCK_DIR/pid" || true
      if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
        rm -f -- "$LOCK_DIR/pid"
        rmdir -- "$LOCK_DIR" 2>/dev/null || true
        continue
      fi
    fi
    sleep 0.1
    ((attempt += 1))
  done
  die "another peer operation is still running after 30 seconds"
}

validate_interface() {
  [[ "$1" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "invalid interface: $1"
  awg show "$1" >/dev/null 2>&1 || die "interface does not exist: $1"
}

validate_key() {
  [[ "$1" =~ ^[A-Za-z0-9+/]{43}=$ ]] || die "invalid WireGuard key"
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

validate_boolean() {
  [[ "$1" == "true" || "$1" == "false" ]] || die "expected true or false"
}

validate_config_path() {
  [[ "$1" =~ ^/(opt/amnezia/awg|etc/amnezia/amneziawg)/[A-Za-z0-9_.-]+\.conf$ ]] \
    || die "invalid AWG config path"
}

save_config() {
  local enabled="$1" config_path="$2"
  [[ "$enabled" == true ]] || return 0
  awg-quick save "$config_path"
}

rollback_apply() {
  local interface="$1" public_key="$2" minor="$3"
  local rate_enabled="$4" save_enabled="$5" config_path="$6"
  awg set "$interface" peer "$public_key" remove 2>/dev/null || true
  if [[ "$rate_enabled" == true ]]; then
    "$TRAFFIC_LIMIT_BINARY" remove "$interface" "$minor" 2>/dev/null || true
  fi
  save_config "$save_enabled" "$config_path" 2>/dev/null || true
}

rollback_batch() {
  local interface="$1" rate_enabled="$2" save_enabled="$3" config_path="$4"
  local index
  for ((index = ${#APPLIED_KEYS[@]} - 1; index >= 0; index -= 1)); do
    awg set "$interface" peer "${APPLIED_KEYS[$index]}" remove 2>/dev/null || true
    if [[ "$rate_enabled" == true ]]; then
      "$TRAFFIC_LIMIT_BINARY" remove \
        "$interface" "${APPLIED_MINORS[$index]}" 2>/dev/null || true
    fi
  done
  save_config "$save_enabled" "$config_path" 2>/dev/null || true
}

apply_peer() {
  local interface="$1" public_key="$2" address="$3" minor="$4"
  local rate_enabled="$5" download="$6" upload="$7"
  local save_enabled="$8" config_path="$9" preshared_key=""

  validate_interface "$interface"
  validate_key "$public_key"
  validate_ipv4 "$address"
  validate_boolean "$rate_enabled"
  validate_boolean "$save_enabled"
  validate_config_path "$config_path"
  if [[ "$rate_enabled" == true ]]; then
    validate_minor "$minor"
    validate_rate "$download"
    validate_rate "$upload"
    [[ -x "$TRAFFIC_LIMIT_BINARY" ]] || die "traffic-limit helper is unavailable"
  fi
  IFS= read -r preshared_key || true
  validate_key "$preshared_key"

  PSK_FILE="$(mktemp /tmp/amnezia-peer-psk.XXXXXX)"
  chmod 0600 "$PSK_FILE"
  printf '%s\n' "$preshared_key" > "$PSK_FILE"

  if ! awg set "$interface" peer "$public_key" \
      preshared-key "$PSK_FILE" allowed-ips "$address/32"; then
    die "cannot add peer to $interface"
  fi
  if [[ "$rate_enabled" == true ]] && \
      ! "$TRAFFIC_LIMIT_BINARY" apply \
        "$interface" "$address" "$minor" "$download" "$upload"; then
    rollback_apply \
      "$interface" "$public_key" "$minor" "$rate_enabled" "$save_enabled" "$config_path"
    die "cannot apply the peer traffic limit"
  fi
  if ! save_config "$save_enabled" "$config_path"; then
    rollback_apply \
      "$interface" "$public_key" "$minor" "$rate_enabled" "$save_enabled" "$config_path"
    die "cannot persist the AWG configuration"
  fi
}

apply_many() {
  local interface="$1" rate_enabled="$2" download="$3" upload="$4"
  local save_enabled="$5" config_path="$6"
  local public_key address minor preshared_key extra index
  local -a batch_keys=() batch_addresses=() batch_minors=() batch_psks=()

  validate_interface "$interface"
  validate_boolean "$rate_enabled"
  validate_boolean "$save_enabled"
  validate_config_path "$config_path"
  if [[ "$rate_enabled" == true ]]; then
    validate_rate "$download"
    validate_rate "$upload"
    [[ -x "$TRAFFIC_LIMIT_BINARY" ]] || die "traffic-limit helper is unavailable"
  fi

  while IFS=$'\t' read -r public_key address minor preshared_key extra; do
    [[ -n "$public_key" && -z "$extra" ]] || die "invalid peer batch record"
    validate_key "$public_key"
    validate_ipv4 "$address"
    validate_key "$preshared_key"
    if [[ "$rate_enabled" == true ]]; then
      validate_minor "$minor"
    fi
    batch_keys+=("$public_key")
    batch_addresses+=("$address")
    batch_minors+=("$minor")
    batch_psks+=("$preshared_key")
  done
  ((${#batch_keys[@]} > 0)) || die "peer batch is empty"

  PSK_FILE="$(mktemp /tmp/amnezia-peer-psk.XXXXXX)"
  chmod 0600 "$PSK_FILE"
  for ((index = 0; index < ${#batch_keys[@]}; index += 1)); do
    public_key="${batch_keys[$index]}"
    address="${batch_addresses[$index]}"
    minor="${batch_minors[$index]}"
    printf '%s\n' "${batch_psks[$index]}" > "$PSK_FILE"
    if ! awg set "$interface" peer "$public_key" \
        preshared-key "$PSK_FILE" allowed-ips "$address/32"; then
      rollback_batch "$interface" "$rate_enabled" "$save_enabled" "$config_path"
      die "cannot add peer batch to $interface"
    fi
    APPLIED_KEYS+=("$public_key")
    APPLIED_MINORS+=("$minor")
    if [[ "$rate_enabled" == true ]] && \
        ! "$TRAFFIC_LIMIT_BINARY" apply \
          "$interface" "$address" "$minor" "$download" "$upload"; then
      rollback_batch "$interface" "$rate_enabled" "$save_enabled" "$config_path"
      die "cannot apply a peer batch traffic limit"
    fi
  done
  if ! save_config "$save_enabled" "$config_path"; then
    rollback_batch "$interface" "$rate_enabled" "$save_enabled" "$config_path"
    die "cannot persist the AWG configuration"
  fi
}

remove_peer() {
  local interface="$1" public_key="$2" minor="$3"
  local rate_enabled="$4" save_enabled="$5" config_path="$6"

  validate_interface "$interface"
  validate_key "$public_key"
  validate_boolean "$rate_enabled"
  validate_boolean "$save_enabled"
  validate_config_path "$config_path"
  if [[ "$rate_enabled" == true ]]; then
    validate_minor "$minor"
  fi

  awg set "$interface" peer "$public_key" remove \
    || die "cannot remove peer from $interface"
  save_config "$save_enabled" "$config_path" \
    || die "cannot persist the AWG configuration"
  if [[ "$rate_enabled" == true ]]; then
    "$TRAFFIC_LIMIT_BINARY" remove "$interface" "$minor" 2>/dev/null \
      || printf 'peer-manager: warning: stale traffic class for %s\n' "$minor" >&2
  fi
}

command -v awg >/dev/null 2>&1 || die "awg is unavailable"
command -v awg-quick >/dev/null 2>&1 || die "awg-quick is unavailable"
acquire_lock

action="${1:-}"
case "$action" in
  apply)
    (($# == 10)) || die "usage: $0 apply IFACE PUBLIC_KEY IPV4 CLASS RATE_ENABLED DOWNLOAD UPLOAD SAVE_ENABLED CONFIG"
    apply_peer "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}"
    ;;
  apply-many)
    (($# == 7)) || die "usage: $0 apply-many IFACE RATE_ENABLED DOWNLOAD UPLOAD SAVE_ENABLED CONFIG"
    apply_many "$2" "$3" "$4" "$5" "$6" "$7"
    ;;
  remove)
    (($# == 7)) || die "usage: $0 remove IFACE PUBLIC_KEY CLASS RATE_ENABLED SAVE_ENABLED CONFIG"
    remove_peer "$2" "$3" "$4" "$5" "$6" "$7"
    ;;
  *)
    die "expected action: apply, apply-many or remove"
    ;;
esac
