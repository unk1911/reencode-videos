#!/usr/bin/env python3
"""
Re-encode and scale down bloated MP4/MOV files using ffmpeg + CUDA/HEVC.
Finds .mp4/.mov files at/above a minimum size in the target directory and re-encodes them.

With --convert, instead converts camera originals to portable siblings (kept
alongside the untouched original): .MXF -> .mp4 (H.264 NVENC) and .CR3 -> .jpg
(the camera's embedded full-res JPEG, via exiftool).
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
EXIFTOOL = shutil.which("exiftool") or "/usr/bin/exiftool"
DEFAULT_MIN_SIZE_MB = 25
DEFAULT_OLD_DIR = "/mnt/synology/oldvids"

# Camera-original formats that --convert transcodes/rewraps into a friendly
# sibling file. These are NOT handled by the normal shrink flow (which only
# touches .mp4/.mov); conversion produces a NEW file and leaves the original
# in place. Map of source extension -> output extension.
VIDEO_CONVERT_EXTS = {".mxf": ".mp4"}
IMAGE_CONVERT_EXTS = {".cr3": ".jpg"}
CONVERT_EXTS = {**VIDEO_CONVERT_EXTS, **IMAGE_CONVERT_EXTS}
CONVERT_TMP_MARKER = "_converting_tmp"
# NVENC H.264 quality for MXF->MP4. Lower = better/bigger; 20 is visually high
# quality at 4K. H.264 8-bit 4:2:0 is chosen for maximum playback compatibility.
H264_CONVERT_CQ = 20


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


def audio_channel_counts(path: Path) -> list[int]:
    """Return the channel count of each audio stream, in order (empty if none)."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=channels",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    counts = []
    for line in result.stdout.split():
        try:
            counts.append(int(line))
        except ValueError:
            pass
    return counts


def find_convert_candidates(directory: Path, recursive: bool) -> list[Path]:
    """Find camera-original files (.mxf/.cr3) eligible for --convert. No size
    filter: 'convert these' should never silently skip a file for being small."""
    prefix = "**/" if recursive else ""
    seen: set[Path] = set()
    candidates = []
    for ext in CONVERT_EXTS:
        # glob is case-sensitive; match both .mxf and .MXF etc.
        for pattern_ext in (ext, ext.upper()):
            for f in directory.glob(f"{prefix}*{pattern_ext}"):
                if f in seen or not f.is_file():
                    continue
                seen.add(f)
                if CONVERT_TMP_MARKER in f.stem:
                    continue
                candidates.append(f)
    return sorted(candidates)


