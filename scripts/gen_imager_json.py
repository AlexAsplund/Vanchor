#!/usr/bin/env python3
"""Generate a Raspberry Pi Imager custom-repository JSON (os_list.json).

Usage:
    gen_imager_json.py --version 1.5.0a8 --img path/to/vanchor-1.5.0a8-arm64.img.xz
        --extract-size N --extract-sha256 HASH [--out os_list.json] [--date YYYY-MM-DD]

The --extract-size (bytes of the uncompressed .img) and --extract-sha256
(sha256 of the uncompressed .img) must be supplied by the caller, because
materialising a 4 GB img just for hashing is expensive — CI computes them
via: size = xz --robot --list; sha256 = xzcat | sha256sum (streaming).

The download size and download sha256 are computed from the .img.xz file itself.

os_list format: https://github.com/raspberrypi/rpi-imager (custom-repo section
in the README). Field names verified against rpi-imager source 2026-07-18.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys


GITHUB_OWNER = "AlexAsplund"
GITHUB_REPO = "vanchor-ng"


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_os_list(
    version: str,
    img_xz: pathlib.Path,
    extract_size: int,
    extract_sha256: str,
    release_date: str,
) -> dict:
    download_size = img_xz.stat().st_size
    download_sha256 = sha256_of(img_xz)

    url = (
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/releases/download/v{version}/vanchor-{version}-arm64.img.xz"
    )

    return {
        "os_list": [
            {
                "name": f"Vanchor-NG {version}",
                "description": (
                    "Trolling-motor autopilot boat controller. "
                    "Boots a setup hotspot 'vanchor-setup' (password vanchor-boat); "
                    "UI at http://vanchor.local:8000."
                ),
                "url": url,
                "extract_size": extract_size,
                "extract_sha256": extract_sha256,
                "image_download_size": download_size,
                "image_download_sha256": download_sha256,
                "release_date": release_date,
                "init_format": "systemd",
                "devices": ["pi5-64bit", "pi4-64bit", "pi3-64bit"],
            }
        ]
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True, help="App version, e.g. 1.5.0a8")
    p.add_argument("--img", required=True, type=pathlib.Path, help="Path to the .img.xz file")
    p.add_argument("--extract-size", required=True, type=int,
                   help="Byte size of the uncompressed .img")
    p.add_argument("--extract-sha256", required=True,
                   help="SHA-256 of the uncompressed .img (hex)")
    p.add_argument("--out", default="os_list.json", type=pathlib.Path,
                   help="Output path (default: os_list.json)")
    p.add_argument("--date", default=None,
                   help="Release date YYYY-MM-DD (default: today)")
    args = p.parse_args(argv)

    img = pathlib.Path(args.img)
    if not img.exists():
        print(f"ERROR: img not found: {img}", file=sys.stderr)
        return 1

    release_date = args.date or datetime.date.today().isoformat()

    data = build_os_list(
        version=args.version,
        img_xz=img,
        extract_size=args.extract_size,
        extract_sha256=args.extract_sha256,
        release_date=release_date,
    )

    out = pathlib.Path(args.out)
    out.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Written: {out} (download_sha256={data['os_list'][0]['image_download_sha256'][:12]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
