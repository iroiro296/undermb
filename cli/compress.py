#!/usr/bin/env python3
"""Compress a video to at most a target size (MB) for X / social uploads."""

from __future__ import annotations

import argparse
import json
import math
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path


SIZE_MARGIN = 0.97  # leave headroom for container overhead
MIN_VIDEO_BITRATE_BPS = 100_000

ProgressCb = Callable[[float, str], None]


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise SystemExit(
            f"Required tool(s) not found: {', '.join(missing)}\n"
            "Install FFmpeg and ensure it is on PATH."
        )


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=capture,
        encoding="utf-8",
        errors="replace",
    )


def probe(path: Path) -> dict:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,bit_rate,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def duration_seconds(info: dict) -> float:
    duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise SystemExit("Could not read video duration.")
    return duration


def current_size_bytes(path: Path, info: dict) -> int:
    size = info.get("format", {}).get("size")
    if size:
        return int(size)
    return path.stat().st_size


def has_audio(info: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def calc_video_bitrate_bps(target_bytes: int, duration: float, audio_bps: int) -> int:
    usable_bits = target_bytes * 8 * SIZE_MARGIN
    total_bps = usable_bits / duration
    video_bps = int(total_bps - audio_bps)
    if video_bps < MIN_VIDEO_BITRATE_BPS:
        raise SystemExit(
            "Target size is too small for this video length.\n"
            f"Need at least ~{math.ceil((MIN_VIDEO_BITRATE_BPS + audio_bps) * duration / 8 / 1024 / 1024 / SIZE_MARGIN)} MB."
        )
    return video_bps


def default_output(src: Path, target_mb: float) -> Path:
    label = str(int(target_mb)) if float(target_mb).is_integer() else str(target_mb).replace(".", "p")
    return src.with_name(f"{src.stem}_{label}mb{src.suffix if src.suffix.lower() == '.mp4' else '.mp4'}")


def _parse_out_time_seconds(fields: dict[str, str]) -> float | None:
    if "out_time_ms" in fields:
        # ffmpeg reports microseconds in out_time_ms despite the name
        return max(0.0, int(fields["out_time_ms"]) / 1_000_000)
    if "out_time_us" in fields:
        return max(0.0, int(fields["out_time_us"]) / 1_000_000)
    if "out_time" in fields:
        # HH:MM:SS.micro
        parts = fields["out_time"].split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
    return None


def _print_cli_progress(percent: float, label: str, *, newline: bool = False) -> None:
    pct = max(0.0, min(100.0, percent))
    width = 28
    filled = int(width * pct / 100)
    bar = "#" * filled + "-" * (width - filled)
    line = f"\r[{bar}] {pct:5.1f}%  {label}"
    sys.stdout.write(line.ljust(90))
    sys.stdout.flush()
    if newline or pct >= 100:
        sys.stdout.write("\n")
        sys.stdout.flush()


def run_ffmpeg_progress(
    cmd: list[str],
    *,
    duration: float,
    on_progress: ProgressCb | None,
    range_start: float,
    range_end: float,
    label: str,
) -> None:
    """Run ffmpeg with -progress pipe:1 and map time -> overall percent."""
    # Ensure progress goes to stdout as key=value lines
    filtered = [c for c in cmd if c not in ("-stats",)]
    # Insert after ffmpeg binary
    prog_cmd = [filtered[0], "-nostats", "-progress", "pipe:1", "-loglevel", "error", *filtered[1:]]

    proc = subprocess.Popen(
        prog_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None

    fields: dict[str, str] = {}
    last_pct = -1.0
    finished = False

    def emit(local_frac: float) -> None:
        nonlocal last_pct
        local_frac = max(0.0, min(1.0, local_frac))
        overall = range_start + (range_end - range_start) * local_frac
        if on_progress is None:
            if overall - last_pct >= 0.5 or local_frac >= 1.0:
                last_pct = overall
                _print_cli_progress(overall, label, newline=local_frac >= 1.0)
        else:
            on_progress(overall, label)

    emit(0.0)

    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key] = value
        if key == "progress":
            t = _parse_out_time_seconds(fields)
            if t is not None and duration > 0:
                emit(t / duration)
            if value == "end":
                emit(1.0)
                finished = True
            fields = {}

    stderr = proc.stderr.read() if proc.stderr else ""
    code = proc.wait()
    if code != 0:
        raise subprocess.CalledProcessError(code, prog_cmd, stderr=stderr)
    if not finished:
        emit(1.0)


def _two_pass(
    src: Path,
    dst: Path | None,
    *,
    common_video: list[str],
    passlog: Path,
    audio: bool,
    audio_kbps: int,
    duration: float,
    on_progress: ProgressCb | None,
    range_start: float,
    range_end: float,
    stage_label: str,
) -> None:
    mid = range_start + (range_end - range_start) * 0.45
    null_out = "NUL" if sys.platform == "win32" else "/dev/null"

    pass1 = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(src),
        *common_video,
        "-pass",
        "1",
        "-passlogfile",
        str(passlog),
        "-an",
        "-f",
        "null",
        null_out,
    ]
    run_ffmpeg_progress(
        pass1,
        duration=duration,
        on_progress=on_progress,
        range_start=range_start,
        range_end=mid,
        label=f"{stage_label} pass 1/2",
    )

    pass2 = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(src),
        *common_video,
        "-pass",
        "2",
        "-passlogfile",
        str(passlog),
    ]
    if audio:
        pass2 += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    else:
        pass2 += ["-an"]
    assert dst is not None
    pass2.append(str(dst))
    run_ffmpeg_progress(
        pass2,
        duration=duration,
        on_progress=on_progress,
        range_start=mid,
        range_end=range_end,
        label=f"{stage_label} pass 2/2",
    )


