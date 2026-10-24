#!/usr/bin/env python3
"""Record a guided NextTime Dutch rehearsal walkthrough video with Playwright.

Outputs ``docs/media/nexttime-demo.webm`` covering:
1. Loading & confirming the Dutch standup calendar rehearsal candidate (shift_clarification).
2. Polly counterpart audio playback + turn-boundary haptic pulses (SHORT_DOUBLE / STEADY_BUZZ).
3. Simulated consented Dutch speech input with live streaming transcription updates & PCM deletion.
4. Post-rehearsal communication & pacing breakdown analysis.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
import playwright._impl._driver as pw_driver
import uvicorn
from nexttime_api import create_app
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_VIDEO = REPO_ROOT / "docs" / "media" / "nexttime-demo.webm"
WEB_APP_DIR = REPO_ROOT / "apps" / "web"
WEBM_EBML_MAGIC = b"\x1a\x45\xdf\xa3"

CONTAINER_CHROMIUM_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--single-process",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
)

GUIDED_OVERLAY_INIT_SCRIPT = """
(() => {
  window.__ntEnsureOverlay = () => {
    if (document.getElementById('nt-demo-caption')) return;
    const badge = document.createElement('div');
    badge.id = 'nt-demo-caption';
    Object.assign(badge.style, {
      position: 'fixed',
      right: '20px',
      bottom: '16px',
      zIndex: '99999',
      padding: '9px 14px',
      borderRadius: '10px',
      background: 'rgba(35, 51, 47, 0.94)',
      color: '#FFFFFF',
      fontFamily: '-apple-system, BlinkMacSystemFont, system-ui, sans-serif',
      fontSize: '13px',
      lineHeight: '1.35',
      fontWeight: '600',
      boxShadow: '0 8px 24px rgba(35, 51, 47, 0.22)',
      borderLeft: '4px solid #2F6255',
      maxWidth: '390px',
      pointerEvents: 'none',
    });
    badge.textContent = 'NextTime Guided Rehearsal Demo';
    document.body.appendChild(badge);

    const cursor = document.createElement('div');
    cursor.id = 'nt-demo-cursor';
    Object.assign(cursor.style, {
      position: 'fixed',
      left: '640px',
      top: '360px',
      width: '20px',
      height: '20px',
      marginLeft: '-10px',
      marginTop: '-10px',
      borderRadius: '50%',
      background: 'rgba(47, 98, 85, 0.34)',
      border: '2.5px solid #2F6255',
      zIndex: '100000',
      pointerEvents: 'none',
      transition: 'left 260ms ease, top 260ms ease, transform 140ms ease',
    });
    document.body.appendChild(cursor);
  };

  window.__ntSetCaption = (text) => {
    window.__ntEnsureOverlay();
    const el = document.getElementById('nt-demo-caption');
    if (el) el.textContent = text;
  };

  window.__ntMoveCursor = (x, y, pulse) => {
    window.__ntEnsureOverlay();
    const el = document.getElementById('nt-demo-cursor');
    if (!el) return;
    el.style.left = `${Math.round(x)}px`;
    el.style.top = `${Math.round(y)}px`;
    el.style.transform = pulse ? 'scale(0.76)' : 'scale(1)';
  };
})();
"""


@dataclass(frozen=True)
class RecorderConfig:
    output_path: Path
    viewport_width: int = 1280
    viewport_height: int = 800
    external_url: str | None = None


def find_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        addr = sock.getsockname()
        return int(addr[1])


def wait_for_http_ready(url: str, timeout_sec: float = 15.0) -> None:
    if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError(f"Refusing non-local readiness probe URL: {url}")
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:  # noqa: S310
                if 200 <= resp.status < 400:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for local server at {url}")


def ensure_playwright_ffmpeg_ready() -> Path:
    """Ensure Playwright's internal video recorder finds a working Linux ffmpeg binary."""
    _node_bin, cli_js = pw_driver.compute_driver_executable()
    browsers_manifest = json.loads(
        (Path(cli_js).parent / "browsers.json").read_text(encoding="utf-8")
    )
    ffmpeg_rev = "1013"
    for entry in browsers_manifest.get("browsers", []):
        if isinstance(entry, dict) and entry.get("name") == "ffmpeg":
            ffmpeg_rev = str(entry.get("revision", "1013"))
            break

    pw_browsers_dir = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/tmp/nexttime-pw-browsers")  # noqa: S108
    )
    target_dir = pw_browsers_dir / f"ffmpeg-{ffmpeg_rev}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_bin = target_dir / "ffmpeg-linux"
    if not target_bin.is_file():
        src_ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
        shutil.copy2(src_ffmpeg, target_bin)
        target_bin.chmod(0o755)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_browsers_dir)
    return target_bin


