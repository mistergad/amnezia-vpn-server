#!/usr/bin/env bash
set -Eeuo pipefail

# One-command deployment for a dedicated Ubuntu 24.04 VPS.
# The AWG3 container itself is built and configured by pinned upstream scripts
# from amnezia-vpn/amnezia-client; see vendor/amnezia-client/UPSTREAM.md.

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly VENDOR_DIR="$SCRIPT_DIR/vendor/amnezia-client"
readonly APP_DIR="/opt/amnezia-service"
readonly APP_USER="amnezia-service"
readonly ENV_FILE="/etc/amnezia-service.env"
readonly CREDENTIALS_FILE="/root/amnezia-service-credentials.txt"
readonly CADDY_DATA_DIR="/var/lib/caddy/.local/share/caddy"
# AmneziaVPN retains this historical identifier for its userspace AWG backend,
# including protocol version 3.
readonly CONTAINER_NAME="amnezia-awg2"
readonly AWG_BUILD_DIR="/opt/amnezia/amnezia-awg2"
readonly GENERATED_DIR="/opt/amnezia/deploy-generated"
readonly AWG_SUBNET_IP="10.8.1.0"
readonly AWG_SUBNET_CIDR="24"
readonly AWG_IMAGE="amneziavpn/amneziawg-go:3.0.20260805@sha256:8447c91637c37536dd99b8bbd4420c819ac9f330f047804197291625bfb0ea8a"

DOMAIN="${DOMAIN:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
AWG_PORT="${AWG_PORT:-55424}"
AWG_DOWNLOAD_LIMIT_MBIT="${AWG_DOWNLOAD_LIMIT_MBIT:-}"
AWG_UPLOAD_LIMIT_MBIT="${AWG_UPLOAD_LIMIT_MBIT:-}"
PUBLIC_INTERFACE="${PUBLIC_INTERFACE:-eth0}"
IP_TLS_MODE="${IP_TLS_MODE:-public}"
HOST_IS_IP=false
RESET_FOR_AWG3=false

log() { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARNING: %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  sudo bash deploy/vps-bootstrap.sh [options]
  sudo bash deploy/vps-bootstrap.sh --host vpn.example.com [options]
  sudo bash deploy/vps-bootstrap.sh --host 203.0.113.10 [options]

Options:
  --interface IFACE            Interface for automatic IPv4 detection (default: eth0)
  --host HOST                  Override detected IPv4 with a domain or IPv4 address
  --domain DOMAIN              Backward-compatible alias for --host
  --ip IPV4                    Alias for --host when no domain is available
  --admin-email EMAIL          Initial administrator (default: derived from HOST)
  --admin-password PASSWORD    At least 12 safe ASCII characters; generated if omitted
  --awg-port PORT              Public AWG3 UDP port (default: 55424)
  --download-limit-mbps RATE   Per-device download limit (default: 10)
  --upload-limit-mbps RATE     Per-device upload limit (default: 8)
  --ip-tls-mode MODE           TLS for an IPv4 host: public or internal (default: public)
  --reset-for-awg3             DELETE every account/key and rebuild the VPN as AWG3
  -h, --help                   Show this help

The script targets a dedicated Ubuntu 24.04 server. It installs Docker,
PostgreSQL, Caddy, AWG3 and the control panel. Re-running it is supported.
The destructive --reset-for-awg3 option is intended for a one-time migration
from AWG2 and must not be used during later routine updates.
For a public IPv4, Caddy obtains and renews a publicly trusted short-lived
Let's Encrypt certificate. Use --ip-tls-mode internal only as a fallback.
EOF
}

while (($#)); do
  case "$1" in
    --host|--domain|--ip) DOMAIN="${2:-}"; shift 2 ;;
    --interface) PUBLIC_INTERFACE="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="${2:-}"; shift 2 ;;
    --awg-port) AWG_PORT="${2:-}"; shift 2 ;;
    --download-limit-mbps) AWG_DOWNLOAD_LIMIT_MBIT="${2:-}"; shift 2 ;;
    --upload-limit-mbps) AWG_UPLOAD_LIMIT_MBIT="${2:-}"; shift 2 ;;
    --ip-tls-mode) IP_TLS_MODE="${2:-}"; shift 2 ;;
    --reset-for-awg3) RESET_FOR_AWG3=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

read_env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

AWG_DOWNLOAD_LIMIT_MBIT="${AWG_DOWNLOAD_LIMIT_MBIT:-$(read_env_value AWG_DOWNLOAD_LIMIT_MBPS)}"
AWG_UPLOAD_LIMIT_MBIT="${AWG_UPLOAD_LIMIT_MBIT:-$(read_env_value AWG_UPLOAD_LIMIT_MBPS)}"
AWG_DOWNLOAD_LIMIT_MBIT="${AWG_DOWNLOAD_LIMIT_MBIT:-10}"
AWG_UPLOAD_LIMIT_MBIT="${AWG_UPLOAD_LIMIT_MBIT:-8}"