def convert_video(input_path: Path, dry_run: bool, force: bool, scale: int = 1) -> str:
    """Transcode a camera video (e.g. .MXF) to a compatibility .mp4 next to it.

    H.264 8-bit 4:2:0 via NVENC for universal playback. With scale > 1 the frame
    is downscaled iw/scale:ih/scale (e.g. scale=2 turns 4K into 1080p). Audio: if
    the source has two or more mono tracks (typical Canon 4-channel layout) the
    first two are joined into one stereo AAC track; a single track is mapped
    as-is; no audio means a silent file. The original is left untouched."""
    out_ext = VIDEO_CONVERT_EXTS[input_path.suffix.lower()]
    out_path = input_path.with_suffix(out_ext)
    tmp_path = input_path.with_stem(input_path.stem + CONVERT_TMP_MARKER).with_suffix(out_ext)

    if out_path.exists() and not force:
        print(f"  [SKIP] output already exists: {out_path.name} "
              f"(use --force to overwrite)")
        return "skipped"

    # Build one filtergraph for both video (optional scale) and audio (optional
    # stereo join) so we never mix -vf with -filter_complex.
    filter_parts = []
    if scale > 1:
        filter_parts.append(f"[0:v:0]scale=iw/{scale}:ih/{scale}[vout]")
        video_map = "[vout]"
        scale_desc = f"scale 1/{scale}"
    else:
        video_map = "0:v:0"
        scale_desc = "full resolution"

    chans = audio_channel_counts(input_path)
    audio_codec = []
    if len(chans) >= 2 and chans[0] == 1 and chans[1] == 1:
        audio_desc = "join ch1+ch2 -> stereo AAC"
        filter_parts.append("[0:a:0][0:a:1]join=inputs=2:channel_layout=stereo[aout]")
        audio_map = "[aout]"
        audio_codec = ["-c:a", "aac", "-b:a", "256k"]
    elif chans:
        audio_desc = "map first audio track -> AAC"
        audio_map = "0:a:0"
        audio_codec = ["-c:a", "aac", "-b:a", "256k"]
    else:
        audio_desc = "no audio"
        audio_map = None

    cmd = [FFMPEG, "-y", "-hwaccel", "cuda", "-i", str(input_path)]
    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", video_map]
    if audio_map is not None:
        cmd += ["-map", audio_map] + audio_codec
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
        "-rc", "vbr", "-cq", str(H264_CONVERT_CQ), "-b:v", "0",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(tmp_path),
    ]

    input_size = input_path.stat().st_size
    print(f"\n  Input:   {input_path.name}  ({human_size(input_size)})")
    print(f"  Output:  {out_path.name}  ({scale_desc}, original kept in place)")
    print(f"  Audio:   {audio_desc}")
    print(f"  Command: {' '.join(cmd)}")

    if dry_run:
        print("  [DRY RUN] skipping actual convert")
        return "dryrun"

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [ERROR] ffmpeg failed:\n{result.stderr[-2000:]}")
        if tmp_path.exists():
            tmp_path.unlink()
        return "error"
    try:
        tmp_path.replace(out_path)
    except Exception as e:
        print(f"  [ERROR] failed to finalize output: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return "error"

    output_size = out_path.stat().st_size
    print(f"  Done.  {out_path.name}  ({human_size(output_size)})")
    return "converted"


def extract_embedded_jpeg(path: Path) -> bytes | None:
    """Pull the full-size JPEG the camera baked into a raw file. Tries the
    full-res JpgFromRaw first, then the smaller PreviewImage as a fallback."""
    for tag in ("-JpgFromRaw", "-PreviewImage"):
        try:
            result = subprocess.run([EXIFTOOL, "-b", tag, str(path)], capture_output=True)
        except FileNotFoundError:
            print(f"  [ERROR] exiftool not found at {EXIFTOOL} "
                  f"(install: sudo apt install libimage-exiftool-perl)")
            return None
        # A real embedded JPEG is many KB; guard against an empty/odd extraction.
        if result.returncode == 0 and len(result.stdout) > 1024:
            return result.stdout
    return None


def convert_image(input_path: Path, dry_run: bool, force: bool) -> str:
    """Extract the camera's embedded full-res JPEG from a raw image (e.g. .CR3)
    to a .jpg next to it. Near-instant (no demosaic) and matches the camera's
    own color rendering. The original raw is left untouched."""
    out_ext = IMAGE_CONVERT_EXTS[input_path.suffix.lower()]
    out_path = input_path.with_suffix(out_ext)
    tmp_path = input_path.with_stem(input_path.stem + CONVERT_TMP_MARKER).with_suffix(out_ext)

    if out_path.exists() and not force:
        print(f"  [SKIP] output already exists: {out_path.name} "
              f"(use --force to overwrite)")
        return "skipped"

    input_size = input_path.stat().st_size
    print(f"\n  Input:   {input_path.name}  ({human_size(input_size)})")
    print(f"  Output:  {out_path.name}  (embedded JPEG, original kept in place)")

    if dry_run:
        print("  [DRY RUN] skipping actual convert")
        return "dryrun"

    data = extract_embedded_jpeg(input_path)
    if data is None:
        print(f"  [ERROR] no embedded JPEG found in {input_path.name}")
        return "error"
    try:
        tmp_path.write_bytes(data)
        tmp_path.replace(out_path)
    except Exception as e:
        print(f"  [ERROR] failed to write output: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return "error"

    output_size = out_path.stat().st_size
    print(f"  Done.  {out_path.name}  ({human_size(output_size)})")
    return "converted"


def convert_file(input_path: Path, dry_run: bool, force: bool, scale: int = 1) -> str:
    """Route a camera-original file to the right converter by extension."""
    ext = input_path.suffix.lower()
    if ext in VIDEO_CONVERT_EXTS:
        return convert_video(input_path, dry_run, force, scale)
    if ext in IMAGE_CONVERT_EXTS:
        # scale does not apply to embedded-JPEG extraction (it's a byte copy).
        return convert_image(input_path, dry_run, force)
    print(f"  [SKIP] not a convertible format: {input_path.name}")
    return "skipped"


def reencode(input_path: Path, scan_dir: Path, old_base: Path, scale: int, cq: int,
             dry_run: bool, force: bool, stabilize: bool, tripod: bool,
             smoothing: int, keep_fov: bool, lossless: bool) -> str:
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
    ]
    if lossless:
        # Truly lossless: pixel-identical to the (stabilized) frames. NVENC
        # ignores -cq in this mode; output is usually LARGER than the already-
        # compressed source. Use for a stabilize-only pass where you don't want
        # to throw away any quality.
        encode_cmd += ["-tune", "lossless"]
    else:
        encode_cmd += ["-cq", str(cq)]
    encode_cmd += ["-c:a", "aac", str(tmp_path)]

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


def run_convert_mode(target: Path, args) -> None:
    """--convert: turn camera originals (.MXF/.CR3) into friendly siblings."""
    if target.is_dir():
        candidates = find_convert_candidates(target, args.recursive)
    else:
        if target.suffix.lower() not in CONVERT_EXTS:
            print(f"Error: --convert supports {', '.join(sorted(CONVERT_EXTS))}; "
                  f"got {target.suffix}", file=sys.stderr)
            sys.exit(1)
        candidates = [target]

    scale_desc = "full res" if args.scale <= 1 else f"scale 1/{args.scale} (video)"
    print(f"Scanning: {target}")
    print(f"Settings: convert mode "
          f"({', '.join(f'{s}->{d}' for s, d in CONVERT_EXTS.items())}), "
          f"{scale_desc}, recursive={args.recursive}, force={args.force}")
    if args.dry_run:
        print("DRY RUN mode — nothing will be converted")

    if not candidates:
        print("No convertible files found.")
        return

    print(f"\nFound {len(candidates)} file(s) to convert:\n")
    for f in candidates:
        print(f"  {f.name}  ({human_size(f.stat().st_size)})  -> {CONVERT_EXTS[f.suffix.lower()]}")

    if not args.dry_run and not args.yes:
        confirm = input(f"\nProceed with converting {len(candidates)} file(s)? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return

    print()
    converted = skipped = errors = 0
    total = len(candidates)
    for i, f in enumerate(candidates, 1):
        pct = i / total * 100
        print(f"[{i}/{total}  {pct:.0f}%] ── {f.name}")
        status = convert_file(f, args.dry_run, args.force, args.scale)
        if status == "converted":
            converted += 1
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors += 1

    print(f"\n{'='*50}")
    print(f"Done. Converted: {converted}  Skipped: {skipped}  Errors: {errors}")


def main():
    parser = argparse.ArgumentParser(
        description="Re-encode bloated MP4/MOV files with ffmpeg CUDA/HEVC."
    )
    parser.add_argument("path", help="Target directory to scan or a specific file")
    parser.add_argument(
        "--scale", type=int, default=None,
        help="Scale divisor (e.g. 4 = iw/4:ih/4). Default: 4 in shrink mode, "
             "1 (full resolution) in --convert mode"
    )
    parser.add_argument(
        "--cq", type=int, default=28,
        help="NVENC constant-quality level, 1-51 (lower=better quality/bigger "
             "file). NOTE: 0 means 'automatic' (VBR), not lossless. Default: 28"
    )
    parser.add_argument(
        "--lossless", action="store_true",
        help="Encode truly lossless (-tune lossless); --cq is ignored. Output is "
             "usually LARGER than the source. Use for stabilize-only passes where "
             "you don't want to lose any quality"
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
        "--convert", action="store_true",
        help="Convert camera-original files to friendly siblings instead of "
             "shrinking: .MXF -> .mp4 (H.264 NVENC, 4:2:0) and .CR3 -> .jpg "
             "(embedded full-res JPEG). Writes a NEW file next to each original "
             "and leaves the original in place. Honors --scale (default 1 = full "
             "resolution; e.g. --scale 2 turns 4K MXF into 1080p). Ignores other "
             "shrink-only flags (--cq/--stabilize/--min-size/--old-dir)"
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
    # --scale defaults differ by mode: shrink downscales 1/4 by default, but a
    # straight --convert keeps full resolution unless the user asks otherwise.
    if args.scale is None:
        args.scale = 1 if args.convert else 4
    if args.scale < 1:
        print("Error: --scale must be >= 1", file=sys.stderr)
        sys.exit(1)

    if args.tripod:
        args.stabilize = True
        print("Note: --tripod locks to a single reference frame; it only helps on "
              "short, near-static clips and can make handheld/moving footage WORSE. "
              "For most footage use plain --stabilize instead.", file=sys.stderr)

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"Error: path does not exist: {target}", file=sys.stderr)
        sys.exit(1)

    if args.convert:
        run_convert_mode(target, args)
        return

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
    quality = "lossless" if args.lossless else f"cq={args.cq}"
    print(f"Settings: scale=1/{args.scale}, {quality}, "
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
                           args.keep_fov, args.lossless)
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
