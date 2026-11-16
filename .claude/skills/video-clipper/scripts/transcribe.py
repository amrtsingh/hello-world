#!/usr/bin/env python3
"""Transcribe a video's speech with Whisper, for finding moments and for captions.

    transcribe.py INPUT [--model small] [--language en] [--max-words 4] [--outdir DIR]

Writes two files next to each other:
    <name>.transcript.txt  one timestamped line per sentence-ish segment - read this
                           to choose moments
    <name>.srt             short caption cues built from per-word timings - pass this
                           to clip.py --captions

Needs the `faster-whisper` package (pip install faster-whisper). Exits with a plain
message instead of a traceback when it can't run, so the caller can decide what to
do: code 2 = package not installed, code 3 = model could not be downloaded/loaded.
"""
import argparse
import os
import sys


def stamp(seconds, sep=","):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input")
    parser.add_argument("--model", default="small",
                        help="tiny/base/small/medium/large-v3; bigger is slower and more accurate (default small)")
    parser.add_argument("--language", help="e.g. en, es; default is auto-detect")
    parser.add_argument("--max-words", type=int, default=4, help="words per caption cue (default 4)")
    parser.add_argument("--outdir", help="default: next to the input")
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("transcribe.py: faster-whisper is not installed. "
              "Install it with: pip install faster-whisper", file=sys.stderr)
        sys.exit(2)

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input))
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.join(outdir, os.path.splitext(os.path.basename(args.input))[0])

    try:
        model = WhisperModel(args.model, device="auto", compute_type="auto")
    except Exception as error:  # first use downloads the model from huggingface.co
        print(f"transcribe.py: could not load the '{args.model}' model "
              f"({type(error).__name__}: {str(error)[:200]}). The first run downloads it "
              "from huggingface.co, so this usually means that host is unreachable.",
              file=sys.stderr)
        sys.exit(3)
    segments, info = model.transcribe(args.input, language=args.language,
                                      word_timestamps=True, vad_filter=True)

    lines, cues = [], []
    for segment in segments:
        lines.append(f"[{stamp(segment.start, '.')} -> {stamp(segment.end, '.')}] {segment.text.strip()}")
        words = [w for w in (segment.words or []) if w.word.strip()]
        if not words:
            cues.append((segment.start, segment.end, segment.text.strip()))
            continue
        for i in range(0, len(words), args.max_words):
            group = words[i:i + args.max_words]
            cues.append((group[0].start, group[-1].end, "".join(w.word for w in group).strip()))

    with open(stem + ".transcript.txt", "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    with open(stem + ".srt", "w", encoding="utf-8") as handle:
        for n, (start, end, text) in enumerate(cues, 1):
            handle.write(f"{n}\n{stamp(start)} --> {stamp(end)}\n{text}\n\n")

    print(f"language: {info.language}")
    print(f"transcript: {stem}.transcript.txt ({len(lines)} segments)")
    print(f"captions:   {stem}.srt ({len(cues)} cues)")


if __name__ == "__main__":
    main()
