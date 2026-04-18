from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from afk_x2gltf.bootstrap import (
    AssimpBootstrapError,
    VENDOR_DIR,
    ensure_assimp,
    register_existing_dll,
)
from afk_x2gltf.native_dialog import pick_folder as _pick_folder
from afk_x2gltf.config import (
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    AxisUp,
    ConvertConfig,
    OutputFormat,
)
from afk_x2gltf.converter import BatchConverter


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class ConvertRequest(BaseModel):
    input_dir: str = Field(..., description="输入目录")
    output_dir: str = Field(..., description="输出目录")
    output_format: str = "glb"
    recursive: bool = True
    overwrite: bool = True

    axis_up: str = "y_up"
    flip_handedness: bool = False
    global_scale: float = 1.0

    join_identical_vertices: bool = True
    generate_normals: bool = False
    generate_smooth_normals: bool = False
    calc_tangent_space: bool = False
    triangulate: bool = True
    limit_bone_weights: bool = True
    improve_cache_locality: bool = False

    keep_single_animation: str | None = None

    embed_textures: bool = True
    copy_textures_for_gltf: bool = True

    workers: int = 4


@dataclass(slots=True)
class JobProgress:
    done: int = 0
    total: int = 0
    current: str = ""
    status: str = "pending"
    events: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    ok: int = 0
    failed: int = 0


_jobs: dict[str, JobProgress] = {}
_jobs_lock = threading.Lock()
_install_state: dict[str, Any] = {"running": False, "progress": 0.0, "message": "", "error": None}


def _to_config(req: ConvertRequest) -> ConvertConfig:
    output_dir = Path(req.output_dir)
    return ConvertConfig(
        input_dir=Path(req.input_dir),
        output_dir=output_dir,
        output_format=OutputFormat(req.output_format),
        recursive=req.recursive,
        overwrite=req.overwrite,
        axis_up=AxisUp(req.axis_up),
        flip_handedness=req.flip_handedness,
        global_scale=req.global_scale,
        join_identical_vertices=req.join_identical_vertices,
        generate_normals=req.generate_normals,
        generate_smooth_normals=req.generate_smooth_normals,
        calc_tangent_space=req.calc_tangent_space,
        triangulate=req.triangulate,
        limit_bone_weights=req.limit_bone_weights,
        improve_cache_locality=req.improve_cache_locality,
        keep_single_animation=req.keep_single_animation,
        embed_textures=req.embed_textures,
        copy_textures_for_gltf=req.copy_textures_for_gltf,
        workers=req.workers,
        report_path=output_dir / "_convert_report.json",
    )


def _run_job(job_id: str, cfg: ConvertConfig) -> None:
    job = _jobs[job_id]

    def progress(done: int, total: int, src: str, msg: str) -> None:
        with _jobs_lock:
            job.done = done
            job.total = total
            job.current = src
            job.events.append(
                {
                    "done": done,
                    "total": total,
                    "src": src,
                    "msg": msg,
                    "ts": time.time(),
                }
            )

    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        results = BatchConverter(cfg).run(progress=progress)
        with _jobs_lock:
            job.ok = sum(1 for r in results if r.ok)
            job.failed = sum(1 for r in results if not r.ok)
            job.status = "done" if job.failed == 0 else "partial"
            job.finished = True
            for r in results:
                if not r.ok:
                    job.events.append(
                        {
                            "done": job.done,
                            "total": job.total,
                            "src": str(r.source),
                            "msg": f"FAIL: {r.message}",
                            "ts": time.time(),
                        }
                    )
    except Exception as exc:
        with _jobs_lock:
            job.status = "error"
            job.finished = True
            job.events.append(
                {
                    "done": job.done,
                    "total": job.total,
                    "src": "",
                    "msg": f"ERROR: {exc}",
                    "ts": time.time(),
                }
            )


