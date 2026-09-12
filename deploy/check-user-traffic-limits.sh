#!/usr/bin/env bash
set -Eeuo pipefail

trap 'status=$?; printf "check-user-traffic-limits: command failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2; exit "$status"' ERR

DB_NAME="${AMNEZIA_DB_NAME:-amnezia}"
CONTAINER_NAME="${AMNEZIA_CONTAINER_NAME:-amnezia-awg2}"
INTERFACE="${AMNEZIA_INTERFACE:-awg0}"
ENV_FILE="${AMNEZIA_ENV_FILE:-/etc/amnezia-service.env}"

die() {
  printf 'check-user-traffic-limits: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'Usage: sudo bash %s EMAIL\n' "$0"
  printf "Example: sudo bash %s 'arsen@mail.ru'\n" "$0"
}

read_env_value() {
  local name="$1"
  [[ -r "$ENV_FILE" ]] || return 0
  sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1
}

[[ $# -eq 1 && -n "$1" ]] || {
  usage >&2
  exit 2
}
EMAIL="$1"

[[ "$DB_NAME" =~ ^[A-Za-z0-9_]+$ ]] || die "invalid database name"
[[ "$CONTAINER_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid container name"
[[ "$INTERFACE" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "invalid interface name"
command -v sudo >/dev/null 2>&1 || die "sudo is not installed"
command -v docker >/dev/null 2>&1 || die "docker is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1 \
  || die "container does not exist: $CONTAINER_NAME"
docker exec "$CONTAINER_NAME" sh -lc 'command -v tc >/dev/null 2>&1' \
  || die "tc is not installed in $CONTAINER_NAME"

AWG_SUBNET="$(read_env_value AWG_SUBNET)"
AWG_SUBNET="${AWG_SUBNET:-10.8.1.0/24}"
CONFIGURED_DOWNLOAD="$(read_env_value AWG_DOWNLOAD_LIMIT_MBPS)"
CONFIGURED_UPLOAD="$(read_env_value AWG_UPLOAD_LIMIT_MBPS)"

rows="$({
  sudo -u postgres psql \
    -X -v ON_ERROR_STOP=1 -v email="$EMAIL" \
    -d "$DB_NAME" -At -F $'\t' <<'SQL'
SELECT
    vc.assigned_ip,
    replace(replace(vc.label, chr(9), ' '), chr(10), ' '),
    vc.status::text
FROM vpn_credentials AS vc
JOIN users AS u ON u.id = vc.user_id
WHERE lower(u.email) = lower(:'email')
  AND vc.revoked_at IS NULL
ORDER BY vc.assigned_ip;
SQL
})"

[[ -n "$rows" ]] || die "no active or suspended devices found for $EMAIL"

printf 'Клиент: %s\n' "$EMAIL"
printf 'Подсеть: %s\n' "$AWG_SUBNET"
if [[ -n "$CONFIGURED_DOWNLOAD" || -n "$CONFIGURED_UPLOAD" ]]; then
  printf 'Настройки сервиса: загрузка %s Мбит/с, отдача %s Мбит/с\n' \
    "${CONFIGURED_DOWNLOAD:-не задана}" "${CONFIGURED_UPLOAD:-не задана}"
fi

while IFS=$'\t' read -r address label status; do
  minor="$(python3 - "$address" "$AWG_SUBNET" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
network = ipaddress.ip_network(sys.argv[2], strict=False)
if address.version != 4 or network.version != 4 or address not in network:
    raise SystemExit(f"{address} is outside {network}")
print(int(address) - int(network.network_address))
PY
)"
  printf -v class_minor '%x' "$((10#$minor))"

  download_filter="$(
    docker exec "$CONTAINER_NAME" tc filter show dev "$INTERFACE" \
      parent 1: protocol ip pref "$minor" 2>/dev/null || true
  )"
  download_class="$(
    docker exec "$CONTAINER_NAME" tc -s class show dev "$INTERFACE" \
      classid "1:$class_minor" 2>/dev/null || true
  )"
  upload_filter="$(
    docker exec "$CONTAINER_NAME" tc -s filter show dev "$INTERFACE" \
      parent ffff: protocol ip pref "$minor" 2>/dev/null || true
  )"

  download_limit="не ограничена"
  if [[ "$download_filter" == *"dst_ip $address"* \
        && "$download_class" =~ rate[[:space:]]+([^[:space:]]+) ]]; then
    download_limit="${BASH_REMATCH[1]}"
  fi

  upload_limit="не ограничена"
  if [[ "$upload_filter" == *"src_ip $address"* \
        && "$upload_filter" =~ rate[[:space:]]+([^[:space:]]+) ]]; then
    upload_limit="${BASH_REMATCH[1]}"
  fi

  printf '\nУстройство: %s\n' "$label"
  printf '  IP: %s\n' "$address"
  printf '  Статус: %s\n' "$status"
  printf '  Загрузка на устройство: %s\n' "$download_limit"
  printf '  Отдача с устройства: %s\n' "$upload_limit"
done <<< "$rows"