def ensure_portable_chromium_binary() -> tuple[Path | None, list[str]]:
    """Return an explicit Chromium path + flags when standard Playwright Chromium is absent."""
    cached_bin = Path("/tmp/chromium")  # noqa: S108
    cached_libs = Path("/tmp/al2023/lib")  # noqa: S108
    if cached_bin.is_file() and cached_libs.is_dir():
        existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{cached_libs}:/tmp:{existing_ld}"
        return cached_bin, list(CONTAINER_CHROMIUM_ARGS)

    npm_bin = shutil.which("npm")
    node_bin = shutil.which("node")
    tar_bin = shutil.which("tar")
    if npm_bin is None or node_bin is None or tar_bin is None:
        return None, []

    with tempfile.TemporaryDirectory(prefix="nexttime_chromium_") as tmp_str:
        tmp_dir = Path(tmp_str)
        subprocess.run(  # noqa: S603
            [npm_bin, "pack", "@sparticuz/chromium@123.0.1"],
            cwd=str(tmp_dir),
            check=True,
            capture_output=True,
        )
        tgz_files = list(tmp_dir.glob("sparticuz-chromium-*.tgz"))
        if not tgz_files:
            return None, []
        subprocess.run(  # noqa: S603
            [tar_bin, "-xzf", str(tgz_files[0])],
            cwd=str(tmp_dir),
            check=True,
            capture_output=True,
        )
        unpack_js = (
            "const fs = require('fs'), zlib = require('zlib');"
            "fs.writeFileSync('al2023.tar', "
            "zlib.brotliDecompressSync(fs.readFileSync('package/bin/al2023.tar.br')));"
            "fs.writeFileSync('/tmp/chromium', "
            "zlib.brotliDecompressSync(fs.readFileSync('package/bin/chromium.br')), "
            "{ mode: 0o755 });"
            "fs.writeFileSync('swiftshader.tar', "
            "zlib.brotliDecompressSync(fs.readFileSync('package/bin/swiftshader.tar.br')));"
        )
        subprocess.run(  # noqa: S603
            [node_bin, "-e", unpack_js],
            cwd=str(tmp_dir),
            check=True,
            capture_output=True,
        )
        al2023_root = Path("/tmp/al2023")  # noqa: S108
        al2023_root.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603
            [tar_bin, "-xf", str(tmp_dir / "al2023.tar"), "-C", str(al2023_root)],
            check=True,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603
            [tar_bin, "-xf", str(tmp_dir / "swiftshader.tar"), "-C", "/tmp"],  # noqa: S108
            check=True,
            capture_output=True,
        )

    existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{cached_libs}:/tmp:{existing_ld}"
    return cached_bin, list(CONTAINER_CHROMIUM_ARGS)


def launch_chromium_browser(pw: Playwright) -> Browser:
    default_exec = Path(pw.chromium.executable_path)
    if default_exec.is_file():
        return pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])

    portable_exec, extra_args = ensure_portable_chromium_binary()
    if portable_exec is not None and portable_exec.is_file():
        child_env: dict[str, str | float | bool] = {
            k: v for k, v in os.environ.items() if isinstance(k, str) and isinstance(v, str)
        }
        tmp_root = Path(tempfile.gettempdir())
        al2023_lib = tmp_root / "al2023" / "lib"
        child_env["LD_LIBRARY_PATH"] = (
            f"{al2023_lib}:{tmp_root}:{child_env.get('LD_LIBRARY_PATH', '')}"
        )
        return pw.chromium.launch(
            headless=True,
            executable_path=str(portable_exec),
            args=extra_args,
            env=child_env,
        )
    raise RuntimeError(
        "No Chromium executable available. Run 'uv run playwright install chromium' or ensure npm "
        "is installed for portable @sparticuz/chromium fallback."
    )


