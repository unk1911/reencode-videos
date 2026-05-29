#!/usr/bin/env python3
"""
Re-encode and scale down bloated MP4/MOV files using ffmpeg + CUDA/HEVC.
Finds .mp4/.mov files at/above a minimum size in the target directory and re-encodes them.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"
DEFAULT_MIN_SIZE_MB = 25
DEFAULT_OLD_DIR = "/mnt/synology/oldvids"


def find_candidates(directory: Path, recursive: bool, min_size_mb: int) -> list[Path]:
    prefix = "**/" if recursive else ""
    extensions = ("*.mp4", "*.MP4", "*.mov", "*.MOV")
    min_bytes = min_size_mb * 1024 * 1024
    seen: set[Path] = set()
    candidates = []
    for ext in extensions:
        for f in directory.glob(f"{prefix}{ext}"):
            if f in seen:
                continue
            seen.add(f)
            if "_reencoding_tmp" in f.stem:
                continue
            if f.stat().st_size >= min_bytes:
                candidates.append(f)
    return sorted(candidates)


def human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def video_codec(path: Path) -> str | None:
    """Return the codec_name of the first video stream, or None if undetermined."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def reencode(input_path: Path, scan_dir: Path, old_base: Path, scale: int, cq: int,
             dry_run: bool, force: bool, stabilize: bool, tripod: bool,
             smoothing: int, keep_fov: bool) -> str:
    # Mirror relative path inside old_base to avoid collisions across subdirs
    rel = input_path.relative_to(scan_dir)
    old_path = old_base / rel
    tmp_path = input_path.with_stem(input_path.stem + "_reencoding_tmp")

    backup_exists = old_path.exists()
    if backup_exists and not force:
        print(f"  [SKIP] already processed (found in oldvids): {input_path.name} "
              f"(use --force to re-encode anyway)")
        return "skipped"

    if not force:
        codec = video_codec(input_path)
        if codec == "hevc":
            print(f"  [SKIP] already HEVC, nothing to gain: {input_path.name} "
                  f"(use --force to re-encode anyway)")
            return "skipped"

    # Build the video filter chain. When stabilizing, vidstab is a 2-pass op:
    # pass 1 (vidstabdetect) writes a transforms file describing camera motion;
    # pass 2 (vidstabtransform) warps each frame steady. Stabilize runs before
    # the downscale so motion is measured at full resolution.
    trf_path = None
    detect_cmd = None
    vf_parts = []
    if stabilize:
        fd, trf_name = tempfile.mkstemp(suffix=".trf", prefix="vidstab_")
        os.close(fd)
        trf_path = Path(trf_name)
        # tripod mode must be set in BOTH passes: detect compares every frame to
        # reference frame 1, and transform locks to it (smoothing=0). It kills
        # drift on short, near-static clips but actively makes longer/moving
        # footage worse as the scene drifts away from that reference — prefer
        # plain --stabilize (smoothing) there.
        detect_vf = "vidstabdetect=tripod=1:" if tripod else "vidstabdetect="
        detect_cmd = [
            FFMPEG, "-y", "-hwaccel", "cuda",
            "-i", str(input_path),
            "-vf", f"{detect_vf}result={trf_path}",
            "-f", "null", "-",
        ]
        smooth = 0 if tripod else smoothing
        transform = f"vidstabtransform=input={trf_path}:smoothing={smooth}"
        if tripod:
            transform += ":tripod=1"
        if keep_fov:
            # Preserve full field-of-view: no zoom-in, show black borders where
            # the warp pushes the frame off, instead of zooming/cropping in.
            transform += ":optzoom=0:crop=black"
        vf_parts.append(transform)
        vf_parts.append("unsharp=5:5:0.8:3:3:0.4")  # vidstab-recommended re-sharpen
    vf_parts.append(f"scale=iw/{scale}:ih/{scale}")
    vf = ",".join(vf_parts)

    encode_cmd = [
        FFMPEG, "-y", "-hwaccel", "cuda",
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", "hevc_nvenc",
        "-cq", str(cq),
        "-c:a", "aac",
        str(tmp_path),
    ]

    def cleanup():
        if tmp_path.exists():
            tmp_path.unlink()
        if trf_path and trf_path.exists():
            trf_path.unlink()

    input_size = input_path.stat().st_size
    print(f"\n  Input:   {input_path.name}  ({human_size(input_size)})")
    if backup_exists:
        print(f"  Backup:  {old_path}  (already exists — original preserved, not overwritten)")
    else:
        print(f"  Backup:  {old_path}")
    if detect_cmd:
        mode = "tripod" if tripod else f"smoothing={smoothing}"
        print(f"  Stabilize: 2-pass vidstab ({mode})")
        print(f"  Pass 1:  {' '.join(detect_cmd)}")
        print(f"  Pass 2:  {' '.join(encode_cmd)}")
    else:
        print(f"  Command: {' '.join(encode_cmd)}")

    if dry_run:
        print("  [DRY RUN] skipping actual encode")
        cleanup()
        return "dryrun"

    if detect_cmd:
        print("  [stabilize] pass 1/2: analyzing camera motion...")
        detect = subprocess.run(detect_cmd, capture_output=True, text=True)
        if detect.returncode != 0:
            print(f"  [ERROR] vidstabdetect failed:\n{detect.stderr[-2000:]}")
            cleanup()
            return "error"
        print("  [stabilize] pass 2/2: transforming + encoding...")

    result = subprocess.run(encode_cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  [ERROR] ffmpeg failed:\n{result.stderr[-2000:]}")
        cleanup()
        return "error"

    if trf_path and trf_path.exists():
        trf_path.unlink()

    if backup_exists:
        # The true original is already safe in old_base; the file in place is a
        # prior re-encode. Replace it with the new encode and leave the backup
        # untouched so we never clobber the original. tmp is in the same dir, so
        # this is an atomic same-filesystem replace.
        try:
            tmp_path.replace(input_path)
        except Exception as e:
            print(f"  [ERROR] failed to replace file with encoded version: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return "error"
    else:
        # Move original to old_base (mirroring subdir structure), rename tmp to original name
        old_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Some network filesystems reject metadata ops used by copy2.
            # copyfile avoids copystat/utime while still moving file contents.
            shutil.move(str(input_path), str(old_path), copy_function=shutil.copyfile)
        except Exception as e:
            print(f"  [ERROR] failed to move original to backup: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return "error"

        try:
            tmp_path.rename(input_path)
        except Exception as e:
            print(f"  [ERROR] failed to replace original with encoded file: {e}")
            # Best-effort rollback so source file path is restored.
            if old_path.exists() and not input_path.exists():
                try:
                    shutil.move(str(old_path), str(input_path), copy_function=shutil.copyfile)
                except Exception as rollback_err:
                    print(f"  [ERROR] rollback failed: {rollback_err}")
            return "error"

    output_size = input_path.stat().st_size
    ratio = input_size / output_size if output_size else 0
    saved = input_size - output_size
    print(f"  Done.  {human_size(input_size)} -> {human_size(output_size)}  "
          f"(saved {human_size(saved)}, {ratio:.1f}x smaller)")
    return "encoded"


def main():
    parser = argparse.ArgumentParser(
        description="Re-encode bloated MP4/MOV files with ffmpeg CUDA/HEVC."
    )
    parser.add_argument("path", help="Target directory to scan or a specific file")
    parser.add_argument(
        "--scale", type=int, default=4,
        help="Scale divisor (e.g. 4 = iw/4:ih/4). Default: 4"
    )
    parser.add_argument(
        "--cq", type=int, default=28,
        help="NVENC CQ quality (lower=better quality/bigger file). Default: 28"
    )
    parser.add_argument(
        "--min-size", type=int, default=DEFAULT_MIN_SIZE_MB, metavar="MB",
        help=f"Minimum file size in MB to be eligible. Default: {DEFAULT_MIN_SIZE_MB}"
    )
    parser.add_argument(
        "--recursive", "-r", action="store_true",
        help="Scan subdirectories recursively"
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Show what would be done without encoding"
    )
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Re-encode even if already HEVC or already processed (the existing "
             "backup is preserved, never overwritten)"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt (batch mode)"
    )
    parser.add_argument(
        "--stabilize", "-s", action="store_true",
        help="Stabilize shaky footage with a 2-pass ffmpeg vidstab analysis/transform"
    )
    parser.add_argument(
        "--tripod", action="store_true",
        help="Stabilize by locking to a single reference frame (implies --stabilize). "
             "Best for short clips; drifts on long ones"
    )
    parser.add_argument(
        "--smoothing", type=int, default=10, metavar="N",
        help="vidstab smoothing window in frames (ignored with --tripod). Default: 10"
    )
    parser.add_argument(
        "--keep-fov", action="store_true",
        help="When stabilizing, preserve full field-of-view (show black borders) "
             "instead of zooming/cropping in. Pairs well with a high --smoothing"
    )
    parser.add_argument(
        "--old-dir", default=DEFAULT_OLD_DIR,
        help=f"Directory to move originals into. Default: {DEFAULT_OLD_DIR}"
    )
    args = parser.parse_args()
    if args.tripod:
        args.stabilize = True
        print("Note: --tripod locks to a single reference frame; it only helps on "
              "short, near-static clips and can make handheld/moving footage WORSE. "
              "For most footage use plain --stabilize instead.", file=sys.stderr)

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"Error: path does not exist: {target}", file=sys.stderr)
        sys.exit(1)

    if target.is_dir():
        scan_dir = target
        candidates = find_candidates(scan_dir, args.recursive, args.min_size)
    elif target.is_file():
        scan_dir = target.parent
        valid_exts = {".mp4", ".mov"}
        if target.suffix.lower() not in valid_exts:
            print(f"Error: unsupported file extension: {target.suffix}", file=sys.stderr)
            sys.exit(1)
        if "_reencoding_tmp" in target.stem:
            print("No eligible files found.")
            return
        candidates = [target]
    else:
        print(f"Error: not a file or directory: {target}", file=sys.stderr)
        sys.exit(1)

    old_base = Path(args.old_dir).expanduser().resolve()

    print(f"Scanning: {target}")
    print(f"Old dir:  {old_base}")
    if args.stabilize:
        stab = "tripod" if args.tripod else f"smoothing={args.smoothing}"
        if args.keep_fov:
            stab += ",keep-fov"
    else:
        stab = "off"
    print(f"Settings: scale=1/{args.scale}, cq={args.cq}, "
          f"min_size={args.min_size}MB, recursive={args.recursive}, stabilize={stab}")
    if args.dry_run:
        print("DRY RUN mode — nothing will be encoded")

    if not candidates:
        print("No eligible files found.")
        return

    print(f"\nFound {len(candidates)} eligible file(s):\n")
    for f in candidates:
        print(f"  {f.relative_to(scan_dir)}  ({human_size(f.stat().st_size)})")

    if not args.dry_run and not args.yes:
        confirm = input(f"\nProceed with encoding {len(candidates)} file(s)? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return

    print()
    encoded = skipped = errors = 0
    total_saved = 0
    total = len(candidates)

    for i, f in enumerate(candidates, 1):
        pct = i / total * 100
        print(f"[{i}/{total}  {pct:.0f}%] ── {f.name}")
        original_size = f.stat().st_size
        status = reencode(f, scan_dir, old_base, args.scale, args.cq, args.dry_run,
                           args.force, args.stabilize, args.tripod, args.smoothing,
                           args.keep_fov)
        if status == "encoded":
            encoded += 1
            total_saved += original_size - f.stat().st_size
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors += 1

    print(f"\n{'='*50}")
    print(f"Done. Encoded: {encoded}  Skipped: {skipped}  Errors: {errors}")
    if total_saved > 0:
        print(f"Total space saved: {human_size(total_saved)}")


if __name__ == "__main__":
    main()
