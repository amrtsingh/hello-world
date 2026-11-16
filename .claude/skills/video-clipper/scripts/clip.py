#!/usr/bin/env python3
"""Cut one or more clips out of a video with ffmpeg.

Handles the fiddly parts so they come out the same every time: frame-accurate
cuts, reframing to vertical/square, snapping cut points to pauses in speech,
burned-in captions, loudness normalisation, fades, and web-friendly encoding.

Single clip:
    clip.py INPUT --start 00:01:23.5 --end 00:01:58 --name "the-big-reveal" --aspect 9:16

Many clips from a spec file:
    clip.py INPUT --spec clips.json --outdir clips/

Spec format (everything except start/end is optional; per-clip keys override
"defaults", which override command-line flags):
    {
      "defaults": {"aspect": "9:16", "fit": "blur", "captions": "talk.srt"},
      "clips": [
        {"start": "00:01:23.5", "end": "00:01:58", "name": "the-big-reveal",
         "title": "Why we threw away the first version"},
        {"start": 754, "end": 790.5, "name": "pricing-mistake", "focus_x": 0.3}
      ]
    }

Writes NN_name.mp4 per clip plus clips_manifest.json describing what was made.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ASPECTS = {  # output frame size per aspect ratio
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}
OPTION_KEYS = [
    "aspect", "fit", "focus_x", "captions", "caption_words", "snap",
    "normalize", "fade", "crf", "copy", "preview",
]


def die(msg):
    sys.exit(f"clip.py: {msg}")


def parse_time(value):
    """Accept seconds (754, "754.5") or [HH:]MM:SS[.mmm] and return seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    parts = text.split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        die(f"can't read time {value!r} (use seconds or HH:MM:SS.mmm)")


def fmt_time(seconds):
    hours, rem = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def probe(path):
    result = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    if result.returncode != 0:
        die(f"ffprobe failed on {path}: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        die(f"{path} has no video stream")
    width, height = int(video["width"]), int(video["height"])
    rotation = 0
    for side in video.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation) or 0)
    if abs(rotation) in (90, 270):  # phone footage: stored sideways, shown upright
        width, height = height, width
    return {
        "duration": float(data["format"].get("duration", 0) or 0),
        "width": width,
        "height": height,
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


# ---------- snapping cut points to pauses ----------

def snap_to_silence(path, t, is_start, window=0.75):
    """Move a cut point to the nearest pause within +/- window seconds.

    Cutting mid-word is the most common thing that makes a clip feel amateur,
    so starts land just before speech resumes and ends just after it stops.
    Returns t unchanged when there is no pause nearby.
    """
    begin = max(t - window, 0)
    result = run([
        "ffmpeg", "-hide_banner", "-nostats", "-ss", f"{begin:.3f}",
        "-t", f"{t - begin + window:.3f}", "-i", path, "-vn",
        "-af", "silencedetect=noise=-35dB:d=0.18", "-f", "null", "-",
    ])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", result.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", result.stderr)]
    if len(ends) < len(starts):  # still silent when the window ran out
        ends.append(t - begin + window)
    best = None
    for s, e in zip(starts, ends):
        s, e = max(s, 0) + begin, e + begin
        if is_start:
            candidate = max(e - 0.10, s)   # a breath of lead-in before the first word
        else:
            candidate = min(s + 0.20, e)   # let the last word ring out
        if best is None or abs(candidate - t) < abs(best - t):
            best = candidate
    return t if best is None else best


# ---------- captions ----------

TIME_RE = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
CUE_RE = re.compile(TIME_RE + r"\s*-->\s*" + TIME_RE)