def ensure_web_bundle_built() -> None:
    dist_index = WEB_APP_DIR / "dist" / "index.html"
    if dist_index.is_file():
        return
    pnpm_bin = shutil.which("pnpm")
    if pnpm_bin is None:
        raise RuntimeError("apps/web/dist is missing and pnpm is not installed on PATH")
    subprocess.run(  # noqa: S603
        [pnpm_bin, "--dir", str(WEB_APP_DIR), "build"],
        check=True,
    )


def guided_click(page: Page, test_id: str, caption: str, pause_after_ms: int = 800) -> None:
    page.evaluate("(txt) => window.__ntSetCaption && window.__ntSetCaption(txt)", caption)
    locator = page.get_by_test_id(test_id)
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    if box is not None:
        cx = box["x"] + box["width"] / 2.0
        cy = box["y"] + box["height"] / 2.0
        page.evaluate(
            "([x, y]) => window.__ntMoveCursor && window.__ntMoveCursor(x, y, false)",
            [cx, cy],
        )
        page.wait_for_timeout(280)
        page.evaluate(
            "([x, y]) => window.__ntMoveCursor && window.__ntMoveCursor(x, y, true)",
            [cx, cy],
        )
    locator.click()
    page.wait_for_timeout(140)
    if box is not None:
        cx = box["x"] + box["width"] / 2.0
        cy = box["y"] + box["height"] / 2.0
        page.evaluate(
            "([x, y]) => window.__ntMoveCursor && window.__ntMoveCursor(x, y, false)",
            [cx, cy],
        )
    page.wait_for_timeout(pause_after_ms)


def run_guided_walkthrough(page: Page, target_url: str) -> None:
    page.add_init_script(GUIDED_OVERLAY_INIT_SCRIPT)
    page.goto(target_url, wait_until="networkidle")
    page.evaluate(
        "(txt) => window.__ntSetCaption && window.__ntSetCaption(txt)",
        "1/4 · Calendar Rehearsal Candidate: Dutch Standup (opaque calref_*, zero raw title)",
    )
    page.wait_for_timeout(1200)

    # Select Dutch standup candidate explicitly & confirm to launch 90-second rehearsal
    guided_click(
        page,
        "candidate-shift_clarification",
        "1/4 · Selected Dutch Morning Standup & Shift Handover (target: confirmation)",
        pause_after_ms=900,
    )
    guided_click(
        page,
        "confirm-candidate-btn",
        "1/4 · Confirming candidate & opening live /v1/rehearsal/ws 90-second session",
        pause_after_ms=1000,
    )

    # Play Polly counterpart audio on Turn 1 & inspect SHORT_DOUBLE turn-boundary haptic
    guided_click(
        page,
        "play-polly-audio-btn",
        "2/4 · Polly Counterpart Audio + SHORT_DOUBLE Turn-Boundary Haptic Pulse",
        pause_after_ms=1350,
    )

    # Stream consented Dutch PTT speech on Turn 1 -> STEADY_BUZZ -> chunks -> 0B PCM deleted
    guided_click(
        page,
        "simulate-speech-ptt-btn",
        "2/4 · Streaming Dutch PTT Speech -> Partial Transcription -> Immediate 0B PCM Deletion",
        pause_after_ms=2350,
    )

    # Turn 2: Play counterpart prompt, demonstrate gentle pacing/confirmation repair, then confirm
    guided_click(
        page,
        "play-polly-audio-btn",
        "3/4 · Turn 2 Counterpart: Asking to Confirm Tuesday 08:00 Shift & Handover",
        pause_after_ms=1300,
    )
    guided_click(
        page,
        "try-ambiguous-reply-btn",
        "3/4 · Incomplete Reply ('Ja, dank u.') -> Gentle Functional Repair + SHORT_DOUBLE Haptic",
        pause_after_ms=1500,
    )
    guided_click(
        page,
        "simulate-speech-ptt-btn",
        "3/4 · Speaking Explicit Dutch Shift & Handover Confirmation (Streaming PTT)",
        pause_after_ms=2350,
    )

    # Scene 4: Post-rehearsal communication & pacing breakdown analysis
    page.get_by_test_id("breakdown-summary-panel").wait_for(state="visible", timeout=5000)
    page.evaluate("() => window.scrollTo({ top: 140, behavior: 'smooth' })")
    page.evaluate(
        "(txt) => window.__ntSetCaption && window.__ntSetCaption(txt)",
        "4/4 · Breakdown Analysis: Rubric Satisfied · 0B Raw PCM · Authentic Mastery Untouched",
    )
    page.wait_for_timeout(1400)
    page.get_by_test_id("privacy-ledger-list").scroll_into_view_if_needed()
    page.wait_for_timeout(1500)


