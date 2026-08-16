from __future__ import annotations


def ensure_client_mtu(config: str, mtu: int) -> str:
    """Return an AWG client config with the requested Interface MTU."""
    if not 576 <= mtu <= 9000:
        raise ValueError("Client MTU must be between 576 and 9000")

    newline = "\r\n" if "\r\n" in config else "\n"
    lines = config.splitlines()
    interface_start: int | None = None
    interface_end = len(lines)

    for index, line in enumerate(lines):
        section = line.strip().casefold()
        if section == "[interface]":
            interface_start = index
            continue
        if interface_start is not None and section.startswith("[") and section.endswith("]"):
            interface_end = index
            break

    if interface_start is None:
        raise ValueError("AWG client config has no Interface section")

    for index in range(interface_start + 1, interface_end):
        key, separator, _ = lines[index].partition("=")
        if separator and key.strip().casefold() == "mtu":
            lines[index] = f"MTU = {mtu}"
            break
    else:
        lines.insert(interface_start + 1, f"MTU = {mtu}")

    rendered = newline.join(lines)
    if config.endswith(("\n", "\r")):
        rendered += newline
    return rendered
