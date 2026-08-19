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
scripts. The `RandomTrailers` and `DisableCookies` additions in
`awg2/configure_container.sh` are synchronized with upstream commit
`b1a37b3779644b9daad1179d40a74f2231beec99`. The wrapper pins the official
image to `amneziavpn/amneziawg-go:3.1.20260812` and its OCI index digest
`sha256:c60cc651df4a2315d67dcd5411203fa1eb1beb4cb493aa6326cfaf8359d00434`,
so a future upstream change cannot silently alter a server deployment. The
bootstrap also checks that both `awg` and `amneziawg-go` contain their AWG 3.1
configuration symbols before creating the VPN container.

`awg2/run_container.sh` has one operational hardening change: upstream's
`--log-driver none` is replaced with Docker's rotating `local` driver (10 MB,
three files), so a failed VPN startup remains diagnosable with `docker logs`.