is_ipv4() {
  local address="$1" first second third fourth extra octet
  IFS='.' read -r first second third fourth extra <<< "$address"
  [[ -n "$first" && -n "$second" && -n "$third" && -n "$fourth" && -z "$extra" ]] || return 1
  for octet in "$first" "$second" "$third" "$fourth"; do
    [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] || return 1
    ((10#$octet <= 255)) || return 1
  done
}

is_non_public_ipv4() {
  local first second third fourth
  IFS='.' read -r first second third fourth <<< "$1"
  first=$((10#$first))
  second=$((10#$second))
  ((first == 0 || first == 10 || first == 127 || first >= 224)) && return 0
  ((first == 100 && second >= 64 && second <= 127)) && return 0
  ((first == 169 && second == 254)) && return 0
  ((first == 172 && second >= 16 && second <= 31)) && return 0
  ((first == 192 && second == 168)) && return 0
  ((first == 198 && (second == 18 || second == 19))) && return 0
  return 1
}

detect_interface_ipv4() {
  local interface="$1" route_line detected
  route_line="$(ip -4 route get 1.1.1.1 oif "$interface" 2>/dev/null || true)"
  detected="$(awk '{
    for (index = 1; index <= NF; index++) {
      if ($index == "src" && index < NF) {
        print $(index + 1)
        exit
      }
    }
  }' <<< "$route_line")"
  if [[ -z "$detected" ]]; then
    detected="$(ip -4 -o address show dev "$interface" scope global \
      | awk 'NR == 1 {split($4, address, "/"); print address[1]}')"
  fi
  printf '%s\n' "$detected"
}

[[ "${EUID}" -eq 0 ]] || die "Run this script as root (sudo)."
[[ -r /etc/os-release ]] || die "Cannot identify the operating system."
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || \
  die "Ubuntu 24.04 is required (found ${PRETTY_NAME:-unknown})."
[[ "$PUBLIC_INTERFACE" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "Invalid network interface name."
if [[ -z "$DOMAIN" ]]; then
  command -v ip >/dev/null 2>&1 || die "The ip command is unavailable; use --host explicitly."
  ip link show dev "$PUBLIC_INTERFACE" >/dev/null 2>&1 || \
    die "Network interface $PUBLIC_INTERFACE does not exist; use --interface or --host."
  DOMAIN="$(detect_interface_ipv4 "$PUBLIC_INTERFACE")"
  [[ -n "$DOMAIN" ]] || \
    die "No global IPv4 address found on $PUBLIC_INTERFACE; use --host explicitly."
  is_ipv4 "$DOMAIN" || die "The address detected on $PUBLIC_INTERFACE is not valid: $DOMAIN"
  if is_non_public_ipv4 "$DOMAIN"; then
    die "Detected non-public IPv4 $DOMAIN on $PUBLIC_INTERFACE; use --host with the VPS public address."
  fi
  log "Detected VPS IPv4 $DOMAIN on $PUBLIC_INTERFACE"
fi
if is_ipv4 "$DOMAIN"; then
  HOST_IS_IP=true
elif [[ "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; then
  HOST_IS_IP=false
else
  die "Host must be a valid domain or IPv4 address: $DOMAIN"
fi
[[ "$AWG_PORT" =~ ^[0-9]+$ ]] && ((AWG_PORT >= 1024 && AWG_PORT <= 65535)) || \
  die "--awg-port must be between 1024 and 65535."
[[ "$AWG_DOWNLOAD_LIMIT_MBIT" =~ ^[0-9]+$ ]] \
  && ((AWG_DOWNLOAD_LIMIT_MBIT >= 1 && AWG_DOWNLOAD_LIMIT_MBIT <= 10000)) || \
  die "--download-limit-mbps must be between 1 and 10000."
[[ "$AWG_UPLOAD_LIMIT_MBIT" =~ ^[0-9]+$ ]] \
  && ((AWG_UPLOAD_LIMIT_MBIT >= 1 && AWG_UPLOAD_LIMIT_MBIT <= 10000)) || \
  die "--upload-limit-mbps must be between 1 and 10000."
[[ "$IP_TLS_MODE" == "public" || "$IP_TLS_MODE" == "internal" ]] || \
  die "--ip-tls-mode must be public or internal."

EXISTING_ADMIN_EMAIL="$(read_env_value ADMIN_EMAIL)"
if [[ "$HOST_IS_IP" == true ]]; then
  ADMIN_EMAIL="${ADMIN_EMAIL:-${EXISTING_ADMIN_EMAIL:-admin@localhost}}"
else
  ADMIN_EMAIL="${ADMIN_EMAIL:-${EXISTING_ADMIN_EMAIL:-admin@$DOMAIN}}"
fi
[[ "$ADMIN_EMAIL" =~ ^[A-Za-z0-9.!#$%\&\'*+/=?^_\`{|}~-]+@[A-Za-z0-9.-]+$ ]] || \
  die "Invalid administrator email."
if [[ -n "$ADMIN_PASSWORD" ]]; then
  [[ "$ADMIN_PASSWORD" =~ ^[A-Za-z0-9._~!@%+=:-]{12,128}$ ]] || \
    die "Admin password must contain 12-128 safe ASCII characters."
fi

for required_file in \
  "$VENDOR_DIR/awg2/Dockerfile" \
  "$VENDOR_DIR/awg2/configure_container.sh" \
  "$VENDOR_DIR/awg2/run_container.sh" \
  "$VENDOR_DIR/awg2/start.sh" \
  "$SCRIPT_DIR/awg2-start.sh" \
  "$SCRIPT_DIR/awg2-traffic-limit.sh" \
  "$VENDOR_DIR/build_container.sh" \
  "$VENDOR_DIR/prepare_host.sh" \
  "$VENDOR_DIR/LICENSE"; do
  [[ -f "$required_file" ]] || die "Missing vendored upstream file: $required_file"
done

umask 077
export DEBIAN_FRONTEND=noninteractive

log "Installing operating-system packages"
apt-get update
apt-get install -y \
  apt-transport-https ca-certificates curl debian-archive-keyring debian-keyring \
  docker.io gettext-base gnupg openssl postgresql postgresql-contrib \
  python3 python3-pip python3-venv sudo ufw
systemctl enable --now docker postgresql

if [[ ! -f /etc/apt/sources.list.d/caddy-stable.list \
    || ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]]; then
  log "Installing Caddy from its official stable repository"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  chmod o+r /etc/apt/sources.list.d/caddy-stable.list
fi
apt-get update
apt-get install -y caddy

if [[ "$RESET_FOR_AWG3" == true ]]; then
  log "Destructive AWG3 reset: deleting every account, key and old VPN peer"
  systemctl stop amnezia-service 2>/dev/null || true
  if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    docker rm --force "$CONTAINER_NAME" >/dev/null
  fi
  if runuser -u postgres -- psql -tAc \
      "SELECT 1 FROM pg_database WHERE datname='amnezia'" | grep -q 1; then
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
      -c "DROP DATABASE amnezia WITH (FORCE)"
  fi
fi

rand_between() {
  local minimum="$1" maximum="$2"
  printf '%s\n' $((minimum + $(od -An -N4 -tu4 /dev/urandom) % (maximum - minimum + 1)))
}

generate_awg_parameters() {
  JUNK_PACKET_COUNT="$(rand_between 4 6)"
  JUNK_PACKET_MIN_SIZE="10"
  JUNK_PACKET_MAX_SIZE="50"
  INIT_PACKET_JUNK_SIZE="$(rand_between 12 149)"
  TRANSPORT_PACKET_JUNK_SIZE="12"
  RESPONSE_PACKET_JUNK_SIZE="$(rand_between 12 149)"
  while ((RESPONSE_PACKET_JUNK_SIZE == INIT_PACKET_JUNK_SIZE \
      || RESPONSE_PACKET_JUNK_SIZE == TRANSPORT_PACKET_JUNK_SIZE \
      || 148 + INIT_PACKET_JUNK_SIZE == 92 + RESPONSE_PACKET_JUNK_SIZE)); do
    RESPONSE_PACKET_JUNK_SIZE="$(rand_between 12 149)"
  done
  COOKIE_REPLY_PACKET_JUNK_SIZE="$(rand_between 12 63)"
  while ((COOKIE_REPLY_PACKET_JUNK_SIZE == INIT_PACKET_JUNK_SIZE \
      || COOKIE_REPLY_PACKET_JUNK_SIZE == RESPONSE_PACKET_JUNK_SIZE \
      || COOKIE_REPLY_PACKET_JUNK_SIZE == TRANSPORT_PACKET_JUNK_SIZE \
      || 64 + COOKIE_REPLY_PACKET_JUNK_SIZE == 148 + INIT_PACKET_JUNK_SIZE \
      || 64 + COOKIE_REPLY_PACKET_JUNK_SIZE == 92 + RESPONSE_PACKET_JUNK_SIZE)); do
    COOKIE_REPLY_PACKET_JUNK_SIZE="$(rand_between 12 63)"
  done
  INIT_PACKET_MAGIC_HEADER="1"
  RESPONSE_PACKET_MAGIC_HEADER="2"
  UNDERLOAD_PACKET_MAGIC_HEADER="3"
  TRANSPORT_PACKET_MAGIC_HEADER="4"
  CONTENT_PADDING_ADDITION="10-100"
  REKEY_AFTER_TIME="100-120"
  REKEY_TIMEOUT="3-7"
  REJECT_AFTER_TIME="150-180"
  KEEPALIVE_TIMEOUT="5-15"
  MAX_HANDSHAKE_ATTEMPTS="15-20"
}

wait_for_awg() {
  local attempt
  for attempt in {1..30}; do
    if docker exec "$CONTAINER_NAME" awg show awg0 >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

APP_SERVICE_PAUSED=false

pause_app_service() {
  if systemctl is-active --quiet amnezia-service 2>/dev/null; then
    log "Pausing the control plane while traffic limits are synchronized"
    systemctl stop amnezia-service
    APP_SERVICE_PAUSED=true
  fi
}

resume_app_service() {
  if [[ "$APP_SERVICE_PAUSED" == true ]]; then
    systemctl start amnezia-service || warn "Could not resume amnezia-service"
    APP_SERVICE_PAUSED=false
  fi
}

trap resume_app_service EXIT

report_awg_failure() {
  warn "AWG3 container state:"
  docker inspect --format \
    'status={{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}} restarts={{.RestartCount}}' \
    "$CONTAINER_NAME" >&2 || true
  warn "Last AWG3 container log lines:"
  docker logs --tail 200 "$CONTAINER_NAME" >&2 || true
}

verify_awg3() {
  local config parameter
  config="$(docker exec "$CONTAINER_NAME" awg showconf awg0)" || return 1
  for parameter in HeaderProtectionKey ContentPaddingAddition RekeyAfterTime \
    RekeyTimeout RejectAfterTime KeepaliveTimeout MaxHandshakeAttempts; do
    grep -Eq "^[[:space:]]*${parameter}[[:space:]]*=" <<< "$config" || return 1
  done
}

install_awg_traffic_control() {
  log "Configuring per-device AWG3 limits (${AWG_DOWNLOAD_LIMIT_MBIT} Mbit/s down, ${AWG_UPLOAD_LIMIT_MBIT} Mbit/s up)"
  install -d -m 0755 "$GENERATED_DIR"
  if ! docker exec "$CONTAINER_NAME" sh -lc 'command -v tc >/dev/null 2>&1'; then
    docker exec "$CONTAINER_NAME" apk add --no-cache iproute2
  fi
  export AWG_SUBNET_IP AWG_SUBNET_CIDR AWG_DOWNLOAD_LIMIT_MBIT AWG_UPLOAD_LIMIT_MBIT
  export WIREGUARD_SUBNET_CIDR="$AWG_SUBNET_CIDR"
  envsubst '${AWG_SUBNET_IP} ${WIREGUARD_SUBNET_CIDR} ${AWG_DOWNLOAD_LIMIT_MBIT} ${AWG_UPLOAD_LIMIT_MBIT}' \
    < "$SCRIPT_DIR/awg2-start.sh" \
    > "$GENERATED_DIR/start.sh"
  chmod 0755 "$GENERATED_DIR/start.sh"
  install -m 0755 "$SCRIPT_DIR/awg2-traffic-limit.sh" \
    "$GENERATED_DIR/traffic-limit.sh"
  docker cp "$GENERATED_DIR/traffic-limit.sh" \
    "$CONTAINER_NAME:/opt/amnezia/traffic-limit.sh"
  docker cp "$GENERATED_DIR/start.sh" "$CONTAINER_NAME:/opt/amnezia/start.sh"
  docker exec "$CONTAINER_NAME" chmod 0755 \
    /opt/amnezia/traffic-limit.sh /opt/amnezia/start.sh
}

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  log "Reusing the existing AWG3 container without replacing its peers"
  docker start "$CONTAINER_NAME" >/dev/null || true
  if ! wait_for_awg; then
    report_awg_failure
    die "Existing $CONTAINER_NAME does not expose a working awg0 interface."
  fi
  verify_awg3 || die "The existing container is not AWG3. Re-run once with --reset-for-awg3 to delete all accounts and keys and rebuild it."
  detected_port="$(docker exec "$CONTAINER_NAME" awg show awg0 listen-port | tr -d '\r\n')"
  if [[ "$detected_port" =~ ^[0-9]+$ && "$detected_port" != "$AWG_PORT" ]]; then
    warn "Existing AWG3 listens on UDP $detected_port; using it instead of $AWG_PORT."
    AWG_PORT="$detected_port"
  fi
  pause_app_service
  install_awg_traffic_control
  docker exec "$CONTAINER_NAME" /opt/amnezia/traffic-limit.sh sync \
    awg0 "$AWG_SUBNET_IP/$AWG_SUBNET_CIDR" \
    "$AWG_DOWNLOAD_LIMIT_MBIT" "$AWG_UPLOAD_LIMIT_MBIT"
  resume_app_service
else
  log "Building AWG3 with pinned official AmneziaVPN server scripts and image $AWG_IMAGE"
  generate_awg_parameters
  install -d -m 0755 "$AWG_BUILD_DIR" "$GENERATED_DIR"
  VENDORED_BASE_IMAGE="$(head -n 1 "$VENDOR_DIR/awg2/Dockerfile" | tr -d '\r')"
  [[ "$VENDORED_BASE_IMAGE" == "FROM amneziavpn/amneziawg-go:latest" ]] || \
    die "Unexpected base image in the vendored AWG Dockerfile."
  {
    printf 'FROM %s\n' "$AWG_IMAGE"
    tail -n +2 "$VENDOR_DIR/awg2/Dockerfile"
  } > "$AWG_BUILD_DIR/Dockerfile"
  chmod 0644 "$AWG_BUILD_DIR/Dockerfile"

  DOCKERFILE_FOLDER="$AWG_BUILD_DIR"
  AWG_SERVER_PORT="$AWG_PORT"
  export CONTAINER_NAME DOCKERFILE_FOLDER AWG_SERVER_PORT
  bash "$VENDOR_DIR/prepare_host.sh"
  bash "$VENDOR_DIR/build_container.sh"
  if ! docker run --rm --entrypoint /bin/sh "$CONTAINER_NAME" -ec \
      "grep -a -q HeaderProtectionKey /usr/bin/awg && \
       grep -a -q header_protection_key /usr/bin/amneziawg-go"; then
    die "The pinned image does not contain matching AWG3 backend and tools binaries."
  fi
  bash "$VENDOR_DIR/awg2/run_container.sh"
  HEADER_PROTECTION_KEY="$(docker exec "$CONTAINER_NAME" awg genkey | tr -d '\r\n')"
  [[ -n "$HEADER_PROTECTION_KEY" ]] || die "Cannot generate the AWG3 header protection key."

  export AWG_SUBNET_IP AWG_SUBNET_CIDR AWG_PORT
  export WIREGUARD_SUBNET_CIDR="$AWG_SUBNET_CIDR"
  export AWG_SERVER_PORT="$AWG_PORT"
  export JUNK_PACKET_COUNT JUNK_PACKET_MIN_SIZE JUNK_PACKET_MAX_SIZE
  export INIT_PACKET_JUNK_SIZE RESPONSE_PACKET_JUNK_SIZE
  export COOKIE_REPLY_PACKET_JUNK_SIZE TRANSPORT_PACKET_JUNK_SIZE
  export INIT_PACKET_MAGIC_HEADER RESPONSE_PACKET_MAGIC_HEADER
  export UNDERLOAD_PACKET_MAGIC_HEADER TRANSPORT_PACKET_MAGIC_HEADER
  export HEADER_PROTECTION_KEY CONTENT_PADDING_ADDITION REKEY_AFTER_TIME
  export REKEY_TIMEOUT REJECT_AFTER_TIME KEEPALIVE_TIMEOUT MAX_HANDSHAKE_ATTEMPTS

  envsubst '${AWG_SUBNET_IP} ${WIREGUARD_SUBNET_CIDR} ${AWG_SERVER_PORT} ${JUNK_PACKET_COUNT} ${JUNK_PACKET_MIN_SIZE} ${JUNK_PACKET_MAX_SIZE} ${INIT_PACKET_JUNK_SIZE} ${RESPONSE_PACKET_JUNK_SIZE} ${COOKIE_REPLY_PACKET_JUNK_SIZE} ${TRANSPORT_PACKET_JUNK_SIZE} ${INIT_PACKET_MAGIC_HEADER} ${RESPONSE_PACKET_MAGIC_HEADER} ${UNDERLOAD_PACKET_MAGIC_HEADER} ${TRANSPORT_PACKET_MAGIC_HEADER} ${HEADER_PROTECTION_KEY} ${CONTENT_PADDING_ADDITION} ${REKEY_AFTER_TIME} ${REKEY_TIMEOUT} ${REJECT_AFTER_TIME} ${KEEPALIVE_TIMEOUT} ${MAX_HANDSHAKE_ATTEMPTS}' \
    < "$VENDOR_DIR/awg2/configure_container.sh" \
    > "$GENERATED_DIR/configure_container.sh"
  chmod 0755 "$GENERATED_DIR/configure_container.sh"

  docker cp "$GENERATED_DIR/configure_container.sh" \
    "$CONTAINER_NAME:/opt/amnezia/configure_container.sh"
  docker exec "$CONTAINER_NAME" bash /opt/amnezia/configure_container.sh
  docker exec "$CONTAINER_NAME" chmod 0600 \
    /opt/amnezia/awg/awg0.conf \
    /opt/amnezia/awg/wireguard_server_private_key.key \
    /opt/amnezia/awg/wireguard_psk.key
  install_awg_traffic_control
  docker restart "$CONTAINER_NAME" >/dev/null
  if ! wait_for_awg; then
    report_awg_failure
    die "AWG3 failed to start; see the container diagnostics above."
  fi
  verify_awg3 || die "AWG3 parameters are missing after startup; inspect: docker logs $CONTAINER_NAME"
fi

DOCKER_BIN="$(command -v docker)"
AWG_BIN="$(docker exec "$CONTAINER_NAME" sh -lc 'command -v awg' | tr -d '\r\n')"
AWG_QUICK_BIN="$(docker exec "$CONTAINER_NAME" sh -lc 'command -v awg-quick' | tr -d '\r\n')"
[[ -n "$DOCKER_BIN" && -n "$AWG_BIN" && -n "$AWG_QUICK_BIN" ]] || \
  die "Cannot determine Docker/AWG3 binary paths."

read_live_interface_value() {
  local name="$1"
  docker exec "$CONTAINER_NAME" awg showconf awg0 \
    | sed -n "s/^[[:space:]]*${name}[[:space:]]*=[[:space:]]*//p" \
    | head -n 1
}
AWG_I1_VALUE="$(read_live_interface_value I1)"
AWG_I2_VALUE="$(read_live_interface_value I2)"
AWG_I3_VALUE="$(read_live_interface_value I3)"
AWG_I4_VALUE="$(read_live_interface_value I4)"
AWG_I5_VALUE="$(read_live_interface_value I5)"

log "Preparing PostgreSQL and application secrets"
DB_PASSWORD="$(read_env_value DEPLOY_DB_PASSWORD)"
SECRET_KEY="$(read_env_value SECRET_KEY)"
ENCRYPTION_KEY="$(read_env_value ENCRYPTION_KEY)"
if [[ "$RESET_FOR_AWG3" == true ]]; then
  # Invalidate every pre-reset browser session and encryption context.
  SECRET_KEY=""
  ENCRYPTION_KEY=""
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
  ADMIN_PASSWORD="$(read_env_value ADMIN_PASSWORD)"
fi
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 24)}"
SECRET_KEY="${SECRET_KEY:-$(openssl rand -hex 32)}"
ENCRYPTION_KEY="${ENCRYPTION_KEY:-$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)}"
PAYMENT_PROVIDER_VALUE="$(read_env_value PAYMENT_PROVIDER)"
YOOKASSA_SHOP_ID_VALUE="$(read_env_value YOOKASSA_SHOP_ID)"
YOOKASSA_SECRET_KEY_VALUE="$(read_env_value YOOKASSA_SECRET_KEY)"
PAYMENT_PROVIDER_VALUE="${PAYMENT_PROVIDER_VALUE:-mock}"
if [[ "$PAYMENT_PROVIDER_VALUE" == "yookassa" ]] && \
   [[ -z "$YOOKASSA_SHOP_ID_VALUE" || -z "$YOOKASSA_SECRET_KEY_VALUE" ]]; then
  die "Existing YooKassa configuration is incomplete in $ENV_FILE."
fi

if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='amnezia'" | grep -q 1; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
    -c "CREATE ROLE amnezia LOGIN PASSWORD '$DB_PASSWORD'"
else
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
    -c "ALTER ROLE amnezia WITH LOGIN PASSWORD '$DB_PASSWORD'"
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='amnezia'" | grep -q 1; then
  runuser -u postgres -- createdb --owner=amnezia amnezia
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR" "$APP_DIR/app"
cp -a "$PROJECT_DIR/app/." "$APP_DIR/app/"
install -o "$APP_USER" -g "$APP_USER" -m 0644 "$PROJECT_DIR/pyproject.toml" "$APP_DIR/pyproject.toml"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/app"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/.venv"
fi
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" -m pip install "$APP_DIR"

log "Writing production configuration and least-privilege service permissions"
cat > "$ENV_FILE.new" <<EOF
APP_NAME=Amnezia Service
ENVIRONMENT=production
BASE_URL=https://$DOMAIN
SECRET_KEY=$SECRET_KEY
ENCRYPTION_KEY=$ENCRYPTION_KEY
DATABASE_URL=postgresql+psycopg://amnezia:$DB_PASSWORD@127.0.0.1:5432/amnezia
DEPLOY_DB_PASSWORD=$DB_PASSWORD
TRUSTED_HOSTS=["$DOMAIN","localhost","127.0.0.1"]
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASSWORD
SESSION_HTTPS_ONLY=true
PAYMENT_PROVIDER=$PAYMENT_PROVIDER_VALUE
YOOKASSA_SHOP_ID=$YOOKASSA_SHOP_ID_VALUE
YOOKASSA_SECRET_KEY=$YOOKASSA_SECRET_KEY_VALUE
VPN_BACKEND=native
AWG_INTERFACE=awg0
AWG_ENDPOINT=$DOMAIN:$AWG_PORT
AWG_SUBNET=$AWG_SUBNET_IP/$AWG_SUBNET_CIDR
AWG_DNS=1.1.1.1,1.0.0.1
AWG_COMMAND_PREFIX=["sudo","-n","$DOCKER_BIN","exec","-i","$CONTAINER_NAME"]
AWG_BINARY=$AWG_BIN
AWG_QUICK_BINARY=$AWG_QUICK_BIN
AWG_CONFIG_PATH=/opt/amnezia/awg/awg0.conf
AWG_SAVE_CONFIG=true
AWG_RATE_LIMIT_ENABLED=true
AWG_RATE_LIMIT_BINARY=/opt/amnezia/traffic-limit.sh
AWG_DOWNLOAD_LIMIT_MBPS=$AWG_DOWNLOAD_LIMIT_MBIT
AWG_UPLOAD_LIMIT_MBPS=$AWG_UPLOAD_LIMIT_MBIT
AWG_I1=$AWG_I1_VALUE
AWG_I2=$AWG_I2_VALUE
AWG_I3=$AWG_I3_VALUE
AWG_I4=$AWG_I4_VALUE
AWG_I5=$AWG_I5_VALUE
SUBSCRIPTION_RECONCILE_SECONDS=60
MAX_DEVICES_PER_SUBSCRIPTION=20
EOF
install -o root -g "$APP_USER" -m 0640 "$ENV_FILE.new" "$ENV_FILE"
rm -f "$ENV_FILE.new"

cat > /etc/sudoers.d/amnezia-service <<EOF
Cmnd_Alias AMNEZIA_SERVICE_PEERS = $DOCKER_BIN exec -i $CONTAINER_NAME $AWG_BIN *, \\
                                    $DOCKER_BIN exec -i $CONTAINER_NAME $AWG_QUICK_BIN save /opt/amnezia/awg/awg0.conf, \\
                                    $DOCKER_BIN exec -i $CONTAINER_NAME /opt/amnezia/traffic-limit.sh *
$APP_USER ALL=(root) NOPASSWD: AMNEZIA_SERVICE_PEERS
EOF
chmod 0440 /etc/sudoers.d/amnezia-service
visudo -cf /etc/sudoers.d/amnezia-service >/dev/null

install -o root -g root -m 0644 "$SCRIPT_DIR/amnezia-service.service" \
  /etc/systemd/system/amnezia-service.service
systemctl daemon-reload
systemctl enable --now amnezia-service
systemctl restart amnezia-service

log "Configuring Caddy HTTPS reverse proxy"
install -d -o root -g caddy -m 0750 /etc/caddy/sites-enabled
if [[ "$HOST_IS_IP" == true ]]; then
  if [[ "$IP_TLS_MODE" == "public" ]]; then
    CADDY_TLS_DIRECTIVE=$'    tls {\n        issuer acme https://acme-v02.api.letsencrypt.org/directory {\n            profile shortlived\n        }\n    }'
  else
    CADDY_TLS_DIRECTIVE="    tls internal"
  fi
else
  CADDY_TLS_DIRECTIVE=""
fi
cat > /etc/caddy/sites-enabled/amnezia-service.caddy <<EOF
$DOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
$CADDY_TLS_DIRECTIVE
}
EOF
chmod 0644 /etc/caddy/sites-enabled/amnezia-service.caddy
if ! grep -Fq 'import /etc/caddy/sites-enabled/*.caddy' /etc/caddy/Caddyfile; then
  cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.before-amnezia.$(date +%Y%m%d%H%M%S)"
  printf '\nimport /etc/caddy/sites-enabled/*.caddy\n' >> /etc/caddy/Caddyfile
fi
caddy fmt --overwrite /etc/caddy/sites-enabled/amnezia-service.caddy
caddy validate --config /etc/caddy/Caddyfile

CADDY_SERVICE_ACTION="reload"
if [[ "$HOST_IS_IP" == true && "$IP_TLS_MODE" == "public" ]]; then
  CADDY_LOCAL_CERT_BASE="$CADDY_DATA_DIR/certificates/local"
  CADDY_LOCAL_CERT_DIR="$CADDY_LOCAL_CERT_BASE/$DOMAIN"
  if [[ -d "$CADDY_LOCAL_CERT_DIR" ]]; then
    CADDY_LOCAL_CERT_BASE_REAL="$(readlink -f -- "$CADDY_LOCAL_CERT_BASE")"
    CADDY_LOCAL_CERT_DIR_REAL="$(readlink -f -- "$CADDY_LOCAL_CERT_DIR")"
    [[ "$CADDY_LOCAL_CERT_DIR_REAL" == "$CADDY_LOCAL_CERT_BASE_REAL/$DOMAIN" ]] || \
      die "Refusing to move an unexpected Caddy certificate path: $CADDY_LOCAL_CERT_DIR_REAL"

    CADDY_MIGRATION_DIR="$CADDY_DATA_DIR/migrated-local-certificates"
    CADDY_MIGRATION_TARGET="$CADDY_MIGRATION_DIR/$DOMAIN.$(date +%Y%m%d%H%M%S)"
    install -d -o caddy -g caddy -m 0700 "$CADDY_MIGRATION_DIR"
    mv -- "$CADDY_LOCAL_CERT_DIR_REAL" "$CADDY_MIGRATION_TARGET"
    chown -R caddy:caddy "$CADDY_MIGRATION_TARGET"
    log "Archived the previous local IP certificate at $CADDY_MIGRATION_TARGET"
    CADDY_SERVICE_ACTION="restart"
  fi
fi

systemctl enable --now caddy
systemctl "$CADDY_SERVICE_ACTION" caddy

if ufw status 2>/dev/null | grep -q '^Status: active'; then
  log "Updating the active UFW policy"
  SSH_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')"
  ufw allow "${SSH_PORT:-22}/tcp" comment 'SSH' >/dev/null
  ufw allow 80/tcp comment 'Caddy HTTP' >/dev/null
  ufw allow 443/tcp comment 'Caddy HTTPS' >/dev/null
  ufw allow "$AWG_PORT/udp" comment 'AmneziaWG3' >/dev/null
else
  warn "UFW is inactive; it was not enabled automatically. Open TCP 80/443 and UDP $AWG_PORT in the VPS firewall/security group."
fi

for attempt in {1..30}; do
  if curl --fail --silent --show-error \
    --header "Host: $DOMAIN" \
    http://127.0.0.1:8000/healthz >/dev/null; then
    break
  fi
  if ((attempt == 30)); then
    journalctl -u amnezia-service --no-pager -n 80 >&2 || true
    die "The control panel did not pass its local health check."
  fi
  sleep 1
done

if [[ "$HOST_IS_IP" == true && "$IP_TLS_MODE" == "public" ]]; then
  log "Waiting for the publicly trusted IP certificate"
  PUBLIC_TLS_READY=false
  for attempt in {1..30}; do
    if curl --fail --silent \
      --connect-to "$DOMAIN:443:127.0.0.1:443" \
      "https://$DOMAIN/healthz" >/dev/null 2>&1; then
      PUBLIC_TLS_READY=true
      break
    fi
    sleep 2
  done
  if [[ "$PUBLIC_TLS_READY" == true ]]; then
    log "The publicly trusted IP certificate is active"
  else
    warn "The public IP certificate is not active yet. Inspect: journalctl -u caddy -n 100 --no-pager"
  fi
fi

cat > "$CREDENTIALS_FILE" <<EOF
URL: https://$DOMAIN/admin
Admin email: $ADMIN_EMAIL
Admin password: $ADMIN_PASSWORD
AWG3 endpoint: $DOMAIN:$AWG_PORT/udp
Environment: $ENV_FILE
EOF
if [[ "$HOST_IS_IP" == true && "$IP_TLS_MODE" == "internal" ]]; then
  cat >> "$CREDENTIALS_FILE" <<EOF
Caddy local CA: /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
EOF
fi
if [[ "$HOST_IS_IP" == true && "$IP_TLS_MODE" == "public" ]]; then
  cat >> "$CREDENTIALS_FILE" <<EOF
TLS certificate: Let's Encrypt short-lived IP certificate (automatic renewal)
EOF
fi
chmod 0600 "$CREDENTIALS_FILE"

if [[ "$HOST_IS_IP" == false ]] && ! getent ahostsv4 "$DOMAIN" >/dev/null 2>&1; then
  warn "$DOMAIN does not resolve to IPv4 yet. Caddy will issue HTTPS after DNS and TCP 80/443 are reachable."
fi
if [[ "$HOST_IS_IP" == true && "$IP_TLS_MODE" == "internal" ]]; then
  warn "HTTPS uses Caddy's local CA because no domain was supplied. Import its root.crt on administrator devices to remove the browser certificate warning."
fi
if [[ "$HOST_IS_IP" == true && "$IP_TLS_MODE" == "public" ]]; then
  warn "Caddy is obtaining a publicly trusted Let's Encrypt IP certificate. TCP 80/443 must be reachable from the Internet; Caddy will retry and renew it automatically."
fi

log "Deployment completed"
printf 'Admin:       https://%s/admin\n' "$DOMAIN"
printf 'Credentials: %s (root-only)\n' "$CREDENTIALS_FILE"
printf 'AWG3:        %s:%s/udp\n' "$DOMAIN" "$AWG_PORT"
printf 'Status:      systemctl status amnezia-service caddy\n'