def _cue_time(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def read_cues(path):
    """Read an .srt or .vtt file into [(start, end, text)]."""
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        blocks = re.split(r"\n\s*\n", handle.read().replace("\r\n", "\n"))
    cues = []
    for block in blocks:
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            match = CUE_RE.search(line)
            if match:
                g = match.groups()
                text = " ".join(l.strip() for l in lines[i + 1:] if l.strip())
                text = re.sub(r"<[^>]+>|\{[^}]*\}", "", text).strip()
                if text:
                    cues.append((_cue_time(*g[:4]), _cue_time(*g[4:]), text))
                break
    return cues


def cues_for_clip(cues, start, end, max_words):
    """Keep cues inside the clip, shift them to clip time, split long ones.

    Short-form captions read best a few words at a time; a cue with no word
    timings is split evenly by character count, which is close enough to follow.
    """
    out = []
    for cue_start, cue_end, text in cues:
        if cue_end <= start or cue_start >= end:
            continue
        a, b = max(cue_start, start) - start, min(cue_end, end) - start
        words = text.split()
        if not max_words or len(words) <= max_words:
            out.append((a, b, text))
            continue
        chunks = [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
        total = sum(len(c) for c in chunks)
        cursor = a
        for chunk in chunks:
            span = (b - a) * len(chunk) / total
            out.append((cursor, cursor + span, chunk))
            cursor += span
    return out


def write_ass(cues, width, height, path):
    """Write captions as .ass sized in real output pixels.

    Built by hand instead of handing ffmpeg the .srt, because ffmpeg's own
    conversion assumes a 4:3 canvas and mis-sizes text on vertical video.
    """
    vertical = height > width
    size = round(width * 0.062) if vertical else round(height * 0.052)
    # Vertical platforms cover the bottom of the frame with their own UI.
    margin_v = round(height * 0.20) if vertical else round(height * 0.07)
    margin_h = round(width * 0.08)

    def ts(seconds):
        cs = round(seconds * 100)
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,DejaVu Sans,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"1,0,0,0,100,100,0,0,1,{max(size // 12, 2)},{max(size // 30, 1)},2,"
        f"{margin_h},{margin_h},{margin_v},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for a, b, text in cues:
        text = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        lines.append(f"Dialogue: 0,{ts(a)},{ts(b)},Default,,0,0,0,,{text}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ---------- building the ffmpeg command ----------

def video_filters(opts, info, duration, has_captions):
    """Return (filtergraph, out_width, out_height)."""
    aspect = opts["aspect"]
    if aspect == "source":
        width, height = info["width"] // 2 * 2, info["height"] // 2 * 2
        chain = f"[0:v]scale={width}:{height},setsar=1[v0]"
    else:
        width, height = ASPECTS[aspect]
        fit = opts["fit"]
        if fit == "crop":
            fx = min(max(float(opts["focus_x"]), 0), 1)
            chain = (
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:(iw-{width})*{fx}:(ih-{height})/2,setsar=1[v0]"
            )
        elif fit == "blur":
            chain = (
                f"[0:v]split[bg][fg];"
                f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=24:2,eq=brightness=-0.08[bgb];"
                f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]"
            )
        else:  # pad
            chain = (
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v0]"
            )
    tail = []
    if has_captions:
        tail.append("ass=captions.ass")
    fade = float(opts["fade"])
    if fade > 0 and duration > fade * 3:
        tail.append(f"fade=t=in:st=0:d={fade}")
        tail.append(f"fade=t=out:st={duration - fade:.3f}:d={fade}")
    tail.append("format=yuv420p")
    return f"{chain};[v0]{','.join(tail)}[v]", width, height


def audio_filters(opts, duration):
    filters = []
    if opts["normalize"]:
        # -14 LUFS is what the short-form platforms normalise to anyway.
        filters.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    fade = float(opts["fade"])
    if fade > 0 and duration > fade * 3:
        filters.append(f"afade=t=in:st=0:d={fade}")
        filters.append(f"afade=t=out:st={duration - fade:.3f}:d={fade}")
    return ",".join(filters)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]


