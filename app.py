import os
import subprocess
import tempfile

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
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
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output format: {fmt}. Allowed: {', '.join(sorted(ALLOWED_FORMATS))}",
        )

    mi_mode = req.minterpolate_mode.lower()
    if mi_mode not in ALLOWED_MI_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported minterpolate mode: {mi_mode}. Allowed: {', '.join(sorted(ALLOWED_MI_MODES))}",
        )

    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, f"input.{fmt}")
    output_path = os.path.join(tmp_dir, f"output.{fmt}")

    # Download video
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(str(req.url))
            resp.raise_for_status()
            with open(input_path, "wb") as f:
                f.write(resp.content)
    except httpx.HTTPError as e:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=400, detail=f"Failed to download video: {e}")

    # Run ffmpeg minterpolate
    vf = f"minterpolate='fps={req.target_fps}:mi_mode={mi_mode}'"
    cmd = [
        "ffmpeg", "-y",
        "-r", str(req.source_fps),
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-r", str(req.target_fps),
        "-vsync", "cfr",
        "-an",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        _cleanup(tmp_dir)
        detail = str(e)[-500:] if str(e) else "ffmpeg timed out"
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {detail}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    content_type = CONTENT_TYPES[fmt]

    def iter_file():
        with open(output_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename=output.{fmt}"},
        background=BackgroundTask(_cleanup, tmp_dir),
    )


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
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output format: {fmt}. Allowed: {', '.join(sorted(ALLOWED_FORMATS))}",
        )

    mi_mode = minterpolate_mode.lower()
    if mi_mode not in ALLOWED_MI_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported minterpolate mode: {mi_mode}. Allowed: {', '.join(sorted(ALLOWED_MI_MODES))}",
        )

    tmp_dir = tempfile.mkdtemp()
    input_path = os.path.join(tmp_dir, f"input.{fmt}")
    output_path = os.path.join(tmp_dir, f"output.{fmt}")

    content = await file.read()
    if not content:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=400, detail="Empty file")
    with open(input_path, "wb") as f:
        f.write(content)

    vf = f"minterpolate='fps={target_fps}:mi_mode={mi_mode}'"
    cmd = [
        "ffmpeg", "-y",
        "-r", str(source_fps),
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-r", str(target_fps),
        "-vsync", "cfr",
        "-an",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        _cleanup(tmp_dir)
        detail = str(e)[-500:] if str(e) else "ffmpeg timed out"
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {detail}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        _cleanup(tmp_dir)
        raise HTTPException(status_code=500, detail="ffmpeg produced no output")

    content_type = CONTENT_TYPES[fmt]

    def iter_file():
        with open(output_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        iter_file(),
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename=output.{fmt}"},
        background=BackgroundTask(_cleanup, tmp_dir),
    )


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
