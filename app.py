import os
import subprocess
import uuid

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
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

STORAGE = "/storage"


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


def _run_ffmpeg(cmd: list[str], cleanup_dir: str | None = None):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        detail = str(e)[-500:] if str(e) else "ffmpeg timed out"
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {detail}")


async def _download(url: str, dest: str):
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download video: {e}")


def _new_task_dir() -> tuple[str, str]:
    """Create a task directory under STORAGE, return (task_id, absolute_path)."""
    os.makedirs(STORAGE, exist_ok=True)
    task_id = uuid.uuid4().hex[:12]
    task_dir = os.path.join(STORAGE, task_id)
    os.makedirs(task_dir, exist_ok=True)
    return task_id, task_dir


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

    task_id, task_dir = _new_task_dir()
    input_path = os.path.join(task_dir, f"input.{fmt}")
    output_path = os.path.join(task_dir, f"output.{fmt}")

    await _download(str(req.url), input_path)

    vf = f"minterpolate='fps={req.target_fps}:mi_mode={mi_mode}',setpts=N/{req.target_fps}/TB"
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", input_path, "-vf", vf,
         "-c:v", "libx264", "-vsync", "cfr", "-an", output_path],
    )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    return {"file": f"storage/{task_id}/output.{fmt}"}


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

    task_id, task_dir = _new_task_dir()
    input_path = os.path.join(task_dir, f"input.{fmt}")
    output_path = os.path.join(task_dir, f"output.{fmt}")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    with open(input_path, "wb") as f:
        f.write(content)

    vf = f"minterpolate='fps={target_fps}:mi_mode={mi_mode}',setpts=N/{target_fps}/TB"
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", input_path, "-vf", vf,
         "-c:v", "libx264", "-vsync", "cfr", "-an", output_path],
    )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    return {"file": f"storage/{task_id}/output.{fmt}"}


# ── split ───────────────────────────────────────────────────────

class SplitRequest(BaseModel):
    url: HttpUrl
    output_format: str = "mp4"
    segment_duration: int = 5


@app.post("/split")
async def split_video(req: SplitRequest):
    fmt = req.output_format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")

    task_id, task_dir = _new_task_dir()
    input_path = os.path.join(task_dir, f"input.{fmt}")
    await _download(str(req.url), input_path)

    # probe duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0:
        raise HTTPException(status_code=500, detail="ffprobe failed")
    try:
        total_duration = float(probe.stdout.strip())
    except ValueError:
        raise HTTPException(status_code=500, detail="Cannot determine video duration")

    dur = req.segment_duration
    num_segments = max(int(total_duration // dur), 1)

    for i in range(num_segments):
        start = i * dur
        seg_path = os.path.join(task_dir, f"segment_{i}.{fmt}")
        _run_ffmpeg([
            "ffmpeg", "-y", "-ss", str(start),
            "-i", input_path, "-t", str(dur),
            "-c:v", "libx264", "-an", seg_path,
        ])

    return {
        "task_id": task_id,
        "segments": [f"storage/{task_id}/segment_{i}.{fmt}" for i in range(num_segments)],
        "total_segments": num_segments,
    }


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

    task_id, task_dir = _new_task_dir()
    filelist_path = os.path.join(task_dir, "filelist.txt")

    inputs: list[str] = []
    for i, url in enumerate(req.urls):
        path = os.path.join(task_dir, f"input_{i}.{fmt}")
        await _download(str(url), path)
        inputs.append(path)

    with open(filelist_path, "w") as f:
        for p in inputs:
            f.write(f"file '{p}'\n")

    output_path = os.path.join(task_dir, f"merged.{fmt}")
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", filelist_path, "-c:v", "libx264", "-an", output_path,
    ])

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    return {"file": f"storage/{task_id}/merged.{fmt}"}


# ── serve stored files ──────────────────────────────────────────

@app.get("/storage/{task_id}/{filename}")
async def get_stored_file(task_id: str, filename: str):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(STORAGE, task_id, safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = safe_name.rsplit(".", 1)[-1].lower()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=content_type, filename=safe_name)