def make_clip(src, info, clip, opts, index, outdir, cue_cache):
    start, end = parse_time(clip["start"]), parse_time(clip["end"])
    if info["duration"]:
        end = min(end, info["duration"])
    if end - start < 0.5:
        die(f"clip {index}: end ({fmt_time(end)}) must be after start ({fmt_time(start)})")
    if opts["snap"] and info["has_audio"]:
        start = snap_to_silence(src, start, is_start=True)
        end = snap_to_silence(src, end, is_start=False)
    duration = end - start

    slug = slugify(clip.get("name") or clip.get("title") or "") or "clip"
    out_path = os.path.join(outdir, f"{index:02d}_{slug}.mp4")

    if opts["copy"]:
        # Instant and lossless, but cuts land on keyframes (can be seconds off)
        # and nothing can be reframed or drawn on the picture.
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-i", src, "-t", f"{duration:.3f}",
               "-c", "copy", "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", out_path]
        result = run(cmd)
        width, height, captioned = info["width"], info["height"], False
    else:
        with tempfile.TemporaryDirectory() as tmp:
            cues = []
            if opts["captions"]:
                cap_path = os.path.abspath(opts["captions"])
                if cap_path not in cue_cache:
                    if not os.path.exists(cap_path):
                        die(f"captions file not found: {cap_path}")
                    cue_cache[cap_path] = read_cues(cap_path)
                cues = cues_for_clip(cue_cache[cap_path], start, end, int(opts["caption_words"]))
            graph, width, height = video_filters(opts, info, duration, bool(cues))
            if cues:
                write_ass(cues, width, height, os.path.join(tmp, "captions.ass"))
            captioned = bool(cues)
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", f"{start:.3f}", "-i", src, "-t", f"{duration:.3f}",
                   "-filter_complex", graph, "-map", "[v]"]
            if info["has_audio"]:
                cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
                afilters = audio_filters(opts, duration)
                if afilters:
                    cmd += ["-af", afilters]
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(opts["crf"]),
                    "-movflags", "+faststart", out_path]
            # Run inside tmp so the caption file needs no filter-path escaping.
            result = run(cmd, cwd=tmp)
    if result.returncode != 0:
        die(f"ffmpeg failed on clip {index} ({slug}):\n{result.stderr.strip()[-1500:]}")

    entry = {
        "file": out_path,
        "title": clip.get("title"),
        "source_start": fmt_time(start),
        "source_end": fmt_time(end),
        "duration_s": round(probe(out_path)["duration"], 2),
        "size": f"{width}x{height}",
        "captions_burned": captioned,
        "bytes": os.path.getsize(out_path),
    }
    if opts["captions"] and not opts["copy"] and not captioned:
        entry["warning"] = "captions file has no lines inside this clip's time range"
    if opts["preview"]:
        preview = out_path[:-4] + ".jpg"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{duration / 2:.3f}",
             "-i", out_path, "-frames:v", "1", "-vf", "scale=-2:640", preview])
        entry["preview"] = preview
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="source video file")
    parser.add_argument("--spec", help="JSON file listing clips (see above)")
    parser.add_argument("--start", help="clip start (single-clip mode)")
    parser.add_argument("--end", help="clip end (single-clip mode)")
    parser.add_argument("--name", help="output name (single-clip mode)")
    parser.add_argument("--only", type=int, metavar="N",
                        help="with --spec: (re)make just clip number N, keeping its numbering")
    parser.add_argument("--outdir", default="clips", help="where to write clips (default: ./clips)")
    parser.add_argument("--aspect", default="source", choices=["source", *ASPECTS],
                        help="output shape (default: same as source)")
    parser.add_argument("--fit", default="crop", choices=["crop", "blur", "pad"],
                        help="how to fill a new shape: crop into the picture, show the whole "
                             "picture over a blurred copy, or over black bars (default: crop)")
    parser.add_argument("--focus-x", dest="focus_x", type=float, default=0.5,
                        help="with --fit crop: where the subject is, 0=left edge 1=right edge")
    parser.add_argument("--captions", help=".srt/.vtt timed to the SOURCE video; burned into each clip")
    parser.add_argument("--caption-words", dest="caption_words", type=int, default=0,
                        help="max words on screen at once (0 = keep the file's own lines)")
    parser.add_argument("--no-snap", dest="snap", action="store_false",
                        help="cut at exactly the given times instead of the nearest pause")
    parser.add_argument("--no-normalize", dest="normalize", action="store_false",
                        help="leave audio loudness alone")
    parser.add_argument("--fade", type=float, default=0.0, help="fade in/out seconds (default: none)")
    parser.add_argument("--crf", type=int, default=20, help="quality, lower is better/bigger (default 20)")
    parser.add_argument("--copy", action="store_true",
                        help="stream copy: instant and lossless but only keyframe-accurate, no reframing/captions")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        help="skip the mid-clip .jpg written next to each clip")
    args = parser.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        die(f"input not found: {src}")
    info = probe(src)

    base = {key: getattr(args, key) for key in OPTION_KEYS}
    if args.spec:
        with open(args.spec, encoding="utf-8") as handle:
            spec = json.load(handle)
        clips = spec["clips"] if isinstance(spec, dict) else spec
        defaults = spec.get("defaults", {}) if isinstance(spec, dict) else {}
    elif args.start is not None and args.end is not None:
        clips, defaults = [{"start": args.start, "end": args.end, "name": args.name}], {}
    else:
        die("give either --spec FILE or both --start and --end")
    if not clips:
        die("no clips to make")

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    manifest_path = os.path.join(outdir, "clips_manifest.json")
    cue_cache, manifest = {}, []
    if args.only is not None:
        if not 1 <= args.only <= len(clips):
            die(f"--only {args.only}: the spec has clips 1..{len(clips)}")
        if os.path.exists(manifest_path):  # keep the other clips' entries
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = [e for e in json.load(handle).get("clips", [])
                            if not os.path.basename(e["file"]).startswith(f"{args.only:02d}_")]
    for index, clip in enumerate(clips, 1):
        if args.only is not None and index != args.only:
            continue
        opts = {**base, **defaults, **{k: v for k, v in clip.items() if k in OPTION_KEYS}}
        if opts["aspect"] not in ("source", *ASPECTS):
            die(f"clip {index}: unknown aspect {opts['aspect']!r}")
        if opts["fit"] not in ("crop", "blur", "pad"):
            die(f"clip {index}: unknown fit {opts['fit']!r}")
        entry = make_clip(src, info, clip, opts, index, outdir, cue_cache)
        manifest.append(entry)
        manifest.sort(key=lambda e: os.path.basename(e["file"]))
        print(f"[{index}/{len(clips)}] {entry['file']}  "
              f"{entry['source_start']} -> {entry['source_end']}  "
              f"{entry['duration_s']}s  {entry['size']}", file=sys.stderr)

    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"source": src, "clips": manifest}, handle, indent=2)
    print(json.dumps({"source": src, "manifest": manifest_path, "clips": manifest}, indent=2))


if __name__ == "__main__":
    main()
