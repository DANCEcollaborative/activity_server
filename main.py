"""
Activity Server – FastAPI application.

Changes vs. previous version
─────────────────────────────
• UserSubmission now requires only `email` + `activity_id`.
  Fields username, name, prequiz_token, postquiz_token have been removed.
• /api/submit accepts only: email, activity, notebook.
• All lookup / download / score endpoints use email instead of username.
• Dashboard displays email in place of username/name columns.
"""

import asyncio
import os
import subprocess
import tempfile
from datetime import datetime

from fastapi import (Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from models import Activity, Base, Instructor, Notebook, UserSubmission

# ──────────────────────────────────────────────
# DB setup
# ──────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL",
                         "postgresql://activity_user:activity_pass@db:5432/activity_db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base.metadata.create_all(bind=engine)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

app = FastAPI()


# ──────────────────────────────────────────────
# Health check  (stops the 404 flood from Docker healthcheck)
# ──────────────────────────────────────────────

@app.get("/")
async def health():
    return {"status": "ok"}


# ──────────────────────────────────────────────
# Dependency
# ──────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def verify_google_token(token: str) -> dict:
    """Verify a Google ID token and return its claims."""
    return id_token.verify_oauth2_token(
        token, google_requests.Request(), GOOGLE_CLIENT_ID
    )


def require_instructor(request: Request, db: Session) -> Instructor:
    """Raise 401 / 403 if the request does not carry a valid instructor token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = auth.split(" ", 1)[1]
    claims = verify_google_token(token)
    email = claims.get("email", "")
    instructor = db.query(Instructor).filter(Instructor.email == email).first()
    if not instructor:
        raise HTTPException(status_code=403, detail="Not an instructor")
    return instructor


# ──────────────────────────────────────────────
# Background grading
# ──────────────────────────────────────────────

import logging
logger = logging.getLogger("grader")


def _write_grading_result(notebook_id: int, score: float, feedback: str):
    """Persist score + feedback to the Notebook row."""
    db = SessionLocal()
    try:
        nb_row = db.query(Notebook).filter(Notebook.id == notebook_id).first()
        if nb_row:
            nb_row.score    = score
            nb_row.feedback = feedback
            db.commit()
    finally:
        db.close()


def _to_bytes(value) -> bytes:
    """
    Coerce a notebook value from the DB into real bytes.
    PostgreSQL bytea columns can come back as:
      - bytes       (psycopg2 normal path)
      - memoryview  (some driver versions)
      - str/bytes starting with \\x  (hex-escaped bytea)
      - plain str   (legacy rows stored as text)
    """
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, bytes):
        if value.startswith(b'\\x'):
            return bytes.fromhex(value[2:].decode('ascii'))
        return value
    if isinstance(value, str):
        if value.startswith('\\x'):
            return bytes.fromhex(value[2:])
        return value.encode('utf-8')
    raise TypeError(f"Cannot convert {type(value)} to bytes")


async def run_grader(notebook_id: int, notebook_bytes: bytes,
                     notebook_filename: str, task_graders_path: str):
    """
    Write the submitted notebook to a temp file, call grader.py, then store
    the returned score and feedback back into the Notebook row.
    """
    grader_script = os.path.join(
        os.path.dirname(__file__), "grading", "grader", "grader.py"
    )

    logger.info(f"[grader] starting notebook_id={notebook_id} "
                f"grader={grader_script} task_graders={task_graders_path}")

    if not os.path.isfile(grader_script):
        msg = f"grader.py not found at {grader_script}"
        logger.error(f"[grader] {msg}")
        _write_grading_result(notebook_id, 0.0, f"Configuration error: {msg}")
        return

    if not os.path.isdir(task_graders_path):
        msg = f"task_graders directory not found: {task_graders_path}"
        logger.error(f"[grader] {msg}")
        _write_grading_result(notebook_id, 0.0, f"Configuration error: {msg}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        nb_path = os.path.join(tmpdir, notebook_filename or "submission.ipynb")
        with open(nb_path, "wb") as f:
            f.write(_to_bytes(notebook_bytes))

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["python", grader_script, nb_path, task_graders_path],
                capture_output=True,
                text=True,
                timeout=120,
            )
            logger.info(f"[grader] notebook_id={notebook_id} returncode={result.returncode}")
            if result.stderr:
                logger.warning(f"[grader] stderr: {result.stderr[:500]}")

            # Parse sentinel-delimited output:
            #   GRADER_SCORE:<float>
            #   GRADER_FEEDBACK_START
            #   <feedback text>
            #   GRADER_FEEDBACK_END
            stdout = result.stdout
            score    = 0.0
            feedback = "No feedback."
            for line in stdout.splitlines():
                if line.startswith("GRADER_SCORE:"):
                    try:
                        score = float(line[len("GRADER_SCORE:"):].strip())
                    except ValueError:
                        pass
                    break
            if "GRADER_FEEDBACK_START" in stdout and "GRADER_FEEDBACK_END" in stdout:
                fb_start = stdout.index("GRADER_FEEDBACK_START") + len("GRADER_FEEDBACK_START\n")
                fb_end   = stdout.index("GRADER_FEEDBACK_END")
                feedback = stdout[fb_start:fb_end].strip()

        except Exception as exc:
            score    = 0.0
            feedback = (f"Grading error: {exc}\n"
                        f"{result.stderr if 'result' in dir() else ''}")
            logger.error(f"[grader] exception for notebook_id={notebook_id}: {exc}")

    _write_grading_result(notebook_id, score, feedback)
    logger.info(f"[grader] done notebook_id={notebook_id} score={score}")


# ──────────────────────────────────────────────
# Manual grading trigger  (for testing / recovery)
# ──────────────────────────────────────────────

@app.post("/api/grade/{notebook_id}")
async def trigger_grading(notebook_id: int, db: Session = Depends(get_db)):
    """
    Re-run grading for an existing notebook submission.
    Useful for testing and for recovering submissions that were never graded.
    """
    nb_row = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not nb_row:
        raise HTTPException(status_code=404, detail="Notebook not found")

    submission = db.query(UserSubmission).filter(
        UserSubmission.id == nb_row.user_submission_id
    ).first()
    activity = db.query(Activity).filter(
        Activity.activity_id == submission.activity_id
    ).first()

    if not activity or not activity.task_graders:
        raise HTTPException(status_code=400,
                            detail="Activity has no task_graders path configured")

    if not nb_row.notebook:
        raise HTTPException(status_code=400, detail="No notebook content stored")

    nb_bytes = _to_bytes(nb_row.notebook)

    asyncio.create_task(
        run_grader(notebook_id, nb_bytes,
                   nb_row.notebook_filename or "submission.ipynb",
                   activity.task_graders)
    )
    return {"status": "grading started", "notebook_id": notebook_id}


# ──────────────────────────────────────────────
# Activity endpoints
# ──────────────────────────────────────────────

@app.post("/api/activity")
async def create_or_update_activity(
    activity_id:   str  = Form(...),
    activity_name: str  = Form(...),
    enabled:       bool = Form(True),
    task_graders:  str  = Form(None),   # local path to grader directory
    db: Session = Depends(get_db),
):
    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()

    if activity:
        activity.activity_name = activity_name
        activity.enabled       = enabled
        if task_graders is not None:
            activity.task_graders = task_graders
    else:
        activity = Activity(
            activity_id=activity_id,
            activity_name=activity_name,
            enabled=enabled,
            task_graders=task_graders,
        )
        db.add(activity)

    db.commit()
    return {"status": "ok", "activity_id": activity_id}


@app.delete("/api/activity/{activity_id}")
async def delete_activity(activity_id: str, db: Session = Depends(get_db)):
    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    db.delete(activity)
    db.commit()
    return {"status": "deleted"}


@app.get("/api/activities")
async def list_activities(enabled_only: bool = False,
                          db: Session = Depends(get_db)):
    q = db.query(Activity)
    if enabled_only:
        q = q.filter(Activity.enabled == True)
    activities = q.all()
    return [
        {
            "activity_id":   a.activity_id,
            "activity_name": a.activity_name,
            "enabled":       a.enabled,
            "task_graders":  a.task_graders,
        }
        for a in activities
    ]


@app.get("/api/activities/by-email/{email}")
async def activities_by_email(email: str, db: Session = Depends(get_db)):
    activities = db.query(Activity).filter(Activity.enabled == True).all()
    return [
        {"activity_id": a.activity_id, "activity_name": a.activity_name}
        for a in activities
    ]


# ──────────────────────────────────────────────
# Instructor endpoints
# ──────────────────────────────────────────────

class InstructorCreate(BaseModel):
    email: str
    name: str = None
    activity_id: str


@app.post("/api/instructor")
async def add_instructor(data: InstructorCreate, db: Session = Depends(get_db)):
    instructor = db.query(Instructor).filter(
        Instructor.email == data.email
    ).first()
    if not instructor:
        instructor = Instructor(email=data.email, name=data.name)
        db.add(instructor)
        db.flush()
    elif data.name:
        instructor.name = data.name

    activity = db.query(Activity).filter(
        Activity.activity_id == data.activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    if activity not in instructor.activities:
        instructor.activities.append(activity)

    db.commit()
    return {"status": "ok", "instructor_id": instructor.id}


# ──────────────────────────────────────────────
# Submission endpoint
# ──────────────────────────────────────────────

@app.post("/api/submit")
async def submit_notebook(
    email:    str        = Form(...),
    activity: str        = Form(...),
    notebook: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    act = db.query(Activity).filter(Activity.activity_id == activity).first()
    if not act:
        raise HTTPException(status_code=404, detail="Activity not found")

    notebook_content = await notebook.read()

    # Upsert UserSubmission keyed on (email, activity_id)
    existing = db.query(UserSubmission).filter(
        UserSubmission.activity_id == activity,
        UserSubmission.email       == email,
    ).first()

    if not existing:
        existing = UserSubmission(
            activity_id=activity,
            email=email,
        )
        db.add(existing)
        db.flush()

    # Always create a new Notebook row
    new_notebook = Notebook(
        user_submission_id=existing.id,
        notebook=notebook_content,
        notebook_filename=notebook.filename,
        submitted_at=datetime.utcnow().isoformat(),
        score=None,
        feedback=None,
    )
    db.add(new_notebook)
    db.commit()
    db.refresh(new_notebook)
    notebook_id = new_notebook.id

    # Launch grading asynchronously if task_graders is configured
    if act.task_graders:
        asyncio.create_task(
            run_grader(notebook_id, notebook_content,
                       notebook.filename, act.task_graders)
        )

    return {"status": "submitted", "notebook_id": notebook_id}


# ──────────────────────────────────────────────
# Score / feedback manual override
# ──────────────────────────────────────────────

class ScoreUpdate(BaseModel):
    activity_id: str
    email: str
    score: float
    feedback: str = None


@app.put("/api/score")
async def update_score(data: ScoreUpdate, db: Session = Depends(get_db)):
    submission = db.query(UserSubmission).filter(
        UserSubmission.activity_id == data.activity_id,
        UserSubmission.email       == data.email,
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    latest = (
        db.query(Notebook)
        .filter(Notebook.user_submission_id == submission.id)
        .order_by(Notebook.submitted_at.desc())
        .first()
    )
    if not latest:
        raise HTTPException(status_code=404, detail="No notebook found")

    latest.score = data.score
    if data.feedback is not None:
        latest.feedback = data.feedback
    db.commit()
    return {"status": "updated"}


# ──────────────────────────────────────────────
# Download endpoints
# ──────────────────────────────────────────────

@app.get("/download/{activity_id}/{email:path}")
async def download_notebook(
    activity_id: str,
    email: str,
    notebook_id: int = None,
    db: Session = Depends(get_db),
):
    submission = db.query(UserSubmission).filter(
        UserSubmission.activity_id == activity_id,
        UserSubmission.email       == email,
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    if notebook_id:
        nb = db.query(Notebook).filter(
            Notebook.id                 == notebook_id,
            Notebook.user_submission_id == submission.id,
        ).first()
    else:
        nb = (
            db.query(Notebook)
            .filter(Notebook.user_submission_id == submission.id)
            .order_by(Notebook.submitted_at.desc())
            .first()
        )

    if not nb or not nb.notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    safe_email = email.replace("@", "_at_").replace(".", "_")
    filename = nb.notebook_filename or f"{safe_email}_{activity_id}.ipynb"
    content  = _to_bytes(nb.notebook)
    return StreamingResponse(
        iter([content]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download-feedback/{activity_id}/{email:path}")
async def download_feedback(
    activity_id: str,
    email: str,
    notebook_id: int = None,
    db: Session = Depends(get_db),
):
    submission = db.query(UserSubmission).filter(
        UserSubmission.activity_id == activity_id,
        UserSubmission.email       == email,
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    if notebook_id:
        nb = db.query(Notebook).filter(
            Notebook.id                 == notebook_id,
            Notebook.user_submission_id == submission.id,
        ).first()
    else:
        nb = (
            db.query(Notebook)
            .filter(Notebook.user_submission_id == submission.id)
            .order_by(Notebook.submitted_at.desc())
            .first()
        )

    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")

    feedback_text = nb.feedback or "No feedback available."
    safe_email = email.replace("@", "_at_").replace(".", "_")
    filename = f"feedback_{safe_email}_{activity_id}.txt"
    return StreamingResponse(
        iter([feedback_text.encode()]),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ──────────────────────────────────────────────
# Instructor Dashboard
# ──────────────────────────────────────────────

DASHBOARD_CSS = """
<style>
  body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; }
  header { background: #1a73e8; color: white; padding: 16px 24px;
           display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 1.3rem; }
  .container { max-width: 1100px; margin: 32px auto; padding: 0 16px; }
  .activity-card { background: white; border-radius: 8px; margin-bottom: 32px;
                   box-shadow: 0 1px 4px rgba(0,0,0,.12); }
  .activity-card h2 { margin: 0; padding: 16px 20px;
                      border-bottom: 1px solid #e0e0e0; font-size: 1.1rem; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 10px 16px; text-align: left;
           border-bottom: 1px solid #f0f0f0; font-size: .9rem; }
  th { background: #f8f9fa; font-weight: 600; color: #555; }
  a.btn { display: inline-block; padding: 4px 10px; border-radius: 4px;
          font-size: .8rem; text-decoration: none; margin-right: 4px; }
  a.btn-dl   { background: #1a73e8; color: white; }
  a.btn-fb   { background: #34a853; color: white; }
  .badge-grading { color: #f9a825; font-style: italic; }
  .badge-score   { font-weight: bold; }
</style>
"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    # ── Auth ──────────────────────────────────────────────────────────
    token = request.cookies.get("google_token")
    if not token:
        return HTMLResponse(_signin_page())

    try:
        claims     = verify_google_token(token)
        email      = claims.get("email", "")
        instructor = db.query(Instructor).filter(
            Instructor.email == email
        ).first()
        if not instructor:
            return HTMLResponse("<h2>Access denied – not an instructor.</h2>",
                                status_code=403)
    except Exception:
        return HTMLResponse(_signin_page())

    # ── Build HTML ────────────────────────────────────────────────────
    activities = instructor.activities
    cards = ""
    for act in activities:
        rows = ""
        submissions = db.query(UserSubmission).filter(
            UserSubmission.activity_id == act.activity_id
        ).all()

        for sub in submissions:
            notebooks = (
                db.query(Notebook)
                .filter(Notebook.user_submission_id == sub.id)
                .order_by(Notebook.submitted_at.desc())
                .all()
            )
            if not notebooks:
                continue

            latest           = notebooks[0]
            submission_count = len(notebooks)

            if latest.score is None:
                score_cell = '<span class="badge-grading">Grading…</span>'
            else:
                score_cell = f'<span class="badge-score">{latest.score:.2f}</span>'

            dl_url = (f"/download/{act.activity_id}/{sub.email}"
                      f"?notebook_id={latest.id}")
            fb_url = (f"/download-feedback/{act.activity_id}/{sub.email}"
                      f"?notebook_id={latest.id}")

            feedback_btn = (
                f'<a class="btn btn-fb" href="{fb_url}">Feedback</a>'
                if latest.feedback
                else '<span style="color:#aaa;font-size:.8rem">—</span>'
            )

            rows += f"""
            <tr>
              <td>{sub.email}</td>
              <td>{submission_count}</td>
              <td>{latest.submitted_at or ''}</td>
              <td>{score_cell}</td>
              <td>
                <a class="btn btn-dl" href="{dl_url}">Download</a>
                {feedback_btn}
              </td>
            </tr>"""

        cards += f"""
        <div class="activity-card">
          <h2>{act.activity_name} <small style="color:#888;font-size:.8rem">
              ({act.activity_id})</small></h2>
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Submissions</th><th>Latest Submitted</th>
                <th>Latest Score</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="5" style="color:#aaa">No submissions yet</td></tr>'}</tbody>
          </table>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Instructor Dashboard</title>{DASHBOARD_CSS}</head>
<body>
  <header>
    <h1>📚 Instructor Dashboard</h1>
    <span style="margin-left:auto">{instructor.name or email}</span>
  </header>
  <div class="container">{cards or '<p>No activities assigned.</p>'}</div>
</body>
</html>"""
    return HTMLResponse(html)


def _signin_page() -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Sign In</title>
<script src="https://accounts.google.com/gsi/client" async defer></script>
</head>
<body style="display:flex;align-items:center;justify-content:center;height:100vh;
             font-family:Arial,sans-serif;background:#f4f6f8">
  <div style="text-align:center">
    <h2>Instructor Dashboard</h2>
    <div id="g_id_onload"
         data-client_id="{GOOGLE_CLIENT_ID}"
         data-callback="handleCredential"
         data-auto_prompt="false"></div>
    <div class="g_id_signin" data-type="standard"></div>
  </div>
  <script>
    function handleCredential(response) {{
      document.cookie = "google_token=" + response.credential + ";path=/";
      window.location.reload();
    }}
  </script>
</body>
</html>"""
