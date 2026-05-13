"""
api.py — FastAPI backend
REST endpoints + SSE real-time log stream
"""

import json
import asyncio
import datetime
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Body, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel

import config
import runner as test_runner
from personas import PERSONAS, GROUND_TRUTH

app = FastAPI(title="Olly Focus Group", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_stream_queues: dict[str, asyncio.Queue] = {}
_scheduler_state: dict = {"active": False, "day": None, "time": None}
_scheduler_task: Optional[asyncio.Task] = None


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    if not html_path.exists():
        raise HTTPException(404, "dashboard.html bulunamadı")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# PERSONA CRUD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/personas")
async def list_personas():
    return [
        {
            "id":               pid,
            "display_name":     p["display_name"],
            "group":            p["group"],
            "language":         p["language"],
            "max_steps":        p.get("max_steps", 10),
            "has_ground_truth": pid in GROUND_TRUTH,
        }
        for pid, p in PERSONAS.items()
    ]


@app.get("/api/personas/{persona_id}")
async def get_persona(persona_id: str):
    if persona_id not in PERSONAS:
        raise HTTPException(404, f"Persona bulunamadı: {persona_id}")
    p  = PERSONAS[persona_id]
    gt = GROUND_TRUTH.get(persona_id, [])
    return {**p, "ground_truth": gt}


class PersonaCreate(BaseModel):
    id:              str
    display_name:    str
    group:           str
    language:        str = "tr"
    max_steps:       int = 10
    system_prompt:   str
    initial_context: str
    scenario_hints:  list[str] = []
    ground_truth:    list[dict] = []


@app.post("/api/personas", status_code=201)
async def create_persona(data: PersonaCreate):
    if data.id in PERSONAS:
        raise HTTPException(409, f"Persona zaten var: {data.id}")
    PERSONAS[data.id] = {
        "id":              data.id,
        "display_name":    data.display_name,
        "group":           data.group,
        "language":        data.language,
        "max_steps":       data.max_steps,
        "system_prompt":   data.system_prompt,
        "initial_context": data.initial_context,
        "scenario_hints":  data.scenario_hints,
    }
    if data.ground_truth:
        GROUND_TRUTH[data.id] = data.ground_truth
    return {"ok": True, "id": data.id}


@app.put("/api/personas/{persona_id}")
async def update_persona(persona_id: str, data: dict = Body(...)):
    if persona_id not in PERSONAS:
        raise HTTPException(404, f"Persona bulunamadı: {persona_id}")
    gt = data.pop("ground_truth", None)
    PERSONAS[persona_id].update(data)
    if gt is not None:
        GROUND_TRUTH[persona_id] = gt
    return {"ok": True}


@app.delete("/api/personas/{persona_id}")
async def delete_persona(persona_id: str):
    if persona_id not in PERSONAS:
        raise HTTPException(404, f"Persona bulunamadı: {persona_id}")
    del PERSONAS[persona_id]
    GROUND_TRUTH.pop(persona_id, None)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# PERSONAS.PY IMPORT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/personas/import")
async def import_personas_file(file: UploadFile = File(...)):
    """
    Bir personas.py dosyası yükle ve içindeki PERSONAS + GROUND_TRUTH'u
    mevcut in-memory dict'e aktar. Çakışan ID'ler güncellenir.
    """
    if not file.filename.endswith(".py"):
        raise HTTPException(400, "Sadece .py dosyası kabul edilir")

    content = await file.read()

    # Geçici modül olarak yükle
    tmp_path = Path("/tmp/_imported_personas.py")
    tmp_path.write_bytes(content)

    spec   = importlib.util.spec_from_file_location("_imported_personas", tmp_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise HTTPException(422, f"Dosya parse hatası: {e}")

    imported_personas     = getattr(module, "PERSONAS",     None)
    imported_ground_truth = getattr(module, "GROUND_TRUTH", None)

    if not imported_personas or not isinstance(imported_personas, dict):
        raise HTTPException(422, "PERSONAS dict bulunamadı")

    added   = []
    updated = []

    for pid, pdata in imported_personas.items():
        if pid in PERSONAS:
            updated.append(pid)
        else:
            added.append(pid)
        PERSONAS[pid] = pdata

    if imported_ground_truth and isinstance(imported_ground_truth, dict):
        for pid, gt in imported_ground_truth.items():
            GROUND_TRUTH[pid] = gt

    tmp_path.unlink(missing_ok=True)

    return {
        "ok":      True,
        "added":   added,
        "updated": updated,
        "total":   len(PERSONAS),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TEST ÇALIŞTIR — SSE STREAM
# ─────────────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    persona_ids: Optional[list[str]] = None
    group:       Optional[str]       = None


@app.post("/api/run")
async def start_run(req: RunRequest):
    persona_ids = req.persona_ids or []

    if req.group and not persona_ids:
        persona_ids = [
            p["id"] for p in PERSONAS.values() if p["group"] == req.group
        ]
    if not persona_ids:
        persona_ids = list(PERSONAS.keys())

    if not persona_ids:
        raise HTTPException(400, "Çalıştırılacak persona yok")

    unknown = [pid for pid in persona_ids if pid not in PERSONAS]
    if unknown:
        raise HTTPException(400, f"Bilinmeyen persona(lar): {unknown}")

    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    q: asyncio.Queue = asyncio.Queue()
    _stream_queues[run_id] = q

    asyncio.create_task(_run_with_stream(run_id, persona_ids, q))
    return {"run_id": run_id}


async def _run_with_stream(run_id: str, persona_ids: list[str], q: asyncio.Queue):

    async def emit(event_type: str, data: dict):
        await q.put({"type": event_type, "data": data})

    await emit("run_start", {
        "run_id":    run_id,
        "personas":  persona_ids,
        "total":     len(persona_ids),
        "mock_mode": config.MOCK_MODE,
        "timestamp": datetime.datetime.now().isoformat(),
    })

    all_results = []

    for idx, pid in enumerate(persona_ids):
        persona   = PERSONAS[pid]
        gt_list   = GROUND_TRUTH.get(pid, [])
        max_steps = persona.get("max_steps", config.MAX_STEPS_LONG)
        history:  list = []
        steps:    list = []

        await emit("persona_start", {
            "run_id":       run_id,
            "persona_id":   pid,
            "display_name": persona["display_name"],
            "group":        persona["group"],
            "index":        idx + 1,
            "total":        len(persona_ids),
        })

        for step_num in range(1, max_steps + 1):
            try:
                user_msg = await test_runner.persona_next_message(persona, history, step_num)
            except Exception as e:
                await emit("error", {"persona_id": pid, "step": step_num, "error": str(e)})
                break

            await emit("step_user", {
                "run_id": run_id, "persona_id": pid,
                "step": step_num, "message": user_msg,
            })

            try:
                olly_out = await test_runner.call_olly(user_msg, history, pid)
            except Exception as e:
                await emit("error", {"persona_id": pid, "step": step_num, "error": str(e)})
                break

            olly_action = olly_out.get("response_action", "")
            olly_text   = olly_out.get("response_text",   "")
            gt_step     = next((g for g in gt_list if g["step"] == step_num), None)
            eval_result = test_runner.evaluate_step(olly_out, gt_step)

            await emit("step_olly", {
                "run_id":       run_id,
                "persona_id":   pid,
                "step":         step_num,
                "action":       olly_action,
                "text":         olly_text,
                "step_score":   eval_result["step_score"],
                "scores": {
                    "action":      eval_result["action_score"],
                    "memory":      eval_result["memory_score"],
                    "intent":      eval_result["intent_score"],
                    "tone_safety": eval_result["tone_score"],
                },
                "notes":        eval_result["notes"],
                "ground_truth": gt_step,
            })

            steps.append({
                "step":         step_num,
                "user_message": user_msg,
                "olly_output":  olly_out,
                "olly_action":  olly_action,
                "eval":         eval_result,
            })

            history.append({"role": "user",      "content": user_msg})
            history.append({"role": "assistant", "content": olly_text})

            if olly_action == "close":
                break

        step_scores = [s["eval"]["step_score"] for s in steps]
        avg_step    = round(sum(step_scores) / len(step_scores), 1) if step_scores else 0
        retention   = test_runner.compute_retention(steps, max_steps)
        overall     = round(avg_step * config.WEIGHT_STEP_AVG + retention * config.WEIGHT_RETENTION, 1)

        persona_result = {
            "persona_id":      pid,
            "display_name":    persona["display_name"],
            "group":           persona["group"],
            "timestamp":       datetime.datetime.now().isoformat(),
            "mock_mode":       config.MOCK_MODE,
            "steps":           steps,
            "avg_step_score":  avg_step,
            "retention_score": retention,
            "overall_score":   overall,
            "total_steps":     len(steps),
        }
        all_results.append(persona_result)

        await emit("persona_done", {
            "run_id":          run_id,
            "persona_id":      pid,
            "display_name":    persona["display_name"],
            "overall_score":   overall,
            "retention_score": retention,
            "avg_step_score":  avg_step,
            "total_steps":     len(steps),
        })

    report      = test_runner.build_report(all_results)
    report_path = test_runner.save_report(report)

    await emit("run_done", {
        "run_id":          run_id,
        "report_file":     report_path.name,
        "summary":         report["summary"],
        "retention_zones": report["retention_zones"],
        "problem_steps":   report["problem_steps"][:10],
    })

    await test_runner.send_notifications(report)
    await q.put(None)


@app.get("/api/run/{run_id}/stream")
async def stream_run(run_id: str):
    if run_id not in _stream_queues:
        raise HTTPException(404, "run_id bulunamadı veya süresi doldu")

    q = _stream_queues[run_id]

    async def event_generator() -> AsyncGenerator:
        try:
            while True:
                item = await asyncio.wait_for(q.get(), timeout=300)
                if item is None:
                    yield {"event": "done", "data": json.dumps({"run_id": run_id})}
                    break
                yield {
                    "event": item["type"],
                    "data":  json.dumps(item["data"], ensure_ascii=False),
                }
        except asyncio.TimeoutError:
            yield {"event": "timeout", "data": "{}"}
        finally:
            _stream_queues.pop(run_id, None)

    return EventSourceResponse(event_generator())


# ─────────────────────────────────────────────────────────────────────────────
# RAPORLAR — /compare route'u /{filename}'den ÖNCE tanımlanmalı
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/reports")
async def list_reports():
    reports_dir = Path(config.REPORTS_DIR)
    if not reports_dir.exists():
        return []
    files  = sorted(reports_dir.glob("report_*.json"), reverse=True)
    result = []
    for f in files[:50]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            s    = data.get("summary", {})
            result.append({
                "filename":          f.name,
                "timestamp":         data.get("report_timestamp", ""),
                "mock_mode":         data.get("mock_mode", False),
                "total_personas":    s.get("total_personas", 0),
                "avg_overall_score": s.get("avg_overall_score", 0),
                "avg_retention":     s.get("avg_retention_score", 0),
                "critical_count":    s.get("critical_count", 0),
                "improvement_count": s.get("improvement_count", 0),
                "healthy_count":     s.get("healthy_count", 0),
            })
        except Exception:
            pass
    return result


# FIX: /compare MUST be defined before /{filename} to avoid route collision
@app.get("/api/reports/compare")
async def compare_reports(a: str, b: str):
    def load(name: str) -> dict:
        p = Path(config.REPORTS_DIR) / name
        if not p.exists():
            raise HTTPException(404, f"Rapor bulunamadı: {name}")
        return json.loads(p.read_text(encoding="utf-8"))

    ra, rb   = load(a), load(b)
    map_a    = {p["persona"]: p for p in ra.get("per_persona", [])}
    map_b    = {p["persona"]: p for p in rb.get("per_persona", [])}
    all_names = sorted(set(map_a) | set(map_b))

    diff = []
    for name in all_names:
        pa = map_a.get(name)
        pb = map_b.get(name)
        diff.append({
            "persona":         name,
            "overall_a":       pa["overall_score"]   if pa else None,
            "overall_b":       pb["overall_score"]   if pb else None,
            "overall_delta":   round(pb["overall_score"] - pa["overall_score"], 1) if pa and pb else None,
            "retention_a":     pa["retention_score"] if pa else None,
            "retention_b":     pb["retention_score"] if pb else None,
            "retention_delta": round(pb["retention_score"] - pa["retention_score"], 1) if pa and pb else None,
        })

    return {
        "report_a": {"filename": a, "timestamp": ra.get("report_timestamp"), "summary": ra.get("summary")},
        "report_b": {"filename": b, "timestamp": rb.get("report_timestamp"), "summary": rb.get("summary")},
        "diff":     diff,
    }


@app.get("/api/reports/{filename}")
async def get_report(filename: str):
    path = Path(config.REPORTS_DIR) / filename
    if not path.exists():
        raise HTTPException(404, "Rapor bulunamadı")
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    action:      str
    day:         str            = "monday"
    time:        str            = "09:00"
    persona_ids: Optional[list[str]] = None


@app.get("/api/schedule/status")
async def schedule_status():
    # task objesi döndürülmez — sadece serializable alanlar
    return {
        "active": _scheduler_state["active"],
        "day":    _scheduler_state["day"],
        "time":   _scheduler_state["time"],
    }


@app.post("/api/schedule")
async def set_schedule(req: ScheduleRequest):
    global _scheduler_state, _scheduler_task

    if req.action == "stop":
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
        _scheduler_state = {"active": False, "day": None, "time": None}
        _scheduler_task  = None
        return {"ok": True, "status": "stopped"}

    if req.action == "set":
        valid_days = {
            "monday","tuesday","wednesday","thursday",
            "friday","saturday","sunday","daily",
        }
        if req.day not in valid_days:
            raise HTTPException(400, f"Geçersiz gün: {req.day}")

        async def _scheduled():
            while True:
                now   = datetime.datetime.now()
                h, m  = map(int, req.time.split(":"))
                today = now.replace(hour=h, minute=m, second=0, microsecond=0)

                if req.day == "daily":
                    next_run = today if today > now else today + datetime.timedelta(days=1)
                else:
                    day_map = {
                        "monday":0,"tuesday":1,"wednesday":2,"thursday":3,
                        "friday":4,"saturday":5,"sunday":6,
                    }
                    diff     = (day_map[req.day] - now.weekday()) % 7
                    if diff == 0 and today <= now:
                        diff = 7
                    next_run = today + datetime.timedelta(days=diff)

                wait_secs = (next_run - datetime.datetime.now()).total_seconds()
                await asyncio.sleep(max(wait_secs, 0))
                await test_runner.run_and_report(req.persona_ids)

        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()

        _scheduler_task  = asyncio.create_task(_scheduled())
        _scheduler_state = {"active": True, "day": req.day, "time": req.time}
        return {"ok": True, "status": "set", "day": req.day, "time": req.time}

    raise HTTPException(400, "action 'set' veya 'stop' olmalı")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    for w in config.validate():
        print(f"⚠️  {w}")
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
