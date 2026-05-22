"""
Activity Server – FastAPI application.

Schema
──────
• users / user_activities / submissions tables are the source of truth.
• Legacy user_submissions / notebooks tables have been removed.

Endpoints
─────────
POST   /api/user                        – create / update a user
POST   /api/user/activity               – enroll a user in an activity
POST   /api/user/room                   – set / override room_name for an enrollment
POST   /api/user/submit                 – submit a notebook (auto-graded)
GET    /api/user/{email}/activities     – list enabled activities for a user

POST   /api/submit                      – backward-compatible submit
POST   /api/grade/{submission_id}       – re-trigger grading
PUT    /api/score                       – manual score / feedback override

POST   /api/activity                    – create / update an activity
DELETE /api/activity/{activity_id}      – delete an activity
GET    /api/activities                  – list activities

POST   /api/instructor                  – add instructor / assign activity

GET    /download/{activity_id}/{email}  – download latest (or specific) notebook
GET    /download-feedback/{activity_id}/{email} – download latest feedback

GET    /dashboard                       – instructor dashboard (Google auth)
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from datetime import datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models import Activity, Base, Instructor, Submission, User, UserActivity

# ──────────────────────────────────────────────
# DB setup
# ──────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://activity_user:activity_pass@db:5432/activity_db",
)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base.metadata.create_all(bind=engine)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

app = FastAPI()
logger = logging.getLogger("grader")


# ──────────────────────────────────────────────
# Health check
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
        if value.startswith(b"\\x"):
            return bytes.fromhex(value[2:].decode("ascii"))
        return value
    if isinstance(value, str):
        if value.startswith("\\x"):
            return bytes.fromhex(value[2:])
        return value.encode("utf-8")
    raise TypeError(f"Cannot convert {type(value)} to bytes")


# ──────────────────────────────────────────────
# Background grading
# ──────────────────────────────────────────────

def _write_grading_result(submission_id: int, score: float, feedback: str):
    """Persist score + feedback to the graded Submission row, then create a
    mirrored Submission row for every other user that shares the same
    room_name and activity_id, so their submission count and results stay
    in sync with the submitting user."""
    db = SessionLocal()
    try:
        row = db.query(Submission).filter(Submission.id == submission_id).first()
        if not row:
            return
        row.score = score
        row.feedback = feedback
        db.commit()

        # ── Room-mate propagation ─────────────────────────────────────
        # Find the UserActivity that owns this submission.
        ua = db.query(UserActivity).filter(
            UserActivity.id == row.user_activity_id
        ).first()

        if not ua or not ua.room_name:
            # No room assigned – nothing to propagate.
            return

        # Find all other enrollments in the same activity + room.
        roommates = (
            db.query(UserActivity)
            .filter(
                UserActivity.activity_id == ua.activity_id,
                UserActivity.room_name == ua.room_name,
                UserActivity.id != ua.id,
            )
            .all()
        )

        for rm in roommates:
            mirrored = Submission(
                user_activity_id=rm.id,
                notebook=row.notebook,
                notebook_filename=row.notebook_filename,
                submitted_at=row.submitted_at,
                score=score,
                feedback=feedback,
            )
            db.add(mirrored)
            logger.info(
                f"[grader] room mirror: new submission for user_activity_id={rm.id} "
                f"score={score} submitted_at={row.submitted_at} "
                f"via room '{ua.room_name}'"
            )

        if roommates:
            db.commit()

    finally:
        db.close()


async def run_grader(submission_id: int, notebook_bytes: bytes,
                     notebook_filename: str, task_graders_path: str):
    """
    Write the submitted notebook to a temp file, call grader.py, then store
    the returned score and feedback back into the Submission row.
    """
    grader_script = os.path.join(
        os.path.dirname(__file__), "grading", "grader", "grader.py"
    )

    logger.info(
        f"[grader] starting submission_id={submission_id} "
        f"grader={grader_script} task_graders={task_graders_path}"
    )

    if not os.path.isfile(grader_script):
        msg = f"grader.py not found at {grader_script}"
        logger.error(f"[grader] {msg}")
        _write_grading_result(submission_id, 0.0, f"Configuration error: {msg}")
        return

    if not os.path.isdir(task_graders_path):
        msg = f"task_graders directory not found: {task_graders_path}"
        logger.error(f"[grader] {msg}")
        _write_grading_result(submission_id, 0.0, f"Configuration error: {msg}")
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
            logger.info(
                f"[grader] submission_id={submission_id} returncode={result.returncode}"
            )
            if result.stderr:
                logger.warning(f"[grader] stderr: {result.stderr[:500]}")

            # Parse sentinel-delimited output:
            #   GRADER_SCORE:<float>
            #   GRADER_FEEDBACK_START
            #   <feedback text>
            #   GRADER_FEEDBACK_END
            stdout = result.stdout
            score = 0.0
            feedback = "No feedback."
            for line in stdout.splitlines():
                if line.startswith("GRADER_SCORE:"):
                    try:
                        score = float(line[len("GRADER_SCORE:"):].strip())
                    except ValueError:
                        pass
                    break
            if "GRADER_FEEDBACK_START" in stdout and "GRADER_FEEDBACK_END" in stdout:
                fb_start = (
                    stdout.index("GRADER_FEEDBACK_START") + len("GRADER_FEEDBACK_START\n")
                )
                fb_end = stdout.index("GRADER_FEEDBACK_END")
                feedback = stdout[fb_start:fb_end].strip()

        except Exception as exc:
            score = 0.0
            feedback = (
                f"Grading error: {exc}\n"
                f"{result.stderr if 'result' in dir() else ''}"
            )
            logger.error(f"[grader] exception for submission_id={submission_id}: {exc}")

    _write_grading_result(submission_id, score, feedback)
    logger.info(f"[grader] done submission_id={submission_id} score={score}")


# ──────────────────────────────────────────────
# Manual grading trigger
# ──────────────────────────────────────────────

@app.post("/api/grade/{submission_id}")
async def trigger_grading(submission_id: int, db: Session = Depends(get_db)):
    """Re-run grading for an existing submission."""
    sub_row = db.query(Submission).filter(Submission.id == submission_id).first()
    if not sub_row:
        raise HTTPException(status_code=404, detail="Submission not found")

    user_activity = db.query(UserActivity).filter(
        UserActivity.id == sub_row.user_activity_id
    ).first()
    activity = db.query(Activity).filter(
        Activity.activity_id == user_activity.activity_id
    ).first()

    if not activity or not activity.task_graders:
        raise HTTPException(
            status_code=400, detail="Activity has no task_graders path configured"
        )

    if not sub_row.notebook:
        raise HTTPException(status_code=400, detail="No notebook content stored")

    nb_bytes = _to_bytes(sub_row.notebook)
    asyncio.create_task(
        run_grader(
            submission_id,
            nb_bytes,
            sub_row.notebook_filename or "submission.ipynb",
            activity.task_graders,
        )
    )
    return {"status": "grading started", "submission_id": submission_id}


# ──────────────────────────────────────────────
# Activity endpoints
# ──────────────────────────────────────────────

@app.post("/api/activity")
async def create_or_update_activity(
    activity_id: str = Form(...),
    activity_name: str = Form(...),
    enabled: bool = Form(True),
    task_graders: str = Form(None),
    db: Session = Depends(get_db),
):
    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()

    if activity:
        activity.activity_name = activity_name
        activity.enabled = enabled
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
async def list_activities(
    enabled_only: bool = False, db: Session = Depends(get_db)
):
    q = db.query(Activity)
    if enabled_only:
        q = q.filter(Activity.enabled == True)
    return [
        {
            "activity_id": a.activity_id,
            "activity_name": a.activity_name,
            "enabled": a.enabled,
            "task_graders": a.task_graders,
        }
        for a in q.all()
    ]


@app.get("/api/activities/by-email/{email:path}")
async def activities_by_email(email: str, db: Session = Depends(get_db)):
    """
    Return [{activity_id, activity_name}] for all enabled activities that
    the given user is enrolled in.  If the user does not exist in the users
    table, returns an empty list (no error) so callers can check enrollment
    before prompting for registration.

    Example:
        curl https://bazaar.lti.cs.cmu.edu/api/activities/by-email/chas.murray@gmail.com
    """
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return []

    result = []
    for ua in user.activities:
        act = db.query(Activity).filter(
            Activity.activity_id == ua.activity_id,
            Activity.enabled == True,
        ).first()
        if act:
            result.append({
                "activity_id": act.activity_id,
                "activity_name": act.activity_name,
            })
    return result


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
# User endpoints
# ──────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    email: str
    username: str = None


@app.post("/api/user")
async def create_user(data: UserCreate, db: Session = Depends(get_db)):
    """Create a user. Required: name, email. Optional: username.
    If a user with the same email already exists, updates and returns it."""
    user = db.query(User).filter(User.email == data.email).first()
    if user:
        if data.name:
            user.name = data.name
        if data.username is not None:
            user.username = data.username
        db.commit()
        db.refresh(user)
        return {"status": "updated", "user_id": user.id}

    user = User(name=data.name, email=data.email, username=data.username)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"status": "created", "user_id": user.id}


class UserActivityEnroll(BaseModel):
    email: str
    activity_id: str
    password: str = None
    prequiz_token: str = None
    postquiz_token: str = None


@app.post("/api/user/activity")
async def add_user_activity(
    data: UserActivityEnroll, db: Session = Depends(get_db)
):
    """Enroll a user in an activity. Required: email, activity_id."""
    user = db.query(User).filter(User.email == data.email).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found – create the user first via POST /api/user",
        )

    activity = db.query(Activity).filter(
        Activity.activity_id == data.activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == data.activity_id,
    ).first()

    if ua:
        if data.password is not None:
            ua.password = data.password
        if data.prequiz_token is not None:
            ua.prequiz_token = data.prequiz_token
        if data.postquiz_token is not None:
            ua.postquiz_token = data.postquiz_token
        db.commit()
        return {"status": "updated", "user_activity_id": ua.id}

    ua = UserActivity(
        user_id=user.id,
        activity_id=data.activity_id,
        password=data.password,
        prequiz_token=data.prequiz_token,
        postquiz_token=data.postquiz_token,
    )
    db.add(ua)
    db.commit()
    db.refresh(ua)
    return {"status": "enrolled", "user_activity_id": ua.id}


class UserRoomUpdate(BaseModel):
    activity_id: str
    email: str
    room_name: str


@app.post("/api/user/room")
async def update_user_room(data: UserRoomUpdate, db: Session = Depends(get_db)):
    """Set or override the room_name for a user's enrollment in an activity."""
    user = db.query(User).filter(User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == data.activity_id,
    ).first()
    if not ua:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    ua.room_name = data.room_name
    db.commit()
    return {"status": "updated", "user_activity_id": ua.id, "room_name": ua.room_name}


