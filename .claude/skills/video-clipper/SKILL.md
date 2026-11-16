---
name: video-clipper
description: Cut short clips out of longer videos with ffmpeg - trim to exact timestamps, find the best moments in a podcast, stream, talk, interview or webinar, reframe landscape footage to vertical 9:16 or square, burn in captions, and export ready-to-post files for TikTok, Reels, Shorts, X or LinkedIn. Use this whenever the user wants to clip, cut, trim, snip or extract part of a video, asks for highlights, shorts, reels, teasers or "the best bits" of a recording, wants a video made vertical, or gives a video file plus timestamps - even if they never say the word "clip".
---

# Video clipper

Turn a long video into one or more short, finished clips. The bundled scripts do
the ffmpeg work so cuts, reframing and captions come out the same way every time;
your judgement goes into *which* moments to cut and *how* to frame them.

Scripts live in `scripts/` next to this file. They need `ffmpeg`/`ffprobe` and
Python 3; only transcription needs anything extra.

## Workflow

### 1. Get the source and look at it

Work from a local file. If the user gives a URL, download it first (`yt-dlp` if it
is installed) - and only for footage they have the right to reuse.

```bash
python scripts/probe.py INPUT
```

This reports length, frame size, orientation, whether there is audio, and any
embedded subtitle tracks. Read it before deciding anything: orientation decides
whether reframing is needed, and a subtitle track saves you a transcription.

### 2. Decide what to cut

**The user gave timestamps** - use them as given and go to step 3. Don't second-guess
their choice of moment.

**The user wants you to find the moments** - you need the words, with timings. Take
the first of these that exists:

1. A transcript or subtitle file the user supplied (`.srt`, `.vtt`, timestamped text).
2. An embedded track: `python scripts/probe.py INPUT --extract-subs talk.srt`
3. Transcribe it: `python scripts/transcribe.py INPUT` writes `NAME.transcript.txt`
   (read this) and `NAME.srt` (short caption cues for step 3). If it exits with
   code 2, install `faster-whisper` and retry; code 3 means the speech model could
   not be downloaded. If transcription can't be made to work, say so and ask for
   timestamps or a transcript rather than guessing at moments you can't hear.

For footage with no meaningful speech (gameplay, sport, b-roll),
`probe.py INPUT --scenes --pauses` lists scene changes and quiet gaps; pull a few
frames with ffmpeg around candidate times and look at them.

Then read the whole transcript and pick moments. A clip is watched by someone who
has not seen the rest, with the sound possibly off, who will leave in two seconds
if nothing grabs them. So a good clip:

- **Opens on a hook** - a surprising claim, a question, a strong opinion, the start
  of a story. Skip the wind-up ("so, yeah, I think that...") and start on the line
  that earns attention.
- **Stands alone** - no "as I said earlier", no pronoun whose meaning is back in
  minute three. If a moment needs context, start earlier or pick another one.
- **Lands** - ends on the punchline, the conclusion or the answer, then stops. Do
  not trail into the next topic.
- **Is one idea** - usually 20-60 seconds. Go to 90 only when the story needs it;
  under 15 rarely has room for setup and payoff.

Start and end on sentence boundaries from the transcript. Unless told otherwise,
offer 3-5 clips from a long recording, strongest first, with no overlap between
them. Fewer strong clips beat many weak ones - if only two moments are good, make two.

### 3. Cut

Write a spec file listing every clip, then run it once:

```json
{
  "defaults": {"aspect": "9:16", "fit": "blur", "captions": "talk.srt", "caption_words": 4},
  "clips": [
    {"start": "00:12:04.2", "end": "00:12:41.0", "name": "why-we-deleted-v1",
     "title": "Why we threw away the first version"},
    {"start": "00:31:15", "end": "00:32:02", "name": "pricing-mistake"}
  ]
}
```

```bash
python scripts/clip.py INPUT --spec clips.json --outdir clips/
```

