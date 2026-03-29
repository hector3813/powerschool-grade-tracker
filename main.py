from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import uvicorn
import asyncio
import uuid
import json

from powerschool_client import PowerSchoolClient

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# In-memory store: task_id → result (or error)
_tasks: dict = {}


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/login", response_class=HTMLResponse)
async def start_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "loading", "result": None, "error": None}

    async def fetch_grades():
        client = PowerSchoolClient()
        try:
            await client.start()
            await client.login(username, password)
            grades = await client.get_grades()
            student_name = await client.get_student_name()
            gpa = client.calculate_gpa(grades)
            _tasks[task_id] = {
                "status": "done",
                "result": {
                    "student_name": student_name,
                    "grades": grades,
                    "gpa": gpa,
                },
                "error": None,
            }
        except Exception as e:
            _tasks[task_id] = {"status": "error", "result": None, "error": str(e)}
        finally:
            await client.close()

    asyncio.create_task(fetch_grades())

    return templates.TemplateResponse(request=request, name="loading.html", context={
        "task_id": task_id
    })


@app.get("/status/{task_id}")
async def check_status(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return {"status": "error", "error": "Task not found"}
    return {"status": task["status"], "error": task.get("error")}


@app.get("/result/{task_id}", response_class=HTMLResponse)
async def show_result(request: Request, task_id: str):
    task = _tasks.pop(task_id, None)
    if not task or task["status"] != "done":
        return templates.TemplateResponse(request=request, name="index.html", context={
            "error": task.get("error", "Something went wrong.") if task else "Session expired."
        })
    r = task["result"]
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "student_name": r["student_name"],
        "grades": r["grades"],
        "gpa": r["gpa"],
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