@app.post("/api/user/submit")
async def user_submit_notebook(
    email: str = Form(...),
    activity: str = Form(...),
    notebook: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Submit a notebook for grading. Auto-enrolls if not already enrolled."""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found – create the user first via POST /api/user",
        )

    act = db.query(Activity).filter(Activity.activity_id == activity).first()
    if not act:
        raise HTTPException(status_code=404, detail="Activity not found")

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == activity,
    ).first()
    if not ua:
        ua = UserActivity(user_id=user.id, activity_id=activity)
        db.add(ua)
        db.flush()

    notebook_content = await notebook.read()

    new_sub = Submission(
        user_activity_id=ua.id,
        notebook=notebook_content,
        notebook_filename=notebook.filename,
        submitted_at=datetime.utcnow().isoformat(),
        score=None,
        feedback=None,
    )
    db.add(new_sub)
    db.commit()
    db.refresh(new_sub)
    submission_id = new_sub.id

    if act.task_graders:
        asyncio.create_task(
            run_grader(submission_id, notebook_content,
                       notebook.filename, act.task_graders)
        )

    return {"status": "submitted", "submission_id": submission_id}


@app.get("/api/user/{email:path}/activities")
async def get_user_activities(email: str, db: Session = Depends(get_db)):
    """Return {activity_id, activity_name} pairs for a user's enrolled,
    currently-enabled activities."""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    result = []
    for ua in user.activities:
        act = db.query(Activity).filter(
            Activity.activity_id == ua.activity_id,
            Activity.enabled == True,
        ).first()
        if act:
            result.append({
                "activity_id": act.activity_id,
                "activity_name": act.activity_name,
            })
    return result


# ──────────────────────────────────────────────
# Backward-compatible submit endpoint
# ──────────────────────────────────────────────

@app.post("/api/submit")
async def submit_notebook(
    email: str = Form(...),
    activity: str = Form(...),
    notebook: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Backward-compatible submit. Writes to users / user_activities / submissions."""
    act = db.query(Activity).filter(Activity.activity_id == activity).first()
    if not act:
        raise HTTPException(status_code=404, detail="Activity not found")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(name=email, email=email)
        db.add(user)
        db.flush()

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == activity,
    ).first()
    if not ua:
        ua = UserActivity(user_id=user.id, activity_id=activity)
        db.add(ua)
        db.flush()

    notebook_content = await notebook.read()

    new_sub = Submission(
        user_activity_id=ua.id,
        notebook=notebook_content,
        notebook_filename=notebook.filename,
        submitted_at=datetime.utcnow().isoformat(),
        score=None,
        feedback=None,
    )
    db.add(new_sub)
    db.commit()
    db.refresh(new_sub)
    submission_id = new_sub.id

    if act.task_graders:
        asyncio.create_task(
            run_grader(submission_id, notebook_content,
                       notebook.filename, act.task_graders)
        )

    return {"status": "submitted", "submission_id": submission_id}


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
    user = db.query(User).filter(User.email == data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == data.activity_id,
    ).first()
    if not ua:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    latest = (
        db.query(Submission)
        .filter(Submission.user_activity_id == ua.id)
        .order_by(Submission.submitted_at.desc())
        .first()
    )
    if not latest:
        raise HTTPException(status_code=404, detail="No submission found")

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
    submission_id: int = None,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == activity_id,
    ).first()
    if not ua:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    if submission_id:
        sub = db.query(Submission).filter(
            Submission.id == submission_id,
            Submission.user_activity_id == ua.id,
        ).first()
    else:
        sub = (
            db.query(Submission)
            .filter(Submission.user_activity_id == ua.id)
            .order_by(Submission.submitted_at.desc())
            .first()
        )

    if not sub or not sub.notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    safe_email = email.replace("@", "_at_").replace(".", "_")
    filename = sub.notebook_filename or f"{safe_email}_{activity_id}.ipynb"
    content = _to_bytes(sub.notebook)
    return StreamingResponse(
        iter([content]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download-feedback/{activity_id}/{email:path}")
async def download_feedback(
    activity_id: str,
    email: str,
    submission_id: int = None,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ua = db.query(UserActivity).filter(
        UserActivity.user_id == user.id,
        UserActivity.activity_id == activity_id,
    ).first()
    if not ua:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    if submission_id:
        sub = db.query(Submission).filter(
            Submission.id == submission_id,
            Submission.user_activity_id == ua.id,
        ).first()
    else:
        sub = (
            db.query(Submission)
            .filter(Submission.user_activity_id == ua.id)
            .order_by(Submission.submitted_at.desc())
            .first()
        )

    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    feedback_text = sub.feedback or "No feedback available."
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
async def dashboard(request: Request, token: str = None, db: Session = Depends(get_db)):
    # ── Auth ──────────────────────────────────────────────────────────
    # Accept token from either:
    #   1. ?token=<jwt> query param  (used after Google Sign-In callback)
    #   2. google_token cookie       (set on first successful load, fallback)
    if not token:
        token = request.cookies.get("google_token")
    if not token:
        return HTMLResponse(_signin_page())

    try:
        claims = verify_google_token(token)
    except Exception as exc:
        logger.warning(f"[dashboard] token verification failed: {exc}")
        return HTMLResponse(_signin_page(error=str(exc)))

    email = claims.get("email", "")
    instructor = db.query(Instructor).filter(
        Instructor.email == email
    ).first()
    if not instructor:
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset=\'utf-8\'></head><body>"
            f"<h2>Access denied.</h2>"
            f"<p><b>{email}</b> is not registered as an instructor.</p>"
            "<p><a href=\'/dashboard\'>Sign in with a different account</a></p>"
            "</body></html>",
            status_code=403,
        )

    # ── Build HTML ────────────────────────────────────────────────────
    cards = ""
    for act in instructor.activities:
        rows = ""
        enrollments = (
            db.query(UserActivity)
            .filter(UserActivity.activity_id == act.activity_id)
            .all()
        )

        for ua in enrollments:
            submissions = (
                db.query(Submission)
                .filter(Submission.user_activity_id == ua.id)
                .order_by(Submission.submitted_at.desc())
                .all()
            )
            if not submissions:
                continue

            user = db.query(User).filter(User.id == ua.user_id).first()
            user_email = user.email if user else "unknown"

            latest = submissions[0]
            submission_count = len(submissions)

            if latest.score is None:
                score_cell = '<span class="badge-grading">Grading…</span>'
            else:
                score_cell = f'<span class="badge-score">{latest.score:.2f}</span>'

            dl_url = f"/download/{act.activity_id}/{user_email}?submission_id={latest.id}"
            fb_url = f"/download-feedback/{act.activity_id}/{user_email}?submission_id={latest.id}"

            feedback_btn = (
                f'<a class="btn btn-fb" href="{fb_url}">Feedback</a>'
                if latest.feedback
                else '<span style="color:#aaa;font-size:.8rem">—</span>'
            )

            rows += f"""
            <tr>
              <td>{user_email}</td>
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


def _signin_page(error: str = None) -> str:
    error_html = f'<p style="color:red;margin-top:16px">{error}</p>' if error else ''
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Sign In – Instructor Dashboard</title>
  <script src="https://accounts.google.com/gsi/client" async defer></script>
  <style>
    body {{ display:flex; align-items:center; justify-content:center;
           height:100vh; margin:0; font-family:Arial,sans-serif; background:#f4f6f8; }}
    .card {{ text-align:center; background:white; padding:40px 48px;
             border-radius:10px; box-shadow:0 2px 8px rgba(0,0,0,.15); }}
    h2 {{ margin:0 0 24px; color:#1a73e8; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>📚 Instructor Dashboard</h2>
    <div id="g_id_onload"
         data-client_id="{GOOGLE_CLIENT_ID}"
         data-callback="handleCredential"
         data-auto_prompt="false"></div>
    <div class="g_id_signin" data-type="standard"></div>
    {error_html}
  </div>
  <script>
    /*
     * Pass the credential as a URL query-parameter instead of a cookie.
     * This avoids Firefox's third-party / partitioned-cookie restrictions
     * that fire when the Google Sign-In iframe tries to set document.cookie
     * on the parent page from a cross-origin context.
     */
    function handleCredential(response) {{
      const token = encodeURIComponent(response.credential);
      window.location.href = "/dashboard?token=" + token;
    }}
  </script>
</body>
</html>"""
