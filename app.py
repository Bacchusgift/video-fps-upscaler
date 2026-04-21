import os
import subprocess
import tempfile
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="Video FPS Upconverter", version="1.0.0")

CONTENT_TYPES = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mkv": "video/x-matroska",
}

ALLOWED_FORMATS = set(CONTENT_TYPES.keys())
ALLOWED_MI_MODES = {"mci", "aci", "aobmc"}

# ── segment storage ──────────────────────────────────────────────
SEGMENT_STORE = "/tmp/fps_segments"  # persisted inside container
SEGMENT_TTL = 600  # 10 minutes
_segment_registry: dict[str, float] = {}  # task_id → creation timestamp


def _ensure_segment_store():
    os.makedirs(SEGMENT_STORE, exist_ok=True)


def _purge_expired_segments():
    now = time.time()
    expired = [tid for tid, ts in _segment_registry.items() if now - ts > SEGMENT_TTL]
    for tid in expired:
        _cleanup(os.path.join(SEGMENT_STORE, tid))
        _segment_registry.pop(tid, None)


# ── helpers ──────────────────────────────────────────────────────

def _cleanup(tmp_dir: str):
    for fname in os.listdir(tmp_dir):
        try:
            os.remove(os.path.join(tmp_dir, fname))
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass


def _run_ffmpeg(cmd: list[str], tmp_dir: str | None = None):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        if tmp_dir:
            _cleanup(tmp_dir)
        detail = str(e)[-500:] if str(e) else "ffmpeg timed out"
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {detail}")


async def _download(url: str, dest: str, tmp_dir: str | None = None):
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)
    except httpx.HTTPError as e:
        if tmp_dir:
            _cleanup(tmp_dir)
        raise HTTPException(status_code=400, detail=f"Failed to download video: {e}")


# ── convert (URL) ───────────────────────────────────────────────

class ConvertRequest(BaseModel):
    url: HttpUrl
    output_format: str = "mp4"
    source_fps: int = 16
    target_fps: int = 24
    minterpolate_mode: str = "mci"


@app.post("/convert")
async def convert_video(req: ConvertRequest):
    fmt = req.output_format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")
    mi_mode = req.minterpolate_mode.lower()
    if mi_mode not in ALLOWED_MI_MODES:
        raise HTTPException(status_code=400, detail=f"Unsupported mode: {mi_mode}")

    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, f"input.{fmt}")
    output_path = os.path.join(tmp_dir, f"output.{fmt}")

    await _download(str(req.url), input_path, tmp_dir)

    vf = f"minterpolate='fps={req.target_fps}:mi_mode={mi_mode}',setpts=N/{req.target_fps}/TB"
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", input_path, "-vf", vf, "-c:v", "libx264", "-vsync", "cfr", "-an", output_path],
        tmp_dir,
    )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    def iter_file():
        with open(output_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type=CONTENT_TYPES[fmt],
        headers={"Content-Disposition": f"attachment; filename=output.{fmt}"},
        background=BackgroundTask(_cleanup, tmp_dir),
    )


# ── convert (upload) ────────────────────────────────────────────

@app.post("/convert/upload")
async def convert_upload(
    file: UploadFile = File(...),
    output_format: str = Query("mp4"),
    source_fps: int = Query(16),
    target_fps: int = Query(24),
    minterpolate_mode: str = Query("mci"),
):
    fmt = output_format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")
    mi_mode = minterpolate_mode.lower()
    if mi_mode not in ALLOWED_MI_MODES:
        raise HTTPException(status_code=400, detail=f"Unsupported mode: {mi_mode}")

    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, f"input.{fmt}")
    output_path = os.path.join(tmp_dir, f"output.{fmt}")

    content = await file.read()
    if not content:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=400, detail="Empty file")
    with open(input_path, "wb") as f:
        f.write(content)

    vf = f"minterpolate='fps={target_fps}:mi_mode={mi_mode}',setpts=N/{target_fps}/TB"
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", input_path, "-vf", vf, "-c:v", "libx264", "-vsync", "cfr", "-an", output_path],
        tmp_dir,
    )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    def iter_file():
        with open(output_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type=CONTENT_TYPES[fmt],
        headers={"Content-Disposition": f"attachment; filename=output.{fmt}"},
        background=BackgroundTask(_cleanup, tmp_dir),
    )


