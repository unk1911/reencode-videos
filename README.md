# reencode-videos

A small CLI tool to drastically reduce the size of cellphone-recorded `.mp4`/`.mov` videos with minimal visible quality loss.

It uses `ffmpeg` with NVIDIA NVENC (`hevc_nvenc`) to:
- downscale resolution (for example to 1/2 or 1/4),
- re-encode to HEVC,
- optionally stabilize shaky footage (2-pass `vidstab`),
- keep a backup of the original in a separate `old` directory.

Re-runs are safe: before encoding, the tool probes each file with `ffprobe` and skips
anything already encoded as HEVC, so running the same command twice won't re-compress
(and degrade) a file you've already processed. Use `--force` to override this.

## Typical use case

Phone videos are often much larger than needed for storage/sharing. This script batch-processes a folder (or a single file), shrinking files heavily while keeping quality "good enough" for casual viewing.

## Usage

```bash
./reencode_videos.py <path> [--scale N] [--cq N] [--lossless] [--min-size MB] [--recursive] [--old-dir DIR] [--dry-run] [--force] [--yes] [--stabilize] [--tripod] [--smoothing N] [--keep-fov] [--convert]
```

- `<path>`: directory to scan, or a single `.mp4`/`.mov` file
- `--scale`: divisor for resolution (`4` = `iw/4:ih/4`)
- `--cq`: NVENC constant-quality level, `1`–`51` (lower = better quality, larger files; default `28`). **Watch out:** `--cq 0` does **not** mean "no compression" — to NVENC, `0` means *automatic* VBR, which can compress harder and look worse. For visually-lossless output use a low non-zero value like `--cq 18`; for truly lossless use `--lossless`.
- `--lossless`: encode truly lossless (`-tune lossless`); `--cq` is ignored. The output is bit-exact to the (stabilized) frames, but is **usually larger than the source** because the original is already lossy-compressed. There is no way to make the output the *exact* same size as the input — re-encoding always changes the byte count. Use this for a stabilize-only pass (`--lossless --stabilize --scale 1`) when you don't want to throw away any quality.
- `--min-size`: minimum file size in MB to be eligible when scanning a directory (default: `25`)
- `--recursive`: scan subfolders
- `--old-dir`: where originals are moved (default: `/mnt/synology/oldvids`)
- `--dry-run`: preview only, no encoding
- `--force` / `-f`: re-encode even if the file is already HEVC **or** was already processed (has a backup). The existing backup is always preserved, never overwritten.
- `--yes` / `-y`: skip the confirmation prompt (batch mode)
- `--stabilize` / `-s`: stabilize shaky footage. Runs a 2-pass `vidstab` operation — pass 1 (`vidstabdetect`) analyzes camera motion, pass 2 (`vidstabtransform`) warps each frame steady, then re-sharpens — before the downscale. **Only suitable for footage that is jittery while holding a roughly fixed framing.** It is the *wrong* tool for fast pans or motion-blurred clips: correcting large motion forces a hard crop/zoom (visible "pumping"), leaves edge-warp smear, and can't remove baked-in motion blur — the result usually looks worse than the original. When in doubt, eyeball the output before keeping it.
- `--tripod`: stabilize by locking every frame to a single reference frame (implies `--stabilize`). Eliminates drift on short clips but degrades over longer ones as the scene moves away from that reference.
- `--smoothing`: `vidstab` smoothing window in frames (ignored with `--tripod`). Default: `10`.
- `--keep-fov`: when stabilizing, preserve the full field-of-view (show black borders where the warp pushes the frame off) instead of zooming/cropping in. Useful for pans where the default zoom-crop loses too much framing. Pairs well with a high `--smoothing` (e.g. `60`–`100`).
- `--convert`: convert camera-original files into friendly siblings instead of shrinking in place. This is a separate workflow from the rest of the flags above — see [Converting camera originals](#converting-camera-originals-mxf--cr3).

## Examples

```bash
# Re-encode one file at half resolution
./reencode_videos.py /path/to/video.mp4 --scale 2

# Re-encode all eligible files in a folder
./reencode_videos.py /path/to/folder --recursive

# Downscale a file that is already HEVC (skipped by default, so force it)
./reencode_videos.py /path/to/iphone.mov --scale 4 --force

# Stabilize shaky handheld footage (keep full resolution). This is the mode
# you almost always want; raise --smoothing for stronger smoothing.
./reencode_videos.py /path/to/shaky.mp4 --stabilize --scale 1
./reencode_videos.py /path/to/shaky.mp4 --stabilize --smoothing 30 --scale 1

# Smooth a pan without zooming in — keep full framing, allow black borders
./reencode_videos.py /path/to/pan.mp4 --stabilize --smoothing 80 --keep-fov --scale 1

# Stabilize ONLY, throwing away no quality (truly lossless; output may be larger)
./reencode_videos.py /path/to/shaky.mp4 --stabilize --lossless --scale 1

# Tripod mode — ONLY for short, near-static clips. On handheld/moving footage
# it locks to one reference frame and makes the result worse, not better.
./reencode_videos.py /path/to/short_static_clip.mp4 --tripod --scale 1
```

## Converting camera originals (.MXF / .CR3)

`--convert` is a distinct mode from the shrink/re-encode flow. Instead of replacing a bloated file in place and backing the original up to `--old-dir`, it reads a camera-original file and writes a **new, more portable sibling next to it**, leaving the original untouched (you almost always want to keep raw camera footage/stills):

| Source | Output | How |
|---|---|---|
| `.MXF` (e.g. Canon 4K H.264) | `.mp4` | Transcode to H.264 8-bit 4:2:0 via NVENC (`-cq 20`, visually high quality) for universal playback. If the source has 2+ mono audio tracks (typical Canon 4-channel layout), channels 1+2 are joined into one stereo AAC track; a single track is mapped as-is. |
| `.CR3` (Canon raw photo) | `.jpg` | Extract the full-resolution JPEG the camera already baked into the raw file (`exiftool -b -JpgFromRaw`, falling back to `-PreviewImage`). Near-instant — no demosaic — and matches the camera's own color rendering. |

```bash
# Convert every .MXF and .CR3 in a folder (originals are kept in place)
./reencode_videos.py /path/to/CARD --convert

# Convert a single file
./reencode_videos.py /path/to/clip.MXF --convert
./reencode_videos.py /path/to/photo.CR3 --convert

# Preview what would be converted, then batch it without the prompt
./reencode_videos.py /path/to/CARD --convert --dry-run
./reencode_videos.py /path/to/CARD --convert --yes

# Re-run safely: existing .mp4/.jpg siblings are skipped (use --force to overwrite)
./reencode_videos.py /path/to/CARD --convert --force
```

```bash
# Downscale while converting: 4K .MXF -> 1080p .mp4 (recommended for big 4K)
./reencode_videos.py /path/to/clip.MXF --convert --scale 2
```

Notes:
- `--convert` honors `--scale` for video (default `1` = full resolution; `--scale 2` turns a 4K MXF into 1080p, `--scale 4` into 540p). `--scale` does not affect `.CR3` (the embedded JPEG is copied out at its native resolution).
- It ignores the other shrink-only flags (`--cq`, `--lossless`, `--stabilize`/`--tripod`, `--min-size`, `--old-dir`), and honors `--recursive`, `--dry-run`, `--force`, and `--yes`.
- Re-runs are safe: a file is skipped if its output sibling already exists. `--force` overwrites it.
- The `.CR3` path needs `exiftool` (`sudo apt install libimage-exiftool-perl`). The `.MXF` path needs only `ffmpeg`.
- To *also* shrink the resulting `.mp4`, run the normal flow on it afterwards (e.g. `./reencode_videos.py /path/to/clip.mp4 --scale 2 --force`).

## Stabilization guide

`vidstab` removes high-frequency *jitter*; it cannot remove intentional motion
or fix baked-in motion blur. Pick the approach by footage type:

| Footage | What to use |
|---|---|
| Shaky but roughly fixed framing (handheld talking-head, static subject) | `--stabilize` (default `--smoothing 10`); raise smoothing for a stronger effect |
| A pan / moving shot that's jerky | `--stabilize --smoothing 60`–`100` `--keep-fov` — smooths the path into a glide and keeps full framing |
| Short, near-static clip with slow drift | `--tripod` |
| Fast pan **and** heavy motion blur | Don't bother — `vidstab` will look worse. Use an NLE (DaVinci Resolve is free) or an AI tool (Topaz) that can also deblur |

Notes:
- `--keep-fov` shows black borders instead of zooming in. Without it, large
  corrections crop/zoom hard and magnify any blur.
- `--tripod` locks to a single reference frame — great for short static shots,
  actively *worse* on anything that pans or moves.
- Stabilization quality is judged by eye, not by a metric. Always watch the
  output before discarding the original (it's preserved in `--old-dir`).

## Requirements

- Python 3.10+
- `ffmpeg` installed at `/usr/bin/ffmpeg`
- `ffprobe` installed at `/usr/bin/ffprobe` (ships with ffmpeg; used for the HEVC skip check)
- ffmpeg built with `--enable-libvidstab` (only needed for `--stabilize` / `--tripod`)
- NVIDIA GPU + drivers (for `hevc_nvenc` / `h264_nvenc`)
- `exiftool` (only needed for `--convert` of `.CR3` files): `sudo apt install libimage-exiftool-perl`
