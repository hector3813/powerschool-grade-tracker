from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import uvicorn

from powerschool_client import PowerSchoolClient

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    client = PowerSchoolClient()
    try:
        await client.start()
        await client.login(username, password)
        grades = await client.get_grades()
        student_name = await client.get_student_name()
    except Exception as e:
        return templates.TemplateResponse(request=request, name="index.html", context={
            "error": str(e)
        })
    finally:
        await client.close()

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "student_name": student_name,
        "grades": grades,
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
