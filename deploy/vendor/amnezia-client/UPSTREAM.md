# Vendored AmneziaVPN server scripts

The files in this directory are copied without modification from
[`amnezia-vpn/amnezia-client`](https://github.com/amnezia-vpn/amnezia-client) at
commit `06d219b92bfa7e7e8c43cca6e72e354d304b42a7`:

- `client/server_scripts/containers/awg2/*`
- `client/server_scripts/build_container.sh`
- `client/server_scripts/prepare_host.sh`
- `LICENSE`

They are licensed by their upstream authors under GPL-3.0; see `LICENSE`. The
project-owned `deploy/vps-bootstrap.sh` wrapper renders and runs these pinned
scripts so a future upstream change cannot silently alter a server deployment.
