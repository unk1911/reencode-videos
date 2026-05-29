# reencode-videos

A small CLI tool to drastically reduce the size of cellphone-recorded `.mp4`/`.mov` videos with minimal visible quality loss.

It uses `ffmpeg` with NVIDIA NVENC (`hevc_nvenc`) to:
- downscale resolution (for example to 1/2 or 1/4),
- re-encode to HEVC,
- keep a backup of the original in a separate `old` directory.

Re-runs are safe: before encoding, the tool probes each file with `ffprobe` and skips
anything already encoded as HEVC, so running the same command twice won't re-compress
(and degrade) a file you've already processed. Use `--force` to override this.

## Typical use case

Phone videos are often much larger than needed for storage/sharing. This script batch-processes a folder (or a single file), shrinking files heavily while keeping quality "good enough" for casual viewing.

## Usage

```bash
./reencode_videos.py <path> [--scale N] [--cq N] [--min-size MB] [--recursive] [--old-dir DIR] [--dry-run] [--force] [--yes]
```

- `<path>`: directory to scan, or a single `.mp4`/`.mov` file
- `--scale`: divisor for resolution (`4` = `iw/4:ih/4`)
- `--cq`: NVENC quality (lower = better quality, larger files)
- `--min-size`: minimum file size in MB to be eligible when scanning a directory (default: `25`)
- `--recursive`: scan subfolders
- `--old-dir`: where originals are moved (default: `/mnt/synology/oldvids`)
- `--dry-run`: preview only, no encoding
- `--force` / `-f`: re-encode even if the file is already HEVC **or** was already processed (has a backup). The existing backup is always preserved, never overwritten.
- `--yes` / `-y`: skip the confirmation prompt (batch mode)

## Examples

```bash
# Re-encode one file at half resolution
./reencode_videos.py /path/to/video.mp4 --scale 2

# Re-encode all eligible files in a folder
./reencode_videos.py /path/to/folder --recursive

# Downscale a file that is already HEVC (skipped by default, so force it)
./reencode_videos.py /path/to/iphone.mov --scale 4 --force
```

## Requirements

- Python 3.10+
- `ffmpeg` installed at `/usr/bin/ffmpeg`
- `ffprobe` installed at `/usr/bin/ffprobe` (ships with ffmpeg; used for the HEVC skip check)
- NVIDIA GPU + drivers (for `hevc_nvenc`)