For a single clip, flags work too:
`python scripts/clip.py INPUT --start 00:12:04 --end 00:12:41 --name intro --aspect 9:16`

Any option can go in `defaults` or on one clip. `python scripts/clip.py --help`
lists them all; the ones that need a decision:

| Option | Choices | When |
|---|---|---|
| `aspect` | `source` (default), `9:16`, `1:1`, `4:5`, `16:9` | `9:16` for TikTok/Reels/Shorts; `1:1` or `4:5` for feeds; `source` when the user just wants a piece of the video, or names no platform |
| `fit` | `crop` (default), `blur`, `pad` | How landscape footage fills a taller frame - see below |
| `focus_x` | 0-1, default 0.5 | With `crop`: where the subject sits across the original frame |
| `captions` | path to `.srt`/`.vtt` timed to the **source** | Burn captions in. The script picks out and re-times the lines for each clip |
| `caption_words` | e.g. 4 | Words on screen at once. Use 3-5 for vertical clips; leave at 0 to keep the file's own lines |
| `fade` | seconds, default 0 | A short fade (0.2-0.3) softens a cut that can't be made clean |
| `copy` | true/false | Instant lossless copy, but cuts jump to keyframes (can be seconds off) and nothing else applies. Only for "just split this file" jobs where precision doesn't matter |

Two things are on unless turned off, because they are almost always wanted:
cut points move up to 0.75s to the nearest pause so no word is chopped in half
(`"snap": false` for frame-exact cuts, e.g. when cutting to music or when the user
gave precise timestamps they clearly mean), and audio loudness is levelled to the
standard the platforms use (`"normalize": false` to leave it alone).

**Choosing `fit` for vertical output.** Cropping a landscape frame to 9:16 keeps
only the middle third of the picture, so it only works when the subject fits in
that third:

- One person talking, roughly centred -> `crop`. If they sit off-centre, set
  `focus_x` (0.3 = left of centre). Pull a frame and look before guessing.
- Two people side by side, slides, screen recordings, wide action -> `blur`, which
  keeps the whole picture over a blurred copy of itself.
- `pad` (black bars) only when the user asks for it.

Burn captions into vertical/square clips by default whenever you have timed text -
most short-form video is watched muted. Leave them off `source`-shaped clips unless asked.

### 4. Check the result

`clip.py` prints a manifest (also saved as `clips_manifest.json`) with each clip's
final start/end, duration and size, and writes a mid-clip `.jpg` next to each clip.
Look at those images before reporting: confirm the subject is in frame, not cropped
off at the forehead or half out of shot, and captions are legible and clear of the
edges. If a frame looks wrong, fix the spec (`focus_x`, `fit`) and re-run just
that clip with `--only N`. Read any `warning` in the manifest and act on it.

The image only shows the picture. You have not heard the audio, so describe cut
points as chosen from the transcript, not as verified by ear.

### 5. Report

Keep it short and lead with the files:

- A table: clip file, source start-end, length, and a one-line reason it was chosen
  (the hook).
- For clips you selected, a suggested title/caption for posting.
- Anything the user should check themselves or that fell short: a moment that needed
  an awkward cut, captions from auto-transcription that may contain wrong names or
  jargon, a requested clip that couldn't be made.

## Good to know

- Times are seconds (`754.5`) or `HH:MM:SS.mmm`, always on the source video's clock.
- Output is H.264/AAC `.mp4`, playable everywhere and accepted by every platform.
- Auto-transcribed captions get names and technical terms wrong. If the user gave
  the correct spellings, or you can see them elsewhere in the transcript, fix the
  `.srt` before burning it in.
- Long sources are fine: each clip only decodes its own stretch of the file.
- To redo one clip, edit its entry in the spec and re-run with `--only N` (its
  number in the spec). The file is overwritten and the manifest updated in place.
  Keep `name` the same, or the old file is left behind under the old name.
