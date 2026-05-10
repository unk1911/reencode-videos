#!/usr/bin/env python3
"""
Re-encode and scale down bloated MP4/MOV files using ffmpeg + CUDA/HEVC.
Finds .mp4/.mov files >= MIN_SIZE_MB in the target directory and re-encodes them.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG = "/usr/bin/ffmpeg"
MIN_SIZE_MB = 50
DEFAULT_OLD_DIR = "/mnt/synology/oldvids"


def find_candidates(directory: Path, recursive: bool) -> list[Path]:
    prefix = "**/" if recursive else ""
    extensions = ("*.mp4", "*.MP4", "*.mov", "*.MOV")
    min_bytes = MIN_SIZE_MB * 1024 * 1024
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


def reencode(input_path: Path, scan_dir: Path, old_base: Path, scale: int, cq: int, dry_run: bool) -> bool:
    # Mirror relative path inside old_base to avoid collisions across subdirs
    rel = input_path.relative_to(scan_dir)
    old_path = old_base / rel
    tmp_path = input_path.with_stem(input_path.stem + "_reencoding_tmp")

    if old_path.exists():
        print(f"  [SKIP] already processed (found in oldvids): {input_path.name}")
        return False

    cmd = [
        FFMPEG, "-hwaccel", "cuda",
        "-i", str(input_path),
        "-vf", f"scale=iw/{scale}:ih/{scale}",
        "-c:v", "hevc_nvenc",
        "-cq", str(cq),
        "-c:a", "aac",
        str(tmp_path),
    ]

    input_size = input_path.stat().st_size
    print(f"\n  Input:   {input_path.name}  ({human_size(input_size)})")
    print(f"  Backup:  {old_path}")
    print(f"  Command: {' '.join(cmd)}")

    if dry_run:
        print("  [DRY RUN] skipping actual encode")
        return False

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  [ERROR] ffmpeg failed:\n{result.stderr[-2000:]}")
        if tmp_path.exists():
            tmp_path.unlink()
        return False

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
        return False

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
        return False

    output_size = input_path.stat().st_size
    ratio = input_size / output_size if output_size else 0
    saved = input_size - output_size
    print(f"  Done.  {human_size(input_size)} -> {human_size(output_size)}  "
          f"(saved {human_size(saved)}, {ratio:.1f}x smaller)")
    return True


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
        "--recursive", "-r", action="store_true",
        help="Scan subdirectories recursively"
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Show what would be done without encoding"
    )
    parser.add_argument(
        "--old-dir", default=DEFAULT_OLD_DIR,
        help=f"Directory to move originals into. Default: {DEFAULT_OLD_DIR}"
    )
    args = parser.parse_args()

    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"Error: path does not exist: {target}", file=sys.stderr)
        sys.exit(1)

    if target.is_dir():
        scan_dir = target
        candidates = find_candidates(scan_dir, args.recursive)
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
    print(f"Settings: scale=1/{args.scale}, cq={args.cq}, "
          f"min_size={MIN_SIZE_MB}MB, recursive={args.recursive}")
    if args.dry_run:
        print("DRY RUN mode — nothing will be encoded")

    if not candidates:
        print("No eligible files found.")
        return

    print(f"\nFound {len(candidates)} eligible file(s):\n")
    for f in candidates:
        print(f"  {f.relative_to(scan_dir)}  ({human_size(f.stat().st_size)})")

    if not args.dry_run:
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
        ok = reencode(f, scan_dir, old_base, args.scale, args.cq, args.dry_run)
        if ok:
            encoded += 1
            total_saved += original_size - f.stat().st_size
        elif not args.dry_run:
            old_path = old_base / f.relative_to(scan_dir)
            if old_path.exists():
                skipped += 1
            else:
                errors += 1

    print(f"\n{'='*50}")
    print(f"Done. Encoded: {encoded}  Skipped: {skipped}  Errors: {errors}")
    if total_saved > 0:
        print(f"Total space saved: {human_size(total_saved)}")


if __name__ == "__main__":
    main()
