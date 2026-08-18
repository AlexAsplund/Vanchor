"""Flash helm-controller firmware (UF2) onto a USB-connected Pico.

The vanchor-pcb helm firmware ships as UF2 release assets
(``vanchor-helm-pico.uf2`` / ``vanchor-helm-pico2.uf2``). The phone downloads
one with its own internet and uploads it here; this module drives ``picotool``
to do the actual flashing:

    picotool load -f -x <file.uf2>

``-f`` reboots a RUNNING firmware into the BOOTSEL bootloader over the USB
stdio reset interface (the helm firmware exposes it), so no button press is
needed; ``-x`` reboots into the freshly flashed application afterwards. The
board's EN pulldowns keep both motor bridges disabled for the whole window.

picotool is baked into the container image (built from source -- Debian
bookworm has no package) and needs USB access: the compose contract carries a
``c 189:*`` device-cgroup rule for /dev/bus/usb. Older installs that were
bundle-updated (container recreated from an old compose/containers.json)
lack that rule -- picotool then reports no accessible devices; the endpoint
surfaces that as a reflash hint rather than a mystery failure.

BENCH-VERIFY: full flash cycle against a real Pico 2 on the boat Pi
(reset-interface reboot with the CDC port held open by the motor driver,
cgroup rule on a fresh image, re-enumeration afterwards).
"""
from __future__ import annotations

import shutil
import subprocess

# UF2 block layout: 512-byte blocks, magics at offsets 0/4 of every block.
_UF2_MAGIC0 = b"UF2\n"                      # 0x0A324655 little-endian
_UF2_MAGIC1 = (0x9E5D5157).to_bytes(4, "little")
MAX_UF2_BYTES = 8 * 1024 * 1024             # helm UF2s are ~0.5 MB; 8 MB cap

_FLASH_TIMEOUT_S = 120.0


def picotool_path() -> str | None:
    """Absolute path of the picotool binary, or None when not installed."""
    return shutil.which("picotool")


def validate_uf2(data: bytes) -> str | None:
    """Reject anything that is not a plausible UF2 image. None when OK.

    The firmware itself is the real validator (a wrong-chip UF2 is refused by
    the bootloader); this only stops obvious mistakes (HTML error pages,
    bundle.tar uploads) before they reach the flash step.
    """
    if len(data) == 0:
        return "empty file"
    if len(data) > MAX_UF2_BYTES:
        return "file too large for a firmware image"
    if len(data) < 512 or len(data) % 512 != 0:
        return "not a UF2 image (size is not a multiple of 512)"
    if data[0:4] != _UF2_MAGIC0 or data[4:8] != _UF2_MAGIC1:
        return "not a UF2 image (bad magic)"
    return None


def flash(path: str, run=subprocess.run) -> tuple[bool, str]:
    """Flash the UF2 at ``path``; (ok, human-readable output).

    ``run`` is a test seam with the subprocess.run signature.
    """
    tool = picotool_path()
    if tool is None:
        return False, ("picotool is not installed in this image -- update the "
                       "boat to the latest SD image to get the flasher.")
    try:
        proc = run(
            [tool, "load", "-f", "-x", str(path)],
            capture_output=True, text=True, timeout=_FLASH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, "picotool timed out -- unplug/replug the Pico and retry"
    except OSError as exc:
        return False, f"could not run picotool: {exc}"
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        if "No accessible" in out or "no accessible" in out:
            out += ("\nNo Pico visible over USB. Check the cable, or if this "
                    "boat was updated by bundle (not reflashed) the container "
                    "may lack USB access -- reflash the SD card once.")
        return False, out
    return True, out