def record_demo_video(config: RecorderConfig) -> Path:
    ensure_playwright_ffmpeg_ready()
    ensure_portable_chromium_binary()
    config.output_path.parent.mkdir(parents=True, exist_ok=True)

    api_server: uvicorn.Server | None = None
    api_thread: threading.Thread | None = None
    vite_proc: subprocess.Popen[bytes] | None = None

    try:
        if config.external_url is not None:
            target_url = config.external_url
        else:
            ensure_web_bundle_built()
            api_port = find_free_tcp_port()
            web_port = find_free_tcp_port()

            uvi_cfg = uvicorn.Config(
                create_app(),
                host="127.0.0.1",
                port=api_port,
                log_level="warning",
            )
            api_server = uvicorn.Server(uvi_cfg)
            api_thread = threading.Thread(target=api_server.run, daemon=True)
            api_thread.start()
            wait_for_http_ready(f"http://127.0.0.1:{api_port}/v1/health")

            pnpm_bin = shutil.which("pnpm")
            if pnpm_bin is None:
                raise RuntimeError("pnpm is required to launch the Vite preview server")
            env = {
                **os.environ,
                "VITE_API_PROXY_TARGET": f"http://127.0.0.1:{api_port}",
            }
            vite_proc = subprocess.Popen(  # noqa: S603
                [
                    pnpm_bin,
                    "--dir",
                    str(WEB_APP_DIR),
                    "preview",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--strictPort",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            target_url = f"http://127.0.0.1:{web_port}/nexttime/"
            wait_for_http_ready(target_url)

        with tempfile.TemporaryDirectory(prefix="nexttime_video_") as raw_video_dir:
            with sync_playwright() as pw:
                browser = launch_chromium_browser(pw)
                context = browser.new_context(
                    viewport={
                        "width": config.viewport_width,
                        "height": config.viewport_height,
                    },
                    record_video_dir=raw_video_dir,
                    record_video_size={
                        "width": config.viewport_width,
                        "height": config.viewport_height,
                    },
                )
                page = context.new_page()
                run_guided_walkthrough(page, target_url)
                video_handle = page.video
                context.close()
                browser.close()

                if video_handle is None:
                    raise RuntimeError("Playwright context did not produce a video handle")
                recorded_path = Path(video_handle.path())
                shutil.copy2(recorded_path, config.output_path)

    finally:
        if vite_proc is not None:
            vite_proc.terminate()
            try:
                vite_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vite_proc.kill()
        if api_server is not None:
            api_server.should_exit = True
        if api_thread is not None:
            api_thread.join(timeout=5)

    header = config.output_path.read_bytes()[:4]
    if header != WEBM_EBML_MAGIC:
        raise RuntimeError(
            f"Generated file {config.output_path} does not have a valid WebM EBML header"
        )
    return config.output_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record NextTime guided Dutch rehearsal demo video to docs/media/nexttime-demo.webm."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_VIDEO,
        help="Destination .webm path (default: docs/media/nexttime-demo.webm).",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Optional running NextTime web URL (otherwise starts local API + Vite preview).",
    )
    parser.add_argument("--width", type=int, default=1280, help="Video width in pixels.")
    parser.add_argument("--height", type=int, default=800, help="Video height in pixels.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = RecorderConfig(
        output_path=Path(args.output),
        viewport_width=int(args.width),
        viewport_height=int(args.height),
        external_url=args.url,
    )
    saved = record_demo_video(cfg)
    size_kb = saved.stat().st_size / 1024.0
    sys.stdout.write(f"Recorded demo video: {saved} ({size_kb:.1f} KiB)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
