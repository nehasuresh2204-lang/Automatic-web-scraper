"""
Web Scraper Agent — FastAPI Backend
Exposes the agent via HTTP with Server-Sent Events for live progress streaming.
"""

import asyncio
import concurrent.futures
import io
import json
import os
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pandas as pd

# Load .env from project root before anything else
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Add parent directory to path so we can import agent.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import run_agent

# ─── API key (loaded from .env / environment) ─────────────────────────────────

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


# ─── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="Web Scraper Agent API")

@app.on_event("startup")
async def startup_check():
    if not os.environ.get("GROQ_API_KEY"):
        print()
        print("  WARNING: GROQ_API_KEY is not set.")
        print("  Create a .env file in the project root with:")
        print("    GROQ_API_KEY=gsk_your_key_here")
        print()
    else:
        print("  GROQ_API_KEY loaded.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend folder as static files at root
frontend_path = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


# ─── Request / Response models ────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    query: str


class ExportRequest(BaseModel):
    data: list[dict]
    format: str  # "csv" | "excel" | "json"
    filename: str = "results"


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    """Serve the frontend index.html at root."""
    return FileResponse(str(frontend_path / "index.html"))


@app.post("/api/scrape/stream")
async def scrape_stream(body: ScrapeRequest, request: Request):
    """
    Runs the scraping agent and streams progress via Server-Sent Events.

    SSE message format:
      data: {"type": "log",    "message": "..."}
      data: {"type": "result", "status": "...", "data": [...], "visited": [...], "reasoning": "..."}
      data: {"type": "error",  "message": "..."}
      data: {"type": "done"}
    """

    # Auto-fix bare URLs (no scheme)
    url = body.url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url

    api_key = GROQ_API_KEY
    if not api_key:
        async def no_key():
            yield _sse({"type": "error", "message": "GROQ_API_KEY not set. Add it to your .env file in the project root."})
            yield _sse({"type": "done"})
        return StreamingResponse(no_key(), media_type="text/event-stream")

    # Queue bridges the agent thread and the FastAPI async generator
    queue: asyncio.Queue = asyncio.Queue()
    main_loop = asyncio.get_event_loop()

    def progress_cb(msg: str):
        """
        Called from the agent's background thread.
        Uses call_soon_threadsafe to safely post into the main event loop's queue.
        """
        main_loop.call_soon_threadsafe(
            queue.put_nowait, {"type": "log", "message": msg}
        )

    def run_agent_in_thread():
        """
        Runs the agent in a dedicated thread with its own event loop.
        On Windows, Playwright requires ProactorEventLoop and cannot share
        the loop that uvicorn/FastAPI is already running on.
        """
        # Create a brand-new ProactorEventLoop for this thread (Windows-safe)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_agent(url, body.query, api_key, progress_cb)
            )
        except Exception as e:
            result = {"status": "error", "data": [], "visited": [], "reasoning": str(e)}
        finally:
            loop.close()
        # Signal completion by posting the result into the queue
        main_loop.call_soon_threadsafe(queue.put_nowait, {"type": "result", **result})
        main_loop.call_soon_threadsafe(queue.put_nowait, {"type": "done"})

    async def event_generator():
        # Start the agent in a background thread
        thread = threading.Thread(target=run_agent_in_thread, daemon=True)
        thread.start()

        # Stream messages until we receive the "done" sentinel
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=0.4)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            if msg["type"] == "done":
                yield _sse(msg)
                break

            if msg["type"] == "result":
                yield _sse({
                    "type":      "result",
                    "status":    msg.get("status", "error"),
                    "data":      msg.get("data", []),
                    "visited":   msg.get("visited", []),
                    "reasoning": msg.get("reasoning", ""),
                })
                continue

            # log or error messages
            yield _sse(msg)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/export")
async def export_data(body: ExportRequest):
    """
    Converts extracted data to CSV, Excel, or JSON and returns as file download.
    """
    if not body.data:
        return JSONResponse({"error": "No data to export"}, status_code=400)

    df = pd.DataFrame(body.data)
    fmt = body.format.lower()

    if fmt == "csv":
        content = df.to_csv(index=False).encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{body.filename}.csv"'},
        )

    elif fmt == "excel":
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{body.filename}.xlsx"'},
        )

    elif fmt == "json":
        content = json.dumps(body.data, ensure_ascii=False, indent=2).encode("utf-8")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{body.filename}.json"'},
        )

    return JSONResponse({"error": "Invalid format. Use csv, excel, or json."}, status_code=400)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ─── SSE helper ───────────────────────────────────────────────────────────────

def _sse(payload: dict) -> str:
    """Formats a dict as an SSE data line."""
    return f"data: {json.dumps(payload)}\n\n"


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