def compress(
    src: Path,
    dst: Path,
    target_mb: float,
    *,
    codec: str = "h264",
    audio_kbps: int = 128,
    overwrite: bool = False,
    on_progress: ProgressCb | None = None,
) -> Path:
    require_tools()
    if not src.is_file():
        raise SystemExit(f"Input not found: {src}")

    target_bytes = int(target_mb * 1024 * 1024)
    info = probe(src)
    duration = duration_seconds(info)
    src_size = current_size_bytes(src, info)

    def report(pct: float, label: str) -> None:
        if on_progress is None:
            _print_cli_progress(pct, label)
        else:
            on_progress(pct, label)

    if src_size <= target_bytes:
        print(f"Already under target ({src_size / 1024 / 1024:.1f} MB <= {target_mb} MB). Copying.")
        report(100.0, "Copying")
        if src.resolve() != dst.resolve():
            if dst.exists() and not overwrite:
                raise SystemExit(f"Output exists: {dst} (use --force)")
            shutil.copy2(src, dst)
        return dst

    audio = has_audio(info)
    audio_bps = audio_kbps * 1000 if audio else 0
    video_bps = calc_video_bitrate_bps(target_bytes, duration, audio_bps)

    if dst.exists() and not overwrite:
        raise SystemExit(f"Output exists: {dst} (use --force)")

    dst.parent.mkdir(parents=True, exist_ok=True)

    vcodec = "libx264" if codec == "h264" else "libx265"
    preset = "medium"
    print(
        f"Target: {target_mb} MB | Duration: {duration:.1f}s | "
        f"Video ~{video_bps / 1000:.0f} kbps | Audio {'off' if not audio else f'{audio_kbps} kbps'}"
    )
    print(f"Encoding ({codec}, 2-pass) -> {dst}")

    def video_args(bitrate: int) -> list[str]:
        args = [
            "-c:v",
            vcodec,
            "-b:v",
            str(bitrate),
            "-preset",
            preset,
            "-pix_fmt",
            "yuv420p",
        ]
        if codec == "h264":
            args += ["-profile:v", "high", "-movflags", "+faststart"]
        else:
            args += ["-tag:v", "hvc1", "-movflags", "+faststart"]
        return args

    with tempfile.TemporaryDirectory(prefix="vcompress_") as tmp:
        passlog = Path(tmp) / "ffmpeg2pass"
        _two_pass(
            src,
            dst,
            common_video=video_args(video_bps),
            passlog=passlog,
            audio=audio,
            audio_kbps=audio_kbps,
            duration=duration,
            on_progress=on_progress,
            range_start=0.0,
            range_end=90.0,
            stage_label="Encode",
        )

    out_size = dst.stat().st_size
    print(f"Done: {out_size / 1024 / 1024:.2f} MB (was {src_size / 1024 / 1024:.2f} MB)")

    if out_size > target_bytes:
        ratio = target_bytes * SIZE_MARGIN / out_size
        retry_bps = max(MIN_VIDEO_BITRATE_BPS, int(video_bps * ratio))
        print(f"Slightly over target; retrying at ~{retry_bps / 1000:.0f} kbps...")
        with tempfile.TemporaryDirectory(prefix="vcompress_") as tmp:
            passlog = Path(tmp) / "ffmpeg2pass"
            _two_pass(
                src,
                dst,
                common_video=video_args(retry_bps),
                passlog=passlog,
                audio=audio,
                audio_kbps=audio_kbps,
                duration=duration,
                on_progress=on_progress,
                range_start=90.0,
                range_end=100.0,
                stage_label="Retry",
            )

        out_size = dst.stat().st_size
        print(f"Retry done: {out_size / 1024 / 1024:.2f} MB")

    report(100.0, "Complete")

    if out_size > target_bytes:
        print(
            f"Warning: output is still {out_size / 1024 / 1024:.2f} MB "
            f"(target {target_mb} MB). Try a lower --audio bitrate or shorter clip.",
            file=sys.stderr,
        )
    return dst


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compress a video to at most N MB (e.g. for X / social posts).",
    )
    p.add_argument("input", nargs="?", help="Input video path")
    p.add_argument(
        "-s",
        "--size",
        type=float,
        default=512,
        help="Max size in MB (default: 512)",
    )
    p.add_argument("-o", "--output", help="Output path (default: <name>_<size>mb.mp4)")
    p.add_argument(
        "--codec",
        choices=("h264", "h265"),
        default="h264",
        help="Video codec (h264 = widest compatibility, default)",
    )
    p.add_argument(
        "--audio",
        type=int,
        default=128,
        help="Audio bitrate in kbps (default: 128)",
    )
    p.add_argument("-f", "--force", action="store_true", help="Overwrite output")
    p.add_argument("--gui", action="store_true", help="Open simple GUI")
    return p


