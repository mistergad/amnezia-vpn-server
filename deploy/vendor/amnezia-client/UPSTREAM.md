# Vendored AmneziaVPN server scripts

The files in this directory originate from
[`amnezia-vpn/amnezia-client`](https://github.com/amnezia-vpn/amnezia-client) at
commit `dcf53b989e684a2e3e3f7f5c090001fb2def73b9`:

- `client/server_scripts/awg/*` (stored locally as `awg2` because that is the
  container identifier retained by AmneziaVPN for the userspace AWG backend)
- `client/server_scripts/build_container.sh`
- `client/server_scripts/prepare_host.sh`
- `LICENSE`

They are licensed by their upstream authors under GPL-3.0; see `LICENSE`. The
project-owned `deploy/vps-bootstrap.sh` wrapper renders and runs these pinned
scripts and pins the upstream image to `amneziavpn/amneziawg-go:3.0.20260805`
and its OCI index digest
`sha256:8447c91637c37536dd99b8bbd4420c819ac9f330f047804197291625bfb0ea8a`,
so a future upstream change cannot silently alter a server deployment. The
bootstrap also checks that both `awg` and `amneziawg-go` contain their AWG3
configuration symbols before creating the VPN container.

`awg2/run_container.sh` has one operational hardening change: upstream's
`--log-driver none` is replaced with Docker's rotating `local` driver (10 MB,
three files), so a failed VPN startup remains diagnosable with `docker logs`.
