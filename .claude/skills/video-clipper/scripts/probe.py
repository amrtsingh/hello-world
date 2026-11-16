#!/usr/bin/env python3
"""Describe a video so clip decisions rest on facts instead of guesses.

    probe.py INPUT                 # size, length, audio, embedded subtitle tracks
    probe.py INPUT --pauses        # also list pauses in the audio (natural cut points)
    probe.py INPUT --scenes        # also list visual scene changes (for footage without speech)
    probe.py INPUT --extract-subs out.srt   # pull the first embedded subtitle track out

Prints JSON.
"""
import argparse
import json
import re
import subprocess
import sys


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input")
    parser.add_argument("--pauses", action="store_true", help="list pauses of 0.5s or longer")
    parser.add_argument("--scenes", action="store_true", help="list scene-change times")
    parser.add_argument("--extract-subs", metavar="OUT.srt", help="write the first subtitle track to a file")
    args = parser.parse_args()

    result = run(["ffprobe", "-v", "error", "-print_format", "json",
                  "-show_format", "-show_streams", args.input])
    if result.returncode != 0:
        sys.exit(f"probe.py: ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    streams = data["streams"]
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    if video is None:
        sys.exit("probe.py: no video stream found")

    width, height = int(video["width"]), int(video["height"])
    rotation = 0
    for side in video.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation) or 0)
    if abs(rotation) in (90, 270):
        width, height = height, width
    num, _, den = video.get("avg_frame_rate", "0/1").partition("/")
    fps = round(float(num) / float(den or 1), 3) if float(den or 1) else 0
    duration = float(data["format"].get("duration", 0) or 0)
    subs = [s for s in streams if s["codec_type"] == "subtitle"]

    report = {
        "file": args.input,
        "duration_s": round(duration, 2),
        "duration": f"{int(duration // 3600):02d}:{int(duration % 3600 // 60):02d}:{duration % 60:05.2f}",
        "width": width,
        "height": height,
        "orientation": "vertical" if height > width else "square" if height == width else "landscape",
        "fps": fps,
        "video_codec": video.get("codec_name"),
        "has_audio": any(s["codec_type"] == "audio" for s in streams),
        "subtitle_tracks": [
            {"index": s["index"], "codec": s.get("codec_name"),
             "language": s.get("tags", {}).get("language")} for s in subs
        ],
    }

    if args.pauses and report["has_audio"]:
        out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", args.input, "-vn",
                   "-af", "silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"]).stderr
        starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", out)]
        ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", out)]
        report["pauses"] = [
            {"start": round(max(s, 0), 2), "end": round(e, 2), "length": round(e - max(s, 0), 2)}
            for s, e in zip(starts, ends)
        ]

    if args.scenes:
        out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", args.input, "-an",
                   "-vf", "select='gt(scene,0.35)',showinfo", "-f", "null", "-"]).stderr
        report["scene_changes"] = [round(float(x), 2) for x in re.findall(r"pts_time:([\d.]+)", out)]

    if args.extract_subs:
        if not subs:
            report["extracted_subs"] = None
        else:
            done = run(["ffmpeg", "-y", "-loglevel", "error", "-i", args.input,
                        "-map", "0:s:0", args.extract_subs])
            report["extracted_subs"] = args.extract_subs if done.returncode == 0 else None
            if done.returncode != 0:
                report["extract_error"] = done.stderr.strip()[-300:]

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