def launch_gui(default_size: float = 512.0) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Video Size Compressor")
    root.minsize(520, 280)
    root.geometry("600x300")

    src_var = tk.StringVar()
    dst_var = tk.StringVar()
    size_var = tk.StringVar(value=str(int(default_size) if float(default_size).is_integer() else default_size))
    codec_var = tk.StringVar(value="h264")
    status_var = tk.StringVar(value="Select a video, set max MB, then Compress.")
    progress_var = tk.DoubleVar(value=0.0)

    pad = {"padx": 10, "pady": 6}
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Input").grid(row=0, column=0, sticky="w")
    ttk.Entry(frm, textvariable=src_var, width=52).grid(row=0, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse…", command=lambda: pick_input()).grid(row=0, column=2, **pad)

    ttk.Label(frm, text="Output").grid(row=1, column=0, sticky="w")
    ttk.Entry(frm, textvariable=dst_var, width=52).grid(row=1, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse…", command=lambda: pick_output()).grid(row=1, column=2, **pad)

    ttk.Label(frm, text="Max MB").grid(row=2, column=0, sticky="w")
    ttk.Entry(frm, textvariable=size_var, width=12).grid(row=2, column=1, sticky="w", **pad)

    ttk.Label(frm, text="Codec").grid(row=3, column=0, sticky="w")
    ttk.Combobox(frm, textvariable=codec_var, values=("h264", "h265"), state="readonly", width=10).grid(
        row=3, column=1, sticky="w", **pad
    )

    compress_btn = ttk.Button(frm, text="Compress", command=lambda: do_compress())
    compress_btn.grid(row=4, column=1, sticky="w", **pad)

    progress = ttk.Progressbar(frm, maximum=100, variable=progress_var, mode="determinate")
    progress.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)

    ttk.Label(frm, textvariable=status_var, wraplength=520).grid(
        row=6, column=0, columnspan=3, sticky="w", **pad
    )
    frm.columnconfigure(1, weight=1)

    ui_q: queue.Queue[tuple[str, object]] = queue.Queue()
    busy = {"value": False}

    def pick_input() -> None:
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if path:
            src_var.set(path)
            if not dst_var.get():
                dst_var.set(str(default_output(Path(path), float(size_var.get() or 512))))

    def pick_output() -> None:
        path = filedialog.asksaveasfilename(
            title="Save compressed video",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("All files", "*.*")],
        )
        if path:
            dst_var.set(path)

    def poll_queue() -> None:
        try:
            while True:
                kind, payload = ui_q.get_nowait()
                if kind == "progress":
                    pct, label = payload  # type: ignore[misc]
                    progress_var.set(pct)
                    status_var.set(f"{pct:.1f}% — {label}")
                elif kind == "done":
                    busy["value"] = False
                    compress_btn.configure(state="normal")
                    out, mb = payload  # type: ignore[misc]
                    progress_var.set(100.0)
                    status_var.set(f"Done: {mb:.2f} MB → {out}")
                    messagebox.showinfo("Done", f"Saved ({mb:.2f} MB):\n{out}")
                elif kind == "error":
                    busy["value"] = False
                    compress_btn.configure(state="normal")
                    status_var.set("Failed.")
                    messagebox.showerror("Error", str(payload))
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    def do_compress() -> None:
        if busy["value"]:
            return
        src = src_var.get().strip()
        if not src:
            messagebox.showerror("Error", "Choose an input video.")
            return
        try:
            target = float(size_var.get())
            if target <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Size must be a positive number (MB).")
            return

        out = dst_var.get().strip() or str(default_output(Path(src), target))
        dst_var.set(out)
        progress_var.set(0.0)
        status_var.set("0.0% — Starting…")
        busy["value"] = True
        compress_btn.configure(state="disabled")

        def worker() -> None:
            def on_progress(pct: float, label: str) -> None:
                ui_q.put(("progress", (pct, label)))

            try:
                compress(
                    Path(src),
                    Path(out),
                    target,
                    codec=codec_var.get(),
                    overwrite=True,
                    on_progress=on_progress,
                )
                mb = Path(out).stat().st_size / 1024 / 1024
                ui_q.put(("done", (out, mb)))
            except SystemExit as e:
                ui_q.put(("error", str(e) or "Failed."))
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or "").strip() or f"ffmpeg failed (exit {e.returncode})."
                ui_q.put(("error", detail))
            except Exception as e:  # noqa: BLE001
                ui_q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    root.after(100, poll_queue)
    root.mainloop()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.gui or args.input is None:
        launch_gui(args.size)
        return

    src = Path(args.input)
    dst = Path(args.output) if args.output else default_output(src, args.size)
    compress(
        src,
        dst,
        args.size,
        codec=args.codec,
        audio_kbps=args.audio,
        overwrite=args.force,
    )


if __name__ == "__main__":
    main()
