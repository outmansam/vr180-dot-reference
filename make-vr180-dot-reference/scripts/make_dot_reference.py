#!/usr/bin/env python3
"""Compose a still with the approved VR180 projection dot guide."""

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

CANVAS_SIZE = 2048
CONTENT_SIZE = 1024
PNG_INPUTS = {".png", ".jpg", ".jpeg", ".webp"}
ROW_ELEVATIONS = (-81, -72, -60, -47, -33, -16, 0, 16, 33, 47, 60, 72, 81)


def project(longitude, latitude):
    return (
        CANVAS_SIZE * (0.5 + longitude / 180),
        CANVAS_SIZE * (0.5 - latitude / 180),
    )


def draw_dot(canvas, center, radius, shade):
    x, y = center
    for row in range(max(0, math.floor(y - radius)), min(CANVAS_SIZE, math.ceil(y + radius))):
        for col in range(max(0, math.floor(x - radius)), min(CANVAS_SIZE, math.ceil(x + radius))):
            distance = math.hypot(col + 0.5 - x, row + 0.5 - y)
            weight = min(1, max(0, radius - distance))
            if weight:
                offset = 4 * (row * CANVAS_SIZE + col)
                base = canvas[offset]
                value = round(base + (shade - base) * weight)
                canvas[offset:offset + 3] = bytes((value, value, value))


def draw_tick(canvas, start, end):
    x1, y1 = project(*start)
    x2, y2 = project(*end)
    steps = math.ceil(math.hypot(x2 - x1, y2 - y1) / 2)
    for step in range(steps + 1):
        fraction = step / steps
        center = (x1 + fraction * (x2 - x1), y1 + fraction * (y2 - y1))
        draw_dot(canvas, center, 4.5, 38)


def make_guide(content_height):
    canvas = bytearray(b"\xff") * (CANVAS_SIZE * CANVAS_SIZE * 4)
    for yaw in (-46, 46):
        for row, elevation in enumerate(ROW_ELEVATIONS):
            for column in range(-4, 5):
                horizontal = (column + (0.35 if row % 2 else 0)) * 0.20
                longitude = yaw + math.degrees(math.atan(horizontal))
                latitude = math.degrees(
                    math.atan(math.tan(math.radians(elevation)) / math.hypot(1, horizontal))
                )
                point = project(longitude, latitude)
                if 28 < point[0] < CANVAS_SIZE - 28:
                    dark = row in (0, 6, 12)
                    draw_dot(canvas, point, 7.5 if dark else 6.3, 52 if dark else 112)

    for latitude in (-5, 5):
        draw_tick(canvas, (-90, latitude), (-80, latitude))
        draw_tick(canvas, (80, latitude), (90, latitude))

    edge_latitude = content_height / CANVAS_SIZE * 90
    for longitude in (-5.5, 5.5):
        draw_tick(canvas, (longitude, edge_latitude + 2), (longitude, edge_latitude + 15))
        draw_tick(canvas, (longitude, -edge_latitude - 2), (longitude, -edge_latitude - 15))
    return canvas


def run_media(executable, *args, data=None):
    result = subprocess.run(
        [executable, *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(details or f"{Path(executable).name} exited with {result.returncode}")
    return result.stdout


def image_size(ffprobe, path):
    probe = run_media(
        ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
        "-show_frames", "-show_streams", "-show_entries",
        "stream=width,height:stream_side_data=rotation:frame=width,height:frame_side_data=rotation",
        "-of", "json", str(path),
    )
    info = json.loads(probe)
    if not info.get("streams"):
        raise ValueError(f"No image stream in {path.name}")

    stream = info["streams"][0]
    frame = next(iter(info.get("frames", ())), stream)
    width = frame.get("width", stream.get("width", 0))
    height = frame.get("height", stream.get("height", 0))
    if width < 1 or height < 1:
        raise ValueError(f"Invalid image dimensions for {path.name}")

    rotations = (
        side_data["rotation"]
        for source in (frame, stream)
        for side_data in source.get("side_data_list", ())
        if "rotation" in side_data
    )
    rotation = float(next(rotations, 0))
    return (height, width) if round(rotation) % 180 == 90 else (width, height)


def compose(source, dest, ffmpeg, ffprobe):
    width, height = image_size(ffprobe, source)
    factor = CONTENT_SIZE / max(width, height)
    fitted_width = max(1, round(width * factor))
    fitted_height = max(1, round(height * factor))

    scale = []
    if (width, height) != (fitted_width, fitted_height):
        scale = ["-vf", f"format=rgba,scale={fitted_width}:{fitted_height}:flags=lanczos"]

    pixels = run_media(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", *scale, "-frames:v", "1", "-pix_fmt", "rgba",
        "-f", "rawvideo", "pipe:1",
    )
    row_bytes = fitted_width * 4
    if len(pixels) != row_bytes * fitted_height:
        raise ValueError("Decoded image dimensions differ from the expected fitted size")

    canvas = make_guide(fitted_height)
    left = (CANVAS_SIZE - fitted_width) // 2
    top = (CANVAS_SIZE - fitted_height) // 2
    for row in range(fitted_height):
        start = row * row_bytes
        offset = 4 * ((top + row) * CANVAS_SIZE + left)
        canvas[offset:offset + row_bytes] = pixels[start:start + row_bytes]

    png = run_media(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
        "-pixel_format", "rgba", "-video_size", f"{CANVAS_SIZE}x{CANVAS_SIZE}",
        "-i", "pipe:0", "-frames:v", "1", "-c:v", "png", "-compression_level", "5",
        "-f", "image2pipe", "pipe:1", data=canvas,
    )
    with dest.open("xb") as destination:
        try:
            destination.write(png)
            destination.flush()
        except OSError:
            destination.close()
            dest.unlink(missing_ok=True)
            raise
    return {
        "source_size": [width, height],
        "center_size": [fitted_width, fitted_height],
        "center_offset": [left, top],
        "canvas_size": [CANVAS_SIZE, CANVAS_SIZE],
        "output": str(dest),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Input PNG, JPG, JPEG, or WebP")
    parser.add_argument("-o", "--output", type=Path, help="New PNG path. Defaults to a sibling of the input.")
    args = parser.parse_args()
    source = args.image.expanduser().absolute()
    dest = args.output or source.with_name(f"{source.stem}-dot-reference.png")
    dest = dest.expanduser().absolute()

    if not source.is_file():
        parser.error(f"Image file not found: {source}")
    if source.suffix.lower() not in PNG_INPUTS:
        parser.error("Unsupported image extension. Use PNG, JPG, JPEG, or WebP.")
    if dest.suffix.lower() != ".png":
        parser.error("The output path must end in .png.")
    if dest.exists():
        parser.error(f"Output already exists: {dest}. Choose a new path.")
    if not dest.parent.is_dir():
        parser.error(f"Output directory does not exist: {dest.parent}")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        parser.error("FFmpeg and ffprobe must be available on PATH.")

    try:
        result = compose(source, dest, ffmpeg, ffprobe)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not create dot reference: {error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
