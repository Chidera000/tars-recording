"""Capture the public TARS Unity experience and export an accelerated MP4."""
import io
import json
import re
import subprocess
import time
import traceback
from pathlib import Path

import pytesseract
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import sync_playwright

OUT = Path("output")
OUT.mkdir(exist_ok=True)
URL = "https://chidera000.itch.io/tars"
MAX_SECONDS = 1080


def image_of(canvas):
    return Image.open(io.BytesIO(canvas.screenshot(timeout=30000))).convert("RGB")


def read_text(image):
    return pytesseract.image_to_string(image.resize((image.width * 2, image.height * 2)), config="--psm 11")


def start_button(image):
    data = pytesseract.image_to_data(image.resize((image.width * 2, image.height * 2)), config="--psm 11", output_type=pytesseract.Output.DICT)
    for i, word in enumerate(data["text"]):
        if "visual" in word.lower():
            return ((data["left"][i] + data["width"][i] / 2) / 2,
                    (data["top"][i] + data["height"][i] / 2) / 2)
    return None


def run():
    logs = []
    metadata = {"source": URL, "limit_seconds": MAX_SECONDS, "complete": False}
    recorder = None
    video_path = None
    trim_start = 0
    bounds = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=[
            "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            "--ignore-gpu-blocklist", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding"])
        context = browser.new_context(viewport={"width": 1280, "height": 800},
            record_video_dir=".capture", record_video_size={"width": 1280, "height": 800}, device_scale_factor=1)
        page = context.new_page()
        created = time.monotonic()
        recorder = page.video
        page.on("console", lambda m: logs.append(m.type + ": " + m.text))
        page.on("pageerror", lambda e: logs.append(str(e)))
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=120000)
            run_game = page.get_by_text("Run game", exact=True)
            if run_game.count():
                run_game.click()
            source = None
            for _ in range(60):
                for frame in page.frames:
                    if "html-classic.itch.zone" in frame.url:
                        source = frame.url
                        break
                if source:
                    break
                page.wait_for_timeout(1000)
            if source:
                print("Opening Unity build", source, flush=True)
                page.goto(source, wait_until="domcontentloaded", timeout=120000)
            canvas = page.locator("canvas").first
            canvas.wait_for(state="visible", timeout=180000)
            menu_image = None
            point = None
            for attempt in range(90):
                menu_image = image_of(canvas)
                text = read_text(menu_image)
                if attempt % 5 == 0:
                    print("Loading:", text[:400], flush=True)
                point = start_button(menu_image)
                if point:
                    break
                if "does not support WebGL" in page.locator("body").inner_text():
                    raise RuntimeError("WebGL is unavailable")
                page.wait_for_timeout(2000)
            if point is None:
                menu_image.save(OUT / "load-diagnostic.png")
                raise RuntimeError("Could not locate Visualization of Mission; see diagnostic image")
            menu_image.save(OUT / "menu.png")
            bounds = canvas.bounding_box()
            trim_start = max(0, time.monotonic() - created - 2)
            canvas.click(position={"x": point[0], "y": point[1]})
            started = time.monotonic()
            print("MISSION STARTED", flush=True)
            seen = set()
            last_small = None
            still_since = None
            final_seen_at = None
            last_save = -30
            timeline = []
            while time.monotonic() - started < MAX_SECONDS:
                elapsed = time.monotonic() - started
                image = image_of(canvas)
                text = read_text(image)
                normalized = re.sub(r"[^a-z ]", " ", text.lower())
                stages = []
                if "orbit" in normalized and "earth" in normalized:
                    stages.append("Earth orbit")
                if "way" in normalized and "moon" in normalized:
                    stages.append("Outbound to Moon")
                if "return" in normalized and "earth" in normalized:
                    stages.append("Returning to Earth")
                if "descent" in normalized or ("entry" in normalized and "landing" in normalized):
                    stages.append("Entry, Descent, and Landing")
                    final_seen_at = final_seen_at or elapsed
                for stage in stages:
                    if stage not in seen:
                        seen.add(stage)
                        print("STAGE", round(elapsed, 1), stage, flush=True)
                timeline.append({"seconds": round(elapsed, 1), "stages": stages, "text": text})
                if elapsed - last_save >= 30:
                    image.save(OUT / ("frame-%04d.png" % int(elapsed)))
                    last_save = elapsed
                    print("Recording", round(elapsed), "seconds;", sorted(seen), flush=True)
                small = image.resize((240, 150))
                difference = 999 if last_small is None else sum(ImageStat.Stat(ImageChops.difference(small, last_small)).mean) / 3
                still_since = (still_since or elapsed) if difference < 0.08 else None
                last_small = small
                ended = any(s in normalized for s in ["mission complete", "simulation complete", "thank you"])
                looped = "Returning to Earth" in seen and "Earth orbit" in stages and elapsed > 30
                stopped = final_seen_at is not None and still_since is not None and elapsed - still_since > 30
                if ended or looped or stopped:
                    metadata["complete"] = True
                    metadata["end_reason"] = "end message" if ended else "loop restarted" if looped else "final phase stopped moving"
                    page.wait_for_timeout(3000)
                    break
                page.wait_for_timeout(2000)
            metadata["recorded_seconds"] = round(time.monotonic() - started, 2)
            metadata["stages_seen"] = sorted(seen)
            if "end_reason" not in metadata:
                metadata["end_reason"] = "capture limit reached; review final frame"
            (OUT / "timeline.json").write_text(json.dumps(timeline, indent=2))
            image_of(canvas).save(OUT / "final.png")
        except Exception:
            page.screenshot(path=str(OUT / "error.png"))
            (OUT / "error.txt").write_text(traceback.format_exc())
            raise
        finally:
            (OUT / "browser-log.txt").write_text("\n".join(logs))
            (OUT / "capture.json").write_text(json.dumps(metadata, indent=2))
            context.close()
            video_path = recorder.path()
            browser.close()
    if not bounds or not metadata.get("recorded_seconds"):
        raise RuntimeError("No mission captured")
    speed = 4
    x, y = int(bounds["x"]), int(bounds["y"])
    w, h = int(bounds["width"]) // 2 * 2, int(bounds["height"]) // 2 * 2
    crop = "crop=%d:%d:%d:%d" % (w, h, x, y)
    print("Encoding MP4 at", speed, "times playback speed", flush=True)
    subprocess.run(["ffmpeg", "-y", "-ss", str(trim_start), "-i", video_path,
        "-t", str(metadata["recorded_seconds"] + 2), "-vf", crop,
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(OUT / "TARS_Full_Simulation.mp4")], check=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(OUT / "TARS_Full_Simulation.mp4"),
        "-vf", "setpts=PTS/4,fps=30", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(OUT / "TARS_Sped_Up_4x.mp4")], check=True)
    print(json.dumps(metadata), flush=True)


if __name__ == "__main__":
    run()