def _run_install(force: bool) -> None:
    def cb(label: str, done: int, total: int) -> None:
        with _jobs_lock:
            _install_state["message"] = label
            _install_state["progress"] = (done / total) if total else 0.0

    with _jobs_lock:
        _install_state["running"] = True
        _install_state["error"] = None
        _install_state["progress"] = 0.0
        _install_state["message"] = "starting"
    try:
        ensure_assimp(progress=cb, force=force)
        with _jobs_lock:
            _install_state["message"] = "installed"
            _install_state["progress"] = 1.0
    except AssimpBootstrapError as exc:
        with _jobs_lock:
            _install_state["error"] = str(exc)
            _install_state["message"] = "failed"
    finally:
        with _jobs_lock:
            _install_state["running"] = False


def create_app() -> FastAPI:
    register_existing_dll()
    app = FastAPI(title="X2glTF", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "assimp_installed": (VENDOR_DIR / "assimp-vc143-mt.dll").exists(),
        }

    @app.get("/api/defaults")
    def defaults() -> dict[str, str]:
        return {
            "input_dir": str(DEFAULT_INPUT_DIR),
            "output_dir": str(DEFAULT_OUTPUT_DIR),
        }

    @app.post("/api/pick-folder")
    def pick_folder(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        title = (payload or {}).get("title", "选择目录")
        path = _pick_folder(str(title))
        return {"path": path}

    @app.get("/api/scan")
    def scan(dir: str, recursive: bool = True) -> dict[str, Any]:
        p = Path(dir)
        if not p.exists():
            return {"exists": False, "count": 0, "files": []}
        pattern = "**/*.x" if recursive else "*.x"
        files = sorted(str(x) for x in p.glob(pattern) if x.is_file())
        return {"exists": True, "count": len(files), "files": files[:500]}

    @app.post("/api/convert")
    def convert(req: ConvertRequest) -> dict[str, str]:
        try:
            cfg = _to_config(req)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        if not cfg.input_dir.exists():
            raise HTTPException(400, f"input_dir not found: {cfg.input_dir}")

        job_id = uuid.uuid4().hex
        _jobs[job_id] = JobProgress(status="running")
        threading.Thread(target=_run_job, args=(job_id, cfg), daemon=True).start()
        return {"job_id": job_id}

    @app.get("/api/convert/{job_id}/stream")
    async def stream(job_id: str) -> StreamingResponse:
        if job_id not in _jobs:
            raise HTTPException(404, "job not found")

        async def gen():
            last = 0
            while True:
                with _jobs_lock:
                    job = _jobs[job_id]
                    events = list(job.events[last:])
                    last = len(job.events)
                    finished = job.finished
                    snapshot = {
                        "done": job.done,
                        "total": job.total,
                        "status": job.status,
                        "ok": job.ok,
                        "failed": job.failed,
                        "finished": job.finished,
                    }
                payload = {"snapshot": snapshot, "events": events}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if finished:
                    break
                await asyncio.sleep(0.2)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/assimp/install")
    def install(force: bool = False) -> dict[str, Any]:
        with _jobs_lock:
            if _install_state["running"]:
                return {"running": True}
        threading.Thread(target=_run_install, args=(force,), daemon=True).start()
        return {"running": True}

    @app.get("/api/assimp/status")
    def install_status() -> dict[str, Any]:
        with _jobs_lock:
            return dict(_install_state) | {
                "installed": (VENDOR_DIR / "assimp-vc143-mt.dll").exists()
            }

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()


def run(host: str = "127.0.0.1", port: int = 0) -> tuple[str, int, threading.Thread]:
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    ready_event = threading.Event()

    def serve() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    for _ in range(200):
        if server.started and server.servers:
            break
        time.sleep(0.05)

    actual_port = port
    if server.servers:
        for s in server.servers:
            for sock in s.sockets:
                actual_port = sock.getsockname()[1]
                break
            break
    ready_event.set()
    return host, actual_port, thread
