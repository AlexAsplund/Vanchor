#!/usr/bin/env python3
"""Record the getting-started guide's screen clips against a live simulation.

Same isolation approach as scripts/take_screenshots.py: boots a throwaway
vanchor server (temp data dir, sim, charted-lake start), drives the UI through
the real command API + Playwright, records each clip as webm and converts to a
small H.264 mp4 (imageio-ffmpeg's bundled binary — no system ffmpeg needed).

Usage:
    .venv/bin/python scripts/record_guide.py            # all clips
    .venv/bin/python scripts/record_guide.py anchor     # subset by name

Clips land in docs/media/ as <name>.mp4 (target: <2 MB each, 1280x800).
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the screenshot rig's server plumbing (boot_server, cmd, state, LAKE).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from take_screenshots import (  # noqa: E402
    LAKE, boot_server, cmd, wait_app,
)

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "media"

BASE_URL = ""


def convert(webm: str, mp4: Path) -> None:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ff, "-y", "-i", webm, "-c:v", "libx264", "-preset", "slow", "-crf", "28",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
        capture_output=True, check=True,
    )


class Recorder:
    """One browser per clip so each webm starts exactly at the clip's start."""

    def __init__(self, pw, base: str) -> None:
        self.pw = pw
        self.base = base

    def clip(self, name: str, fn) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="vanchor-rec-") as vdir:
            browser = self.pw.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                record_video_dir=vdir,
                record_video_size={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.goto(self.base, wait_until="domcontentloaded")
            wait_app(page)
            fn(page, self.base)
            ctx.close()
            browser.close()
            webm = glob.glob(vdir + "/*.webm")[0]
            mp4 = OUT / f"{name}.mp4"
            convert(webm, mp4)
            print(f"  -> {mp4.name} ({os.path.getsize(mp4) // 1024} KB)")


# --------------------------------------------------------------------------- #
# Clip choreographies are registered here by name. Each takes (page, base) with
# the page already loaded + connected, and should run its story in real time —
# whatever happens on screen is what the viewer sees.
# --------------------------------------------------------------------------- #
CLIPS: dict = {}


def clip(name: str):
    def reg(fn):
        CLIPS[name] = fn
        return fn
    return reg


def main() -> None:
    global BASE_URL
    only = set(sys.argv[1:])
    from playwright.sync_api import sync_playwright

    # Choreographies are defined in record_guide_clips.py (kept separate so the
    # rig and the story content evolve independently).
    import record_guide_clips  # noqa: F401  (registers via @clip)

    with tempfile.TemporaryDirectory(prefix="vanchor-guide-") as wd:
        proc, base = boot_server(Path(wd))
        BASE_URL = base
        try:
            with sync_playwright() as pw:
                rec = Recorder(pw, base)
                for name, fn in CLIPS.items():
                    if only and name not in only:
                        continue
                    print(f"[clip] {name}")
                    rec.clip(name, fn)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
