# reencode-videos

A small CLI tool to drastically reduce the size of cellphone-recorded `.mp4`/`.mov` videos with minimal visible quality loss.

It uses `ffmpeg` with NVIDIA NVENC (`hevc_nvenc`) to:
- downscale resolution (for example to 1/2 or 1/4),
- re-encode to HEVC,
- keep a backup of the original in a separate `old` directory.

## Typical use case

Phone videos are often much larger than needed for storage/sharing. This script batch-processes a folder (or a single file), shrinking files heavily while keeping quality "good enough" for casual viewing.

## Usage

```bash
./reencode_videos.py <path> [--scale N] [--cq N] [--recursive] [--old-dir DIR] [--dry-run]
```

- `<path>`: directory to scan, or a single `.mp4`/`.mov` file
- `--scale`: divisor for resolution (`4` = `iw/4:ih/4`)
- `--cq`: NVENC quality (lower = better quality, larger files)
- `--recursive`: scan subfolders
- `--old-dir`: where originals are moved (default: `/mnt/synology/oldvids`)
- `--dry-run`: preview only, no encoding

## Examples

```bash
# Re-encode one file at half resolution
./reencode_videos.py /path/to/video.mp4 --scale 2

# Re-encode all eligible files in a folder
./reencode_videos.py /path/to/folder --recursive
```

## Requirements

- Python 3.10+
- `ffmpeg` installed at `/usr/bin/ffmpeg`
- NVIDIA GPU + drivers (for `hevc_nvenc`)