# ── split ───────────────────────────────────────────────────────

class SplitRequest(BaseModel):
    url: HttpUrl
    output_format: str = "mp4"
    segment_duration: int = 5  # seconds per segment


@app.post("/split")
async def split_video(req: SplitRequest, request: Request):
    fmt = req.output_format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")

    _ensure_segment_store()
    _purge_expired_segments()

    task_id = uuid.uuid4().hex[:12]
    task_dir = os.path.join(SEGMENT_STORE, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, f"input.{fmt}")
    await _download(str(req.url), input_path, task_dir)

    # get video duration via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0:
        _cleanup(task_dir)
        raise HTTPException(status_code=500, detail="ffprobe failed")
    try:
        total_duration = float(probe.stdout.strip())
    except ValueError:
        _cleanup(task_dir)
        raise HTTPException(status_code=500, detail="Cannot determine video duration")

    dur = req.segment_duration
    num_segments = int(total_duration // dur)
    if num_segments == 0:
        num_segments = 1

    # split with ffmpeg
    for i in range(num_segments):
        start = i * dur
        seg_path = os.path.join(task_dir, f"segment_{i}.{fmt}")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", input_path,
            "-t", str(dur),
            "-c:v", "libx264",
            "-an",
            seg_path,
        ]
        _run_ffmpeg(cmd, task_dir)

    _segment_registry[task_id] = time.time()

    base_url = str(request.base_url).rstrip("/")
    segments = [
        f"{base_url}/segments/{task_id}/segment_{i}.{fmt}"
        for i in range(num_segments)
    ]

    return {
        "task_id": task_id,
        "segments": segments,
        "segment_duration": dur,
        "total_segments": num_segments,
        "expire_seconds": SEGMENT_TTL,
    }


# ── serve stored segments ───────────────────────────────────────

@app.get("/segments/{task_id}/{filename}")
async def get_segment(task_id: str, filename: str):
    task_dir = os.path.join(SEGMENT_STORE, task_id)
    if not os.path.isdir(task_dir):
        raise HTTPException(status_code=404, detail="Segment not found or expired")

    # prevent path traversal
    safe_name = os.path.basename(filename)
    file_path = os.path.join(task_dir, safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = safe_name.rsplit(".", 1)[-1].lower()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=content_type, filename=safe_name)


# ── merge ───────────────────────────────────────────────────────

class MergeRequest(BaseModel):
    urls: list[HttpUrl]
    output_format: str = "mp4"


@app.post("/merge")
async def merge_videos(req: MergeRequest):
    if len(req.urls) < 2:
        raise HTTPException(status_code=400, detail="At least 2 URLs required")

    fmt = req.output_format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")

    tmp_dir = tempfile.mkdtemp()
    inputs: list[str] = []
    filelist_path = os.path.join(tmp_dir, "filelist.txt")

    try:
        # download all segments
        for i, url in enumerate(req.urls):
            path = os.path.join(tmp_dir, f"input_{i}.{fmt}")
            await _download(str(url), path, tmp_dir)
            inputs.append(path)

        # write concat file list
        with open(filelist_path, "w") as f:
            for p in inputs:
                f.write(f"file '{p}'\n")

        output_path = os.path.join(tmp_dir, f"output.{fmt}")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", filelist_path,
            "-c:v", "libx264",
            "-an",
            output_path,
        ]
        _run_ffmpeg(cmd, tmp_dir)

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            _cleanup(tmp_dir)
            raise HTTPException(status_code=500, detail="ffmpeg produced no output")

        def iter_file():
            with open(output_path, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk

        return StreamingResponse(
            iter_file(),
            media_type=CONTENT_TYPES[fmt],
            headers={"Content-Disposition": f"attachment; filename=merged.{fmt}"},
            background=BackgroundTask(_cleanup, tmp_dir),
        )
    except Exception:
        _cleanup(tmp_dir)
        raise
