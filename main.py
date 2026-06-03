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

POST   /api/activity/roster             – upload a CSV roster to create/update an activity and enroll users
POST   /api/activity/roster/update      – update enrollment for an existing activity from a new CSV roster
POST   /api/activity/{activity_id}/graders  – set the task_graders directory path for an activity

POST   /api/instructor                  – add instructor / assign activity

GET    /download/{activity_id}/{email}  – download latest (or specific) notebook
GET    /download-feedback/{activity_id}/{email} – download latest feedback

GET    /dashboard                       – instructor dashboard (Google auth)
"""

import asyncio
import csv
import io
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from typing import List, Optional

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


import re as _re_global

_RFC1123_RE_STR = r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$'


def _to_rfc1123_segment(s: str) -> str:
    """Lowercase, replace non-[a-z0-9] runs with '-', strip edge hyphens."""
    return _re_global.sub(r'^-+|-+$', '', _re_global.sub(r'[^a-z0-9]+', '-', s.lower()))


def _is_tbd_activity_id(activity_id: str) -> bool:
    """Return True if this is a temporary TBD activity ID (contains '-tbd-NNN' suffix)."""
    return bool(_re_global.search(r'-tbd-\d{3}$', activity_id))


def _generate_tbd_activity_id(base: str, db) -> str:
    """
    Given a base slug (name-year-semester), find the lowest available
    base-tbd-NNN (001..999) that does not conflict with an existing activity_id.
    """
    for n in range(1, 1000):
        candidate = f"{base}-tbd-{n:03d}"
        if not db.query(Activity).filter(Activity.activity_id == candidate).first():
            return candidate
    raise HTTPException(status_code=500, detail="Could not generate a unique TBD activity ID")


# ──────────────────────────────────────────────
# Activity endpoints
# ──────────────────────────────────────────────

@app.post("/api/activity")
async def create_or_update_activity(
    request: Request,
    activity_id: str = Form(None),   # optional on create; required when updating
    activity_name: str = Form(...),
    enabled: bool = Form(True),
    task_graders: str = Form(None),
    section: str = Form(None),
    year: int = Form(None),
    semester: str = Form(None),
    db: Session = Depends(get_db),
):
    instructor = require_instructor(request, db)

    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first() if activity_id else None

    if activity:
        # 3g: activity_name CAN be updated; activity_id cannot (it's the PK)
        activity.activity_name = activity_name
        activity.enabled = enabled
        if task_graders is not None:
            activity.task_graders = task_graders
        if section is not None:
            activity.section = section
        if year is not None:
            activity.year = year
        if semester is not None:
            activity.semester = semester
    else:
        # ── Generate activity_id if not supplied ──────────────────────
        if not activity_id:
            if year and semester and activity_name:
                parts = [_to_rfc1123_segment(p)
                         for p in [activity_name, str(year), semester] if p]
                parts = [p for p in parts if p]
                base = '-'.join(parts)
            else:
                base = _to_rfc1123_segment(activity_name) if activity_name else "activity"
            # No roster → TBD section placeholder (1b)
            activity_id = _generate_tbd_activity_id(base, db)
        elif not _re_global.match(_RFC1123_RE_STR, activity_id):
            raise HTTPException(
                status_code=422,
                detail=f"activity_id '{activity_id}' does not meet RFC 1123 rules.",
            )
        activity = Activity(
            activity_id=activity_id,
            activity_name=activity_name,
            enabled=enabled,
            task_graders=task_graders,
            section=section,
            year=year,
            semester=semester,
        )
        db.add(activity)
        db.flush()  # ensure activity.activity_id is set before appending

    # Link activity to this instructor (idempotent)
    if activity not in instructor.activities:
        instructor.activities.append(activity)

    db.commit()
    return {"status": "ok", "activity_id": activity_id}


@app.get("/api/activity/{activity_id}/exists")
async def activity_id_exists(activity_id: str, db: Session = Depends(get_db)):
    """Return {"exists": true/false} for the given activity_id. Used by the GUI."""
    exists = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first() is not None
    return {"exists": exists}


@app.delete("/api/activity/{activity_id}")
async def delete_activity(
    activity_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Delete an activity and cascade-remove all associated user_activities and
    submissions.  Requires a valid instructor Bearer token.
    """
    require_instructor(request, db)

    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Collect every UserActivity for this activity
    user_activities = (
        db.query(UserActivity)
        .filter(UserActivity.activity_id == activity_id)
        .all()
    )

    # Delete all Submissions for each enrollment
    for ua in user_activities:
        db.query(Submission).filter(
            Submission.user_activity_id == ua.id
        ).delete(synchronize_session=False)

    # Delete all UserActivity rows
    db.query(UserActivity).filter(
        UserActivity.activity_id == activity_id
    ).delete(synchronize_session=False)

    # Delete the Activity itself (also removes activity_instructors join rows
    # via the CASCADE defined on the FK)
    db.delete(activity)
    db.commit()
    return {"status": "deleted", "activity_id": activity_id}


@app.patch("/api/activity/{activity_id}/enabled")
async def toggle_activity_enabled(
    activity_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Toggle the enabled flag on an activity. Body: {"enabled": true|false}"""
    require_instructor(request, db)
    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    body = await request.json()
    activity.enabled = bool(body.get("enabled", not activity.enabled))
    db.commit()
    return {"status": "ok", "activity_id": activity_id, "enabled": activity.enabled}


@app.post("/api/activity/{activity_id}/graders")
async def set_activity_graders(
    activity_id: str,
    task_graders: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Set the task_graders directory path for an activity.

    Does not require an instructor token — intended for server-side or
    deployment scripts that configure grading after the activity is created.

    Body (form):
        task_graders  – absolute or relative path to the directory containing
                        per-task grader scripts (grade_task1.py, grade_task2.py, …)
    """
    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    activity.task_graders = task_graders
    db.commit()
    return {
        "status":       "ok",
        "activity_id":  activity_id,
        "task_graders": activity.task_graders,
    }


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
            "section": a.section,
            "year": a.year,
            "semester": a.semester,
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
# Roster upload endpoint
# ──────────────────────────────────────────────

# Valid role values for user_activities.role
_VALID_ROLES = {"Student", "Instructor", "TA", "Admin"}


@app.post("/api/activity/roster")
async def upload_roster(
    request: Request,
    roster: UploadFile = File(...),
    activity_name: str = Form(...),
    year: int = Form(...),
    semester: str = Form(...),
    instructor_email: str = Form(...),
    activity_id: str = Form(None),
    db: Session = Depends(get_db),
):
    """
    Upload a CSV roster to create or update an activity and enroll users.

    CSV columns expected: First Name, Last Name, SID, Email, Role, Section

    Behaviour
    ─────────
    • If an activity with the same (activity_name, section, year, semester)
      already exists, that activity is reused.  All existing "Student"
      enrollments are removed and replaced with the roster rows whose Role is
      "Student" (other roles are preserved and/or upserted).
    • If no matching activity exists, a new one is created.  If activity_id is
      supplied it is used as the primary key; otherwise one is auto-generated
      as  "<slugified_activity_name>-<section>-<year>-<semester>".
    • Users are created in the users table if they do not already exist.
    • The instructor identified by instructor_email must already exist in the
      instructors table; the activity is added to their assignment list.
    """
    # ── Auth ──────────────────────────────────────────────────────────
    instructor = require_instructor(request, db)

    # ── Parse CSV ─────────────────────────────────────────────────────
    raw_bytes = await roster.read()
    try:
        text = raw_bytes.decode("utf-8-sig")   # strip BOM if present
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="Roster CSV is empty")

    # Validate that the required columns are present
    required_cols = {"Email", "Role", "Section"}
    missing = required_cols - set(reader.fieldnames or [])
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV is missing required columns: {missing}",
        )

    # All rows must share a single section value (taken from the first row)
    sections_in_csv = {r["Section"].strip() for r in rows if r.get("Section", "").strip()}
    if len(sections_in_csv) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "All rows in the roster must have the same Section value.  "
                f"Found: {sections_in_csv}"
            ),
        )
    section = sections_in_csv.pop()

    # Validate role values
    for idx, row in enumerate(rows, start=2):   # row 1 is the header
        role = row.get("Role", "").strip()
        if role not in _VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Row {idx}: invalid Role '{role}'.  "
                    f"Must be one of {sorted(_VALID_ROLES)}."
                ),
            )

    # ── Look up the instructor record from the token ──────────────────
    # (require_instructor already verified the token; just confirm the
    #  instructor_email param matches so callers can't enrol on behalf
    #  of a different instructor without owning that token.)
    if instructor.email.lower() != instructor_email.strip().lower():
        raise HTTPException(
            status_code=403,
            detail="instructor_email does not match the authenticated instructor token",
        )

    # ── Find or create the Activity ───────────────────────────────────
    existing_activity = None

    # 1c: If a specific activity_id was passed and it's a TBD id, look it up directly
    if activity_id and _is_tbd_activity_id(activity_id):
        existing_activity = db.query(Activity).filter(
            Activity.activity_id == activity_id
        ).first()

    # Otherwise search by (name, section, year, semester)
    if not existing_activity:
        existing_activity = (
            db.query(Activity)
            .filter(
                Activity.activity_name == activity_name,
                Activity.section       == section,
                Activity.year          == year,
                Activity.semester      == semester,
            )
            .first()
        )

    # xd: If the found activity has a real (non-TBD) section, reject a mismatched section
    if existing_activity and not _is_tbd_activity_id(existing_activity.activity_id):
        if existing_activity.section and existing_activity.section != section:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Activity '{existing_activity.activity_id}' already has section "
                    f"'{existing_activity.section}'. A roster with a different section "
                    f"('{section}') may not be uploaded to it."
                ),
            )

    if existing_activity:
        activity = existing_activity
        is_new_activity = False

        # 1c: Replace TBD activity_id with the real section-based id
        if _is_tbd_activity_id(activity.activity_id):
            import re as _re
            def _to_seg(s):
                return _re.sub(r'^-+|-+$', '', _re.sub(r'[^a-z0-9]+', '-', s.lower()))
            parts = [_to_seg(p) for p in [activity_name, str(year), semester, section] if p]
            new_id = '-'.join([p for p in parts if p])

            # Validate and ensure uniqueness
            if not _re.match(_RFC1123_RE_STR, new_id):
                new_id = activity.activity_id  # keep TBD if something's wrong
            elif db.query(Activity).filter(Activity.activity_id == new_id).first():
                new_id = activity.activity_id  # already exists; keep TBD
            else:
                # ── Upgrade TBD id to real section-based id ───────────
                # ORDER MATTERS for FK constraints:
                #   1. Update the activities PK first and flush so the new
                #      activity_id exists in the table before any FK row
                #      references it.
                #   2. Then update child tables (user_activities,
                #      activity_instructors) that have FKs pointing to it.
                old_id = activity.activity_id
                activity.activity_id = new_id
                activity.section = section
                db.flush()   # PK now exists in activities ← FK rows can follow

                db.query(UserActivity).filter(
                    UserActivity.activity_id == old_id
                ).update({"activity_id": new_id}, synchronize_session=False)

                from sqlalchemy import text as _text
                db.execute(
                    _text("UPDATE activity_instructors SET activity_id=:new WHERE activity_id=:old"),
                    {"new": new_id, "old": old_id},
                )
                db.flush()
                activity_id = new_id
    else:
        # Derive an activity_id if one was not supplied.
        # Follows RFC 1123 subdomain rules: lowercase alphanumeric + hyphens,
        # must start and end with an alphanumeric character.
        if not activity_id:
            # Order: name - year - semester - section
            parts = [_to_rfc1123_segment(p) for p in
                     [activity_name, str(year), semester, section] if p]
            parts = [p for p in parts if p]   # drop any empty segments
            activity_id = '-'.join(parts)

        # Check the supplied activity_id isn't already in use by a different activity
        if not _re_global.match(_RFC1123_RE_STR, activity_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"activity_id '{activity_id}' does not meet RFC 1123 subdomain rules. "
                    "Use only lowercase letters, digits, hyphens (-) and dots (.); "
                    "it must start and end with a letter or digit."
                ),
            )
        id_conflict = db.query(Activity).filter(
            Activity.activity_id == activity_id
        ).first()
        if id_conflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"activity_id '{activity_id}' is already used by a different "
                    "activity.  Supply a unique activity_id or omit it to "
                    "auto-generate one."
                ),
            )

        activity = Activity(
            activity_id=activity_id,
            activity_name=activity_name,
            enabled=True,
            section=section,
            year=year,
            semester=semester,
        )
        db.add(activity)
        db.flush()   # populate activity.activity_id before FK references
        is_new_activity = True

    # ── Assign instructor to the activity ─────────────────────────────
    if activity not in instructor.activities:
        instructor.activities.append(activity)

    # ── If updating an existing activity, drop Student enrollments ────
    if not is_new_activity:
        student_uas = (
            db.query(UserActivity)
            .filter(
                UserActivity.activity_id == activity.activity_id,
                UserActivity.role        == "Student",
            )
            .all()
        )
        for ua in student_uas:
            db.delete(ua)
        db.flush()

    # ── Upsert users and enroll them ──────────────────────────────────
    enrolled: list[dict] = []
    skipped:  list[dict] = []

    for row in rows:
        email = row.get("Email", "").strip()
        if not email:
            continue   # skip rows without an email address

        first = row.get("First Name", "").strip()
        last  = row.get("Last Name",  "").strip()
        sid   = row.get("SID", "").strip() or None

        if first and last:
            name = f"{first} {last}"
        elif first:
            name = first
        elif last:
            name = last
        else:
            name = email   # fallback so name is never empty

        role = row.get("Role", "").strip()

        # Upsert user
        user = db.query(User).filter(User.email == email).first()
        if user:
            if name and name != user.name:
                user.name = name
            if first:
                user.first_name = first
            if last:
                user.last_name = last
            if sid is not None:
                user.user_id = sid
        else:
            user = User(name=name, email=email,
                        first_name=first or None, last_name=last or None,
                        user_id=sid)
            db.add(user)
            db.flush()

        # Check for an existing enrollment (any role) before adding
        existing_ua = (
            db.query(UserActivity)
            .filter(
                UserActivity.user_id     == user.id,
                UserActivity.activity_id == activity.activity_id,
            )
            .first()
        )

        if existing_ua:
            # Update the role in case it changed
            existing_ua.role = role
            skipped.append({"email": email, "reason": "already enrolled; role updated"})
        else:
            ua = UserActivity(
                user_id=user.id,
                activity_id=activity.activity_id,
                role=role,
            )
            db.add(ua)
            enrolled.append({"email": email, "role": role})

    db.commit()

    return {
        "status": "ok",
        "activity_id": activity.activity_id,
        "activity_name": activity.activity_name,
        "section": activity.section,
        "year": activity.year,
        "semester": activity.semester,
        "is_new_activity": is_new_activity,
        "enrolled_count": len(enrolled),
        "skipped_count": len(skipped),
        "enrolled": enrolled,
        "skipped": skipped,
    }


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

import csv as _csv
import io as _io


def _csv_val(v):
    """Return empty string for None/falsy, otherwise the value as a string."""
    if v is None:
        return ""
    return str(v)


@app.get("/download-roster/{activity_id}")
async def download_roster(activity_id: str, db: Session = Depends(get_db)):
    """Download a CSV roster for an activity (5e).
    Columns: First Name, Last Name, SID, Email, Role, Section"""
    activity = db.query(Activity).filter(Activity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    enrollments = (
        db.query(UserActivity)
        .filter(UserActivity.activity_id == activity_id)
        .all()
    )

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["First Name", "Last Name", "SID", "Email", "Role", "Section"])
    for ua in enrollments:
        user = db.query(User).filter(User.id == ua.user_id).first()
        if not user:
            continue
        writer.writerow([
            _csv_val(user.first_name),
            _csv_val(user.last_name),
            _csv_val(user.user_id),
            _csv_val(user.email),
            _csv_val(ua.role),
            _csv_val(activity.section),
        ])

    filename = f"roster_{activity_id}.csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/download-scores/{activity_id}")
async def download_scores(activity_id: str, db: Session = Depends(get_db)):
    """Download a CSV of latest scores for an activity (5f).
    Columns: First Name, Last Name, SID, Email, Role, Section, Score"""
    activity = db.query(Activity).filter(Activity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    enrollments = (
        db.query(UserActivity)
        .filter(UserActivity.activity_id == activity_id)
        .all()
    )

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["First Name", "Last Name", "SID", "Email", "Role", "Section", "Score"])
    for ua in enrollments:
        user = db.query(User).filter(User.id == ua.user_id).first()
        if not user:
            continue
        latest = (
            db.query(Submission)
            .filter(Submission.user_activity_id == ua.id)
            .order_by(Submission.submitted_at.desc())
            .first()
        )
        score = _csv_val(latest.score if latest else None)
        writer.writerow([
            _csv_val(user.first_name),
            _csv_val(user.last_name),
            _csv_val(user.user_id),
            _csv_val(user.email),
            _csv_val(ua.role),
            _csv_val(activity.section),
            score,
        ])

    filename = f"scores_{activity_id}.csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/download/{activity_id}/{email:path}")
async def download_notebook(
    activity_id: str,
    email: str,
    submission_id: int = None,
    dl_name: str = None,
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

    if dl_name:
        filename = dl_name if dl_name.endswith(".ipynb") else dl_name + ".ipynb"
    else:
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
    dl_name: str = None,
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
    if dl_name:
        filename = dl_name if dl_name.endswith(".txt") else dl_name + ".txt"
    else:
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
  /* ── Base ── */
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; }

  /* ── Header ── */
  header { background: #1a73e8; color: white; padding: 16px 24px;
           display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 1.3rem; }
  .header-right { margin-left: auto; display: flex; align-items: center; gap: 14px; }

  /* ── Add Activity button (header) ── */
  .btn-add-activity {
    background: white; color: #1a73e8; border: none; border-radius: 6px;
    padding: 7px 16px; font-size: .9rem; font-weight: 600; cursor: pointer;
    display: flex; align-items: center; gap: 6px;
    transition: background .15s;
  }
  .btn-add-activity:hover { background: #e8f0fe; }

  /* ── Main content ── */
  .container { max-width: 1100px; margin: 32px auto; padding: 0 16px; }

  /* ── Activity cards ── */
  .activity-card { background: white; border-radius: 8px; margin-bottom: 32px;
                   box-shadow: 0 1px 4px rgba(0,0,0,.12); }
  .activity-card h2 { margin: 0; padding: 16px 20px;
                      border-bottom: 1px solid #e0e0e0; font-size: 1.1rem; }
  .activity-meta { font-size: .8rem; color: #888; margin-left: 8px; }
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

  /* ── Modal overlay ── */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.45); z-index: 1000;
    align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }

  /* ── Modal dialog ── */
  .modal {
    background: white; border-radius: 10px; width: 520px; max-width: 95vw;
    box-shadow: 0 8px 32px rgba(0,0,0,.22);
    display: flex; flex-direction: column;
    max-height: 90vh; overflow: hidden;
  }
  .modal-header {
    padding: 18px 24px 14px; border-bottom: 1px solid #e0e0e0;
    display: flex; align-items: center; justify-content: space-between;
  }
  .modal-header h2 { margin: 0; font-size: 1.1rem; color: #1a73e8; }
  .modal-close {
    background: none; border: none; font-size: 1.4rem; color: #888;
    cursor: pointer; line-height: 1; padding: 0 4px;
  }
  .modal-close:hover { color: #333; }
  .modal-body { padding: 20px 24px; overflow-y: auto; flex: 1; }
  .modal-footer {
    padding: 14px 24px; border-top: 1px solid #e0e0e0;
    display: flex; gap: 10px; justify-content: flex-end;
    background: #fafafa;
  }

  /* ── Form fields ── */
  .field { margin-bottom: 16px; }
  .field label {
    display: block; font-size: .85rem; font-weight: 600;
    color: #444; margin-bottom: 5px;
  }
  .field label .req { color: #d93025; margin-left: 2px; }
  .field input[type="text"],
  .field input[type="number"],
  .field select {
    width: 100%; padding: 8px 10px; border: 1px solid #ccc;
    border-radius: 5px; font-size: .9rem; outline: none;
    transition: border-color .15s;
  }
  .field input:focus, .field select:focus { border-color: #1a73e8; }
  .field .hint { font-size: .78rem; color: #888; margin-top: 4px; }

  /* ── File picker row ── */
  .file-row { display: flex; align-items: center; gap: 10px; }
  .file-row input[type="file"] { display: none; }
  .btn-browse {
    padding: 7px 14px; background: #f1f3f4; border: 1px solid #ccc;
    border-radius: 5px; font-size: .85rem; cursor: pointer; white-space: nowrap;
    transition: background .15s;
  }
  .btn-browse:hover { background: #e2e6ea; }
  .file-name { font-size: .85rem; color: #555; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }

  /* ── Error banner ── */
  .error-banner {
    display: none; background: #fce8e6; border: 1px solid #f28b82;
    color: #c5221f; border-radius: 5px; padding: 10px 14px;
    font-size: .85rem; margin-bottom: 14px; white-space: pre-wrap;
  }
  .error-banner.visible { display: block; }

  /* ── Success banner ── */
  .success-banner {
    display: none; background: #e6f4ea; border: 1px solid #81c995;
    color: #137333; border-radius: 5px; padding: 10px 14px;
    font-size: .85rem; margin-bottom: 14px;
  }
  .success-banner.visible { display: block; }

  /* ── Dialog action buttons ── */
  .btn-primary {
    padding: 8px 20px; background: #1a73e8; color: white; border: none;
    border-radius: 6px; font-size: .9rem; font-weight: 600; cursor: pointer;
    transition: background .15s;
  }
  .btn-primary:hover:not(:disabled) { background: #1558b0; }
  .btn-primary:disabled { background: #a8c7fa; cursor: default; }
  .btn-secondary {
    padding: 8px 20px; background: white; color: #444;
    border: 1px solid #ccc; border-radius: 6px; font-size: .9rem;
    cursor: pointer; transition: background .15s;
  }
  .btn-secondary:hover { background: #f1f3f4; }

  /* ── Spinner ── */
  .spinner {
    display: none; width: 18px; height: 18px;
    border: 3px solid #a8c7fa; border-top-color: #1a73e8;
    border-radius: 50%; animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Activity card header row ── */
  .activity-card h2 {
    display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  }
  .activity-card h2 .h2-title { flex: 1; }

  /* ── Enable/Disable toggle button (per-card) ── */
  .btn-toggle-activity {
    border: none; border-radius: 5px;
    padding: 4px 11px; font-size: .78rem; font-weight: 600; cursor: pointer;
    white-space: nowrap; transition: background .15s; flex-shrink: 0;
  }
  .btn-toggle-activity.enabled  { background: #1a73e8; color: white; }
  .btn-toggle-activity.enabled:hover  { background: #1558b0; }
  .btn-toggle-activity.disabled { background: #5f6368; color: white; }
  .btn-toggle-activity.disabled:hover { background: #3c4043; }

  /* ── Update Activity button (per-card) ── */
  .btn-update-roster {
    background: #137333; color: white; border: none; border-radius: 5px;
    padding: 4px 11px; font-size: .78rem; font-weight: 600; cursor: pointer;
    white-space: nowrap; transition: background .15s; flex-shrink: 0;
  }
  .btn-update-roster:hover { background: #0d5226; }

  /* ── Delete Activity button (per-card) ── */
  .btn-delete-activity {
    background: #d93025; color: white; border: none; border-radius: 5px;
    padding: 4px 11px; font-size: .78rem; font-weight: 600; cursor: pointer;
    white-space: nowrap; transition: background .15s; flex-shrink: 0;
  }
  .btn-delete-activity:hover { background: #a50e0e; }

  /* ── Delete-confirm dialog (reuses modal-overlay) ── */
  .confirm-dialog {
    background: #fff0f0; border: 2px solid #d93025;
    border-radius: 10px; width: 480px; max-width: 95vw;
    box-shadow: 0 8px 32px rgba(0,0,0,.25);
    display: flex; flex-direction: column;
  }
  .confirm-header {
    padding: 16px 20px 12px; border-bottom: 1px solid #f28b82;
    display: flex; align-items: center; gap: 10px;
  }
  .confirm-header h2 {
    margin: 0; font-size: 1rem; color: #a50e0e; flex: 1;
    display: block;   /* override the flex rule above for this h2 */
  }
  .confirm-body {
    padding: 18px 20px; font-size: .9rem; color: #3c1010; line-height: 1.55;
  }
  .confirm-body code {
    background: #fce8e6; padding: 1px 5px; border-radius: 3px;
    font-size: .88rem; color: #a50e0e;
  }
  .confirm-footer {
    padding: 12px 20px 16px; display: flex; gap: 10px;
    justify-content: flex-end; background: #fff5f5;
    border-top: 1px solid #f28b82; border-radius: 0 0 8px 8px;
  }
  /* "Do NOT Delete" — green, slightly larger */
  .btn-no-delete {
    padding: 9px 22px; background: #137333; color: white; border: none;
    border-radius: 6px; font-size: .95rem; font-weight: 700; cursor: pointer;
    transition: background .15s;
  }
  .btn-no-delete:hover { background: #0d5226; }
  /* "DELETE" — red, slightly smaller */
  .btn-confirm-delete {
    padding: 7px 18px; background: #d93025; color: white; border: none;
    border-radius: 6px; font-size: .85rem; font-weight: 600; cursor: pointer;
    transition: background .15s;
  }
  .btn-confirm-delete:hover:not(:disabled) { background: #a50e0e; }
  .btn-confirm-delete:disabled { background: #f28b82; cursor: default; }
  /* delete spinner (red tones) */
  .del-spinner {
    display: none; width: 16px; height: 16px;
    border: 3px solid #f28b82; border-top-color: #d93025;
    border-radius: 50%; animation: spin .7s linear infinite;
  }
</style>
"""


def _build_activity_cards(instructor, db) -> str:
    """Return the inner HTML for all activity cards belonging to an instructor."""
    from datetime import datetime, timezone, timedelta

    # ── Pittsburgh ET timezone (EST=UTC-5, EDT=UTC-4) ────────────────
    now_utc = datetime.now(timezone.utc)

    def _nth_sunday(year, month, n):
        from calendar import monthrange
        count, day = 0, 1
        while day <= monthrange(year, month)[1]:
            if datetime(year, month, day).weekday() == 6:
                count += 1
                if count == n:
                    return day
            day += 1
        return day

    yr = now_utc.year
    dst_start = datetime(yr, 3,  _nth_sunday(yr, 3,  2), 2, tzinfo=timezone.utc) + timedelta(hours=5)
    dst_end   = datetime(yr, 11, _nth_sunday(yr, 11, 1), 2, tzinfo=timezone.utc) + timedelta(hours=4)
    is_dst   = dst_start <= now_utc < dst_end
    et_zone  = timezone(timedelta(hours=-4 if is_dst else -5))
    tz_label = "EDT" if is_dst else "EST"

    def _fmt_et(ts_str):
        if not ts_str:
            return ""
        try:
            ts = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(et_zone).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ts_str

    cards = ""
    for act in instructor.activities:
        enrollments = (
            db.query(UserActivity)
            .filter(UserActivity.activity_id == act.activity_id)
            .all()
        )

        # Gather all enrolled users and their latest submission
        user_rows       = []   # (user, ua, latest_sub|None, sub_count)
        has_students    = False
        any_submissions = False

        for ua in enrollments:
            user = db.query(User).filter(User.id == ua.user_id).first()
            if not user:
                continue
            if ua.role == "Student":
                has_students = True
            subs = (
                db.query(Submission)
                .filter(Submission.user_activity_id == ua.id)
                .order_by(Submission.submitted_at.desc())
                .all()
            )
            latest = subs[0] if subs else None
            if latest:
                any_submissions = True
            user_rows.append((user, ua, latest, len(subs)))

        # ── Table rows ────────────────────────────────────────────────
        rows_html = ""
        has_roster = bool(user_rows)   # any enrolled users = has roster
        any_scores = any(
            latest is not None and latest.score is not None
            for _, _, latest, _ in user_rows
        )

        for user, ua, latest, sub_count in user_rows:
            safe_dl = user.email.replace("@", "-at-")
            if latest:
                time_cell  = _fmt_et(latest.submitted_at)
                count_cell = str(sub_count)
                score_cell = (
                    '<span class="badge-grading">Grading…</span>'
                    if latest.score is None
                    else f'<span class="badge-score">{latest.score:.2f}</span>'
                )
                nb_name = f"notebook_{act.activity_id}_{safe_dl}"
                fb_name = f"feedback_{act.activity_id}_{safe_dl}"
                dl_url  = (f"/download/{act.activity_id}/{user.email}"
                           f"?submission_id={latest.id}&dl_name={nb_name}")
                fb_url  = (f"/download-feedback/{act.activity_id}/{user.email}"
                           f"?submission_id={latest.id}&dl_name={fb_name}")
                fb_btn  = (
                    f'<a class="btn btn-fb" href="{fb_url}">Feedback</a>'
                    if latest.feedback
                    else '<span style="color:#aaa;font-size:.8rem">—</span>'
                )
                actions = f'<a class="btn btn-dl" href="{dl_url}">Download</a>{fb_btn}'
            else:
                time_cell = count_cell = score_cell = actions = ""

            rows_html += f"""
            <tr>
              <td>{user.email}</td>
              <td>{count_cell}</td>
              <td>{time_cell}</td>
              <td>{score_cell}</td>
              <td>{actions}</td>
            </tr>"""

        # ── Download Roster / Scores row (items 2, 3, 4) ─────────────
        # Roster button: only when there are enrolled users (item 3)
        # Scores button: only when there are actual numeric scores (item 4)
        # Both in the same row; Scores sits in the Latest Score column (item 2)
        roster_dl_btn = (
            f'<a class="btn btn-dl" href="/download-roster/{act.activity_id}">⬇ Download Roster</a>'
            if has_roster else ""
        )
        scores_dl_btn = (
            f'<a class="btn btn-dl" href="/download-scores/{act.activity_id}">⬇ Download Scores</a>'
            if any_scores else ""
        )
        if roster_dl_btn or scores_dl_btn:
            rows_html += f"""
            <tr>
              <td>{roster_dl_btn}</td>
              <td></td><td></td>
              <td>{scores_dl_btn}</td>
              <td></td>
            </tr>"""

        # Issue 1: show activity even with no enrollments
        if not rows_html:
            rows_html = '<tr><td colspan="5" style="color:#aaa">No users enrolled</td></tr>'

        # ── Card metadata ─────────────────────────────────────────────
        meta_parts = []
        if act.section:  meta_parts.append(f"Section {act.section}")
        if act.semester: meta_parts.append(act.semester)
        if act.year:     meta_parts.append(str(act.year))
        meta_str  = " · ".join(meta_parts)
        meta_html = f'<span class="activity-meta">{meta_str}</span>' if meta_str else ""

        safe_act_id   = act.activity_id.replace("'", "\\'")
        safe_act_name = act.activity_name.replace("'", "\\'").replace("\\", "\\\\")
        safe_semester = (act.semester or "").replace("'", "\\'")
        act_year      = act.year or ""

        # 4/5a: blue=Enable, grey=Disable
        if act.enabled:
            toggle_cls   = "btn-toggle-activity disabled"
            toggle_label = "Disable"
            toggle_title = "Click to disable this activity"
        else:
            toggle_cls   = "btn-toggle-activity enabled"
            toggle_label = "Enable"
            toggle_title = "Click to enable this activity"

        disabled_badge = (
            "" if act.enabled
            else ' <span style="background:#e8eaed;color:#5f6368;font-size:.72rem;'
                 'font-weight:600;padding:1px 7px;border-radius:10px;vertical-align:middle">'
                 'DISABLED</span>'
        )

        # 3b: roster button label
        roster_btn_lbl = "Update Roster" if has_students else "Add Roster"

        cards += f"""
        <div class="activity-card">
          <h2>
            <span class="h2-title">{act.activity_name}{disabled_badge}{meta_html}
              <small style="color:#bbb;font-size:.75rem;margin-left:6px">({act.activity_id})</small>
            </span>
            <button class="{toggle_cls}" title="{toggle_title}"
                    onclick="toggleActivity('{safe_act_id}', {str(act.enabled).lower()}, this)">
              {toggle_label}
            </button>
            <button class="btn-update-roster"
                    onclick="openUpdateModal({{activity_id:'{safe_act_id}',activity_name:'{safe_act_name}',year:'{act_year}',semester:'{safe_semester}',enabled:{str(act.enabled).lower()},has_students:{str(has_students).lower()}}})">
              ↺ Update Activity
            </button>
            <input type="file" id="ur-input-{act.activity_id}"
                   accept=".csv,text/csv" style="display:none"
                   onchange="onUpdateRosterChosen(this, '{safe_act_id}')">
            <button class="btn-delete-activity"
                    onclick="openDeleteConfirm('{safe_act_id}')">
              🗑 Delete Activity
            </button>
          </h2>
          <table>
            <thead>
              <tr>
                <th>Email</th><th>Submissions</th>
                <th>Latest Submission ({tz_label})</th>
                <th>Latest Score</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>"""

    return cards or "<p style='color:#888'>No activities assigned yet.</p>"


# Pre-built JS block for the instructor dashboard.
# Kept outside the f-string to avoid Python escaping issues with
# backslashes in the RFC 1123 regex.  Token/email are injected at
# runtime via str.replace() on the two placeholder strings.
_DASHBOARD_JS = r"""// ── Globals ──────────────────────────────────────────────────────────
const BEARER_TOKEN     = `__BEARER_TOKEN__`;
const INSTRUCTOR_EMAIL = `__INSTRUCTOR_EMAIL__`;

// RFC 1123 subdomain validation.
const RFC1123_RE = new RegExp(
  '^[a-z0-9]([-a-z0-9]*[a-z0-9])?' +
  '(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$'
);

// ── RFC 1123 slug helpers ────────────────────────────────────────────

function toRFC1123Segment(str) {
  return str
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Auto-generate activity_id from: name – year – semester – section.
 * When no section is available (no roster chosen yet), the section
 * component is replaced by tbd-NNN (1b).  The server assigns the
 * actual NNN; the browser just shows a preview with tbd-xxx.
 */
function buildAutoId() {
  const name     = document.getElementById('f-activity-name').value.trim();
  const year     = document.getElementById('f-year').value.trim();
  const semester = document.getElementById('f-semester').value;
  const section  = window._rosterSection || '';

  if (!name || !year || !semester) return '';

  // Order: name - year - semester - section  (3e/3f)
  // If no section yet, use tbd-xxx as a preview placeholder
  const sectionSlug = section ? toRFC1123Segment(section) : 'tbd-xxx';
  const parts = [name, year, semester].map(toRFC1123Segment).filter(Boolean);
  parts.push(sectionSlug);
  if (parts.some(p => !p)) return '';

  return parts.join('-');
}

// ── Activity-ID live update & validation ─────────────────────────────

function updateActivityId() {
  const idField = document.getElementById('f-activity-id');
  // Never overwrite when: user manually edited, OR we are in update mode (locked)
  if (idField.dataset.manual === 'true' || idField.readOnly) return;
  const auto = buildAutoId();
  idField.value = auto;
  refreshIdBadge(auto, null);
}

// Debounce timer for the uniqueness check
let _idCheckTimer = null;

function onActivityIdInput(input) {
  if (input.readOnly) return;  // locked in update mode
  const pos = input.selectionStart;
  input.value = input.value.toLowerCase();
  input.setSelectionRange(pos, pos);
  input.dataset.manual = input.value !== '' ? 'true' : 'false';

  const val = input.value;
  if (!val || val.includes('tbd-xxx')) {
    refreshIdBadge(val, null);
    return;
  }
  if (!RFC1123_RE.test(val)) {
    refreshIdBadge(val, null);   // show invalid format immediately
    return;
  }
  // Valid format: debounce a uniqueness check
  clearTimeout(_idCheckTimer);
  refreshIdBadge(val, 'checking');
  _idCheckTimer = setTimeout(() => _checkIdUnique(val), 400);
}

async function _checkIdUnique(val) {
  // Only act if the field still holds this value
  const field = document.getElementById('f-activity-id');
  if (field.value !== val) return;
  try {
    const resp = await fetch('/api/activity/' + encodeURIComponent(val) + '/exists');
    const data = await resp.json().catch(() => ({}));
    if (field.value !== val) return;  // user typed again
    refreshIdBadge(val, data.exists ? 'taken' : 'free');
  } catch (_) {
    refreshIdBadge(val, null);  // network error: fall back to format-only
  }
}

/**
 * Update the Activity ID validation badge.
 * state: null = format-only, 'checking' = spinner, 'free' = unique, 'taken' = duplicate
 */
function refreshIdBadge(value, state) {
  const badge = document.getElementById('id-valid-badge');
  if (!value || value.includes('tbd-xxx')) { badge.style.display = 'none'; return; }
  const fmtOk = RFC1123_RE.test(value);
  badge.style.display = 'inline';
  if (!fmtOk) {
    badge.textContent      = '\u2717 invalid format';
    badge.style.background = '#fce8e6';
    badge.style.color      = '#c5221f';
  } else if (state === 'checking') {
    badge.textContent      = '\u29d6 checking\u2026';
    badge.style.background = '#e8f0fe';
    badge.style.color      = '#1a73e8';
  } else if (state === 'taken') {
    badge.textContent      = '\u2717 already in use';
    badge.style.background = '#fce8e6';
    badge.style.color      = '#c5221f';
  } else if (state === 'free') {
    badge.textContent      = '\u2713 valid and available';
    badge.style.background = '#e6f4ea';
    badge.style.color      = '#137333';
  } else {
    // format valid, uniqueness unknown
    badge.textContent      = '\u2713 valid format';
    badge.style.background = '#e6f4ea';
    badge.style.color      = '#137333';
  }
}

// ── Modal open / close ───────────────────────────────────────────────

/**
 * Open in "Add" mode (new activity).
 */
function openModal() {
  _openActivityModal({ mode: 'add' });
}

/**
 * Open in "Update" mode for an existing activity.
 * activityData = { activity_id, activity_name, year, semester, enabled, has_students }
 */
function openUpdateModal(activityData) {
  _openActivityModal({ mode: 'update', data: activityData });
}

function _openActivityModal(opts) {
  const isUpdate = opts.mode === 'update';
  const data     = opts.data || {};

  resetModal();

  // Update modal title
  document.getElementById('modal-title').textContent =
    isUpdate ? 'Update Activity' : 'Add Activity';

  if (isUpdate) {
    // Set roster button label (3b)
    const rosterBtn = document.getElementById('roster-btn');
    if (rosterBtn) {
      rosterBtn.textContent = data.has_students ? 'Update Roster' : 'Add Roster';
    }
    // Pre-fill fields
    document.getElementById('f-activity-name').value = data.activity_name || '';
    document.getElementById('f-year').value           = data.year          || '';
    const semSel = document.getElementById('f-semester');
    Array.from(semSel.options).forEach(o => { o.selected = (o.value === data.semester); });
    semSel.dispatchEvent(new Event('change'));   // trigger auto-ID (will be ignored due to readOnly)

    // Lock/unlock the activity_id field depending on TBD status (xd / 3g)
    const idField = document.getElementById('f-activity-id');
    idField.value = data.activity_id || '';
    const isTbd = (data.activity_id || '').includes('-tbd-');
    if (isTbd) {
      // TBD id: editable so instructor can override, but show note
      idField.readOnly = false;
      idField.style.background = '';
      idField.style.color      = '';
      idField.dataset.manual   = 'false';
      document.getElementById('id-optional-label').style.display = 'inline';
      document.getElementById('id-locked-label').style.display   = 'none';
    } else {
      // Real section id: fully locked
      idField.readOnly = true;
      idField.style.background = '#f8f9fa';
      idField.style.color      = '#5f6368';
      document.getElementById('id-optional-label').style.display = 'none';
      document.getElementById('id-locked-label').style.display   = 'inline';
    }
    document.getElementById('id-valid-badge').style.display = 'none';

    // Set enabled radio
    const enabledVal = (data.enabled === true || data.enabled === 'true') ? 'true' : 'false';
    document.querySelectorAll('input[name="f-enabled"]').forEach(r => {
      r.checked = (r.value === enabledVal);
    });


    // Store the activity_id being updated
    document.getElementById('modal-overlay').dataset.activityId = data.activity_id;
  } else {
    document.getElementById('modal-overlay').removeAttribute('data-activity-id');
  }

  document.getElementById('modal-overlay').classList.add('open');
  document.getElementById('f-activity-name').focus();
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
}

document.getElementById('modal-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') { closeModal(); closeDeleteConfirm(); }
});

['f-activity-name', 'f-year', 'f-semester'].forEach(function(id) {
  const el  = document.getElementById(id);
  const evt = (el.tagName === 'SELECT') ? 'change' : 'input';
  el.addEventListener(evt, updateActivityId);
});

// ── Modal helpers ────────────────────────────────────────────────────

function resetModal() {
  document.getElementById('f-activity-name').value = '';
  document.getElementById('f-year').value           = '';
  document.getElementById('f-semester').value       = '';

  const idField = document.getElementById('f-activity-id');
  idField.value            = '';
  idField.readOnly         = false;
  idField.style.background = '';
  idField.style.color      = '';
  idField.dataset.manual   = 'false';

  document.getElementById('id-optional-label').style.display = 'inline';
  document.getElementById('id-locked-label').style.display   = 'none';
  document.getElementById('id-valid-badge').style.display    = 'none';

  // Reset enabled radio to "Enable" (default)
  document.getElementById('f-enabled-yes').checked = true;

  document.getElementById('f-roster').value  = '';
  document.getElementById('file-name-display').textContent = '';
  const rosterBtn = document.getElementById('roster-btn');
  if (rosterBtn) rosterBtn.textContent = 'Add Roster';
  window._rosterSection   = '';
  hideError();
  hideSuccess();
  setLoading(false);
}

function showError(msg) {
  const el = document.getElementById('error-banner');
  el.textContent = msg;
  el.classList.add('visible');
  document.getElementById('success-banner').classList.remove('visible');
}

function hideError() {
  document.getElementById('error-banner').classList.remove('visible');
}

function showSuccess(msg) {
  const el = document.getElementById('success-banner');
  el.textContent = msg;
  el.classList.add('visible');
  document.getElementById('error-banner').classList.remove('visible');
}

function hideSuccess() {
  document.getElementById('success-banner').classList.remove('visible');
}

function setLoading(on) {
  document.getElementById('spinner').style.display  = on ? 'block' : 'none';
  document.getElementById('submit-btn').disabled    = on;
}

function onFileChosen(input) {
  document.getElementById('file-name-display').textContent =
    input.files.length ? input.files[0].name : 'No file chosen';

  window._rosterSection = '';
  if (!input.files.length) { updateActivityId(); return; }

  const reader = new FileReader();
  reader.onload = function(e) {
    try {
      const text     = e.target.result;
      const lines    = text.replace(/\r/g, '').split('\n').filter(l => l.trim());
      if (lines.length < 2) { updateActivityId(); return; }
      const headers  = lines[0].split(',').map(h => h.trim().replace(/^"|"$/g, ''));
      const secIdx   = headers.findIndex(h => h.toLowerCase() === 'section');
      if (secIdx === -1) { updateActivityId(); return; }
      const firstRow = lines[1].split(',').map(c => c.trim().replace(/^"|"$/g, ''));
      window._rosterSection = firstRow[secIdx] || '';
    } catch (_) {
      window._rosterSection = '';
    }
    updateActivityId();
  };
  reader.readAsText(input.files[0]);
}


// ── Submit (Add / Update Activity) ────────────────────────────────────

async function submitActivity() {
  hideError();
  hideSuccess();

  const activityName  = document.getElementById('f-activity-name').value.trim();
  const year          = document.getElementById('f-year').value.trim();
  const semester      = document.getElementById('f-semester').value;
  const activityId    = document.getElementById('f-activity-id').value.trim();
  const rosterInput   = document.getElementById('f-roster');
  const enabledRadio  = document.querySelector('input[name="f-enabled"]:checked');
  const enabled       = enabledRadio ? enabledRadio.value === 'true' : true;
  const isUpdate      = !!document.getElementById('modal-overlay').dataset.activityId;

  if (!activityName) { showError('Activity Name is required.'); return; }
  if (!year)         { showError('Year is required.'); return; }
  if (!semester)     { showError('Semester is required.'); return; }

  const yearInt = parseInt(year, 10);
  if (isNaN(yearInt) || yearInt < 2000 || yearInt > 2099) {
    showError('Year must be a 4-digit number between 2000 and 2099.');
    return;
  }

  // Activity ID validation only on create (it's locked on update).
  // Skip validation for tbd-xxx preview strings (server will resolve them).
  const idLooksTbd = activityId.includes('-tbd-') || activityId.endsWith('-tbd');
  if (!isUpdate && activityId && !idLooksTbd && !RFC1123_RE.test(activityId)) {
    showError(
      'Activity ID has an invalid format.\\n' +
      'Use only lowercase letters, digits, hyphens (-) and dots (.).\\n' +
      'Must start and end with a letter or digit.\\n' +
      'Example: intro-to-ai-2026-fall-11637-b'
    );
    return;
  }

  // Block if activity_id is manually set and already taken (issue 5)
  if (!isUpdate && activityId && !idLooksTbd) {
    const badge = document.getElementById('id-valid-badge');
    if (badge && badge.textContent.includes('already in use')) {
      showError('That Activity ID is already in use. Please choose a different one.');
      return;
    }
  }

  setLoading(true);

  // ── If a roster is present, use the roster endpoint ──────────────
  if (rosterInput.files.length) {
    const fd = new FormData();
    fd.append('activity_name',    activityName);
    fd.append('year',             String(yearInt));
    fd.append('semester',         semester);
    fd.append('instructor_email', INSTRUCTOR_EMAIL);
    fd.append('roster',           rosterInput.files[0]);
    // Pass existing id when updating (especially TBD ids needing section upgrade, 1c)
    if (isUpdate) {
      const existingId = document.getElementById('modal-overlay').dataset.activityId;
      fd.append('activity_id', existingId);
    } else if (activityId && !activityId.includes('tbd-xxx')) {
      fd.append('activity_id', activityId);
    }

    try {
      const resp = await fetch('/api/activity/roster', {
        method:  'POST',
        headers: { 'Authorization': 'Bearer ' + BEARER_TOKEN },
        body:    fd,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showError(typeof data.detail === 'string'
          ? data.detail : JSON.stringify(data.detail || 'Server error ' + resp.status));
        setLoading(false);
        return;
      }
      // After roster upload, also patch enabled + activity_name
      const resolvedId = data.activity_id;
      await _patchActivityMeta(resolvedId, activityName, enabled);
      const label = data.is_new_activity ? 'created' : 'updated';
      showSuccess('Activity "' + data.activity_name + '" ' + label + ' \u00b7 '
        + data.enrolled_count + ' enrolled, ' + data.skipped_count + ' updated.');
    } catch (err) {
      showError('Network error: ' + err.message);
      setLoading(false);
      return;
    }
  } else {
    // ── No roster: create/update the activity record directly ────────
    const fd = new FormData();
    fd.append('activity_name', activityName);
    fd.append('year',          String(yearInt));
    fd.append('semester',      semester);
    fd.append('enabled',       String(enabled));
    // Always send activity_id when updating; on create only if manually set
    if (isUpdate) {
      const existingId = document.getElementById('modal-overlay').dataset.activityId;
      fd.append('activity_id', existingId);
    } else if (activityId && !activityId.includes('tbd-xxx')) {
      fd.append('activity_id', activityId);
    }

    try {
      const resp = await fetch('/api/activity', {
        method:  'POST',
        headers: { 'Authorization': 'Bearer ' + BEARER_TOKEN },
        body:    fd,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showError(typeof data.detail === 'string'
          ? data.detail : JSON.stringify(data.detail || 'Server error ' + resp.status));
        setLoading(false);
        return;
      }
      showSuccess('Activity "' + activityName + '" ' + (isUpdate ? 'updated' : 'created') + '.');
    } catch (err) {
      showError('Network error: ' + err.message);
      setLoading(false);
      return;
    }
  }

  setTimeout(async () => {
    closeModal();
    await refreshActivities();
  }, 1200);
}

/** PATCH activity_name + enabled on an existing activity. */
async function _patchActivityMeta(activityId, activityName, enabled) {
  const fd = new FormData();
  fd.append('activity_id',   activityId);
  fd.append('activity_name', activityName);
  fd.append('enabled',       String(enabled));
  await fetch('/api/activity', {
    method:  'POST',
    headers: { 'Authorization': 'Bearer ' + BEARER_TOKEN },
    body:    fd,
  });
}

// ── Enable/Disable toggle (per-card) ─────────────────────────────────

async function toggleActivity(activityId, currentEnabled, btn) {
  const newEnabled = !currentEnabled;
  btn.disabled = true;

  try {
    const resp = await fetch('/api/activity/' + encodeURIComponent(activityId) + '/enabled', {
      method:  'PATCH',
      headers: {
        'Authorization': 'Bearer ' + BEARER_TOKEN,
        'Content-Type':  'application/json',
      },
      body: JSON.stringify({ enabled: newEnabled }),
    });

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      alert('Toggle failed: ' + (data.detail || 'Server error ' + resp.status));
      btn.disabled = false;
      return;
    }

    await refreshActivities();

  } catch (err) {
    alert('Network error: ' + err.message);
    btn.disabled = false;
  }
}

// ── Update Roster / Activity (per-card file input) ────────────────────

async function onUpdateRosterChosen(input, activityId) {
  if (!input.files.length) return;
  const file = input.files[0];

  const btn = input.previousElementSibling;
  const origLabel = btn.textContent;
  btn.textContent = '\u23f3 Uploading\u2026';
  btn.disabled    = true;

  const fd = new FormData();
  fd.append('activity_id', activityId);
  fd.append('roster',      file);

  try {
    const resp = await fetch('/api/activity/roster/update', {
      method:  'POST',
      headers: { 'Authorization': 'Bearer ' + BEARER_TOKEN },
      body:    fd,
    });

    const data = await resp.json().catch(() => ({}));

    if (!resp.ok) {
      const msg = typeof data.detail === 'string'
        ? data.detail : JSON.stringify(data.detail || 'Server error ' + resp.status);
      alert('Update Activity failed:\\n\\n' + msg);
      btn.textContent = origLabel;
      btn.disabled    = false;
      input.value     = '';
      return;
    }

    await refreshActivities();

  } catch (err) {
    alert('Network error: ' + err.message);
    btn.textContent = origLabel;
    btn.disabled    = false;
    input.value     = '';
  }
}

// ── Refresh activity cards ────────────────────────────────────────────

async function refreshActivities() {
  try {
    const resp = await fetch('/api/dashboard-cards', {
      headers: { 'Authorization': 'Bearer ' + BEARER_TOKEN },
    });
    if (resp.ok) {
      document.getElementById('activity-container').innerHTML = await resp.text();
    } else {
      window.location.reload();
    }
  } catch (_) {
    window.location.reload();
  }
}

// ── Delete-confirm dialog ─────────────────────────────────────────────

let _pendingDeleteId = null;

function openDeleteConfirm(activityId) {
  _pendingDeleteId = activityId;
  document.getElementById('del-body').innerHTML =
    'Are you sure you want to delete activity <code>' + activityId + '</code>?' +
    '<br><br>All activity records, including submissions, will be deleted as well.';
  document.getElementById('btn-confirm-delete').disabled = false;
  document.getElementById('btn-no-delete').disabled      = false;
  document.getElementById('del-spinner').style.display   = 'none';
  document.getElementById('delete-overlay').classList.add('open');
  setTimeout(() => document.getElementById('btn-no-delete').focus(), 50);
}

function closeDeleteConfirm() {
  document.getElementById('delete-overlay').classList.remove('open');
  _pendingDeleteId = null;
}

document.getElementById('delete-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeDeleteConfirm();
});

async function confirmDelete() {
  if (!_pendingDeleteId) return;
  const activityId = _pendingDeleteId;

  document.getElementById('btn-confirm-delete').disabled = true;
  document.getElementById('btn-no-delete').disabled      = true;
  document.getElementById('del-spinner').style.display   = 'block';

  try {
    const resp = await fetch('/api/activity/' + encodeURIComponent(activityId), {
      method:  'DELETE',
      headers: { 'Authorization': 'Bearer ' + BEARER_TOKEN },
    });

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      document.getElementById('del-body').innerHTML +=
        '<br><br><span style="color:#a50e0e;font-weight:600">Error: ' +
        (data.detail || 'Server error ' + resp.status) + '</span>';
      document.getElementById('btn-confirm-delete').disabled = false;
      document.getElementById('btn-no-delete').disabled      = false;
      document.getElementById('del-spinner').style.display   = 'none';
      return;
    }

    closeDeleteConfirm();
    await refreshActivities();

  } catch (err) {
    document.getElementById('del-body').innerHTML +=
      '<br><br><span style="color:#a50e0e;font-weight:600">Network error: ' + err.message + '</span>';
    document.getElementById('btn-confirm-delete').disabled = false;
    document.getElementById('btn-no-delete').disabled      = false;
    document.getElementById('del-spinner').style.display   = 'none';
  }
}"""


def _build_dashboard_script(safe_token: str, safe_email: str) -> str:
    """Return the full <script>…</script> block with token/email injected."""
    js = _DASHBOARD_JS
    js = js.replace('__BEARER_TOKEN__',     safe_token)
    js = js.replace('__INSTRUCTOR_EMAIL__', safe_email)
    return '<script>\n' + js + '\n</script>'


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
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            f"<h2>Access denied.</h2>"
            f"<p><b>{email}</b> is not registered as an instructor.</p>"
            "<p><a href='/dashboard'>Sign in with a different account</a></p>"
            "</body></html>",
            status_code=403,
        )

    cards_html = _build_activity_cards(instructor, db)

    # Escape values that go into JS string literals
    safe_token = token.replace("\\", "\\\\").replace("`", "\\`")
    safe_email = email.replace("\\", "\\\\").replace("`", "\\`")

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Instructor Dashboard</title>
  {DASHBOARD_CSS}
</head>
<body>

<!-- ═══════════════════════════════════════════
     Header
════════════════════════════════════════════ -->
<header>
  <h1>📚 Instructor Dashboard</h1>
  <div class="header-right">
    <button class="btn-add-activity" onclick="openModal()">
      ＋ Add Activity
    </button>
    <span>{instructor.name or email}</span>
  </div>
</header>

<!-- ═══════════════════════════════════════════
     Activity list (refreshed via JS after upload)
════════════════════════════════════════════ -->
<div class="container" id="activity-container">
  {cards_html}
</div>

<!-- ═══════════════════════════════════════════
     Add / Update Activity modal
════════════════════════════════════════════ -->
<div class="modal-overlay" id="modal-overlay" role="dialog"
     aria-modal="true" aria-labelledby="modal-title">
  <div class="modal">

    <div class="modal-header">
      <h2 id="modal-title">Add Activity</h2>
      <button class="modal-close" onclick="closeModal()" aria-label="Close">&times;</button>
    </div>

    <div class="modal-body">
      <!-- Error / success banners -->
      <div class="error-banner"   id="error-banner"></div>
      <div class="success-banner" id="success-banner"></div>

      <!-- Activity Name -->
      <div class="field">
        <label for="f-activity-name">Activity Name <span class="req">*</span></label>
        <input type="text" id="f-activity-name" placeholder="e.g. Introduction to AI">
      </div>

      <!-- Year + Semester (side by side) -->
      <div style="display:flex;gap:14px">
        <div class="field" style="flex:1">
          <label for="f-year">Year <span class="req">*</span></label>
          <input type="number" id="f-year" placeholder="e.g. 2026" min="2000" max="2099">
        </div>
        <div class="field" style="flex:1">
          <label for="f-semester">Semester <span class="req">*</span></label>
          <select id="f-semester">
            <option value="">— select —</option>
            <option>Fall</option>
            <option>Spring</option>
            <option>Summer</option>
          </select>
        </div>
      </div>

      <!-- Instructor email (pre-filled, read-only) -->
      <div class="field">
        <label for="f-instructor">Instructor Email <span class="req">*</span></label>
        <input type="text" id="f-instructor" value="{safe_email}" readonly
               style="background:#f8f9fa;color:#555">
      </div>

      <!-- Enable / Disable radio -->
      <div class="field">
        <label>Status <span class="req">*</span></label>
        <div style="display:flex;gap:20px;margin-top:4px">
          <label style="font-weight:400;display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="radio" name="f-enabled" id="f-enabled-yes" value="true" checked>
            Enable
          </label>
          <label style="font-weight:400;display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="radio" name="f-enabled" id="f-enabled-no" value="false">
            Disable
          </label>
        </div>
      </div>

      <!-- Roster file (optional) -->
      <div class="field">
        <label>Roster CSV <span style="color:#888;font-weight:400">(optional)</span></label>
        <div class="file-row">
          <button class="btn-browse" type="button" id="roster-btn"
                  onclick="document.getElementById('f-roster').click()">
            Add Roster
          </button>
          <input type="file" id="f-roster" accept=".csv,text/csv"
                 onchange="onFileChosen(this)">
          <span class="file-name" id="file-name-display"></span>
        </div>
        <div class="hint">
          Expected columns: First Name, Last Name, SID, Email, Role, Section.
          All rows must share a single Section value.
        </div>
      </div>

      <!-- Activity ID — last field (1a); locked once a real section is set (xd) -->
      <div class="field">
        <label for="f-activity-id">Activity ID
          <span id="id-optional-label" style="color:#888;font-weight:400">(optional)</span>
          <span id="id-locked-label"   style="color:#888;font-weight:400;display:none">(locked — cannot change after creation)</span>
          <span id="id-valid-badge" style="display:none;margin-left:8px;font-size:.78rem;
                font-weight:600;padding:1px 7px;border-radius:10px"></span>
        </label>
        <input type="text" id="f-activity-id"
               placeholder="Auto-generated from fields above"
               oninput="onActivityIdInput(this)">
        <div class="hint">
          Auto-generated from Activity Name + Year + Semester + Section.
          Without a roster, the Section component is set to <code>tbd-###</code>
          (a unique 3-digit placeholder) and will be updated automatically when a
          roster is first added.<br>
          You may edit it manually, but only during activity creation.
          The Activity ID must follow RFC&nbsp;1123 subdomain format:
          lowercase letters, digits, <code>-</code>, and <code>.</code> only;
          must start and end with a letter or digit
          (e.g.&nbsp;<code>intro-to-ai-2026-fall-11637-b</code>).
        </div>
      </div>

    </div><!-- /modal-body -->

    <div class="modal-footer">
      <div class="spinner" id="spinner"></div>
      <button class="btn-secondary" onclick="closeModal()">Cancel</button>
      <button class="btn-primary"   id="submit-btn" onclick="submitActivity()">Submit</button>
    </div>

  </div><!-- /modal -->
</div><!-- /modal-overlay -->

<!-- ═══════════════════════════════════════════
     Delete-confirm dialog  (reuses modal-overlay pattern)
════════════════════════════════════════════ -->
<div class="modal-overlay" id="delete-overlay" role="dialog"
     aria-modal="true" aria-labelledby="del-title">
  <div class="confirm-dialog">

    <div class="confirm-header">
      <h2 id="del-title">⚠️ Confirm Deletion</h2>
    </div>

    <div class="confirm-body" id="del-body">
      <!-- filled by JS -->
    </div>

    <div class="confirm-footer">
      <div class="del-spinner" id="del-spinner"></div>
      <button class="btn-no-delete"      id="btn-no-delete"
              onclick="closeDeleteConfirm()">Do NOT Delete</button>
      <button class="btn-confirm-delete" id="btn-confirm-delete"
              onclick="confirmDelete()">DELETE</button>
    </div>

  </div>
</div>

{_build_dashboard_script(safe_token, safe_email)}

</body>
</html>"""
    response = HTMLResponse(html)
    # Persist token in a cookie so the user stays logged in across refreshes
    response.set_cookie("google_token", token, httponly=True, samesite="lax")
    return response


@app.post("/api/activity/roster/update")
async def update_roster(
    request: Request,
    roster: UploadFile = File(...),
    activity_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Update the enrollment list for an existing activity from a new CSV roster.

    • Users in the CSV who are not yet enrolled → added (user created if needed).
    • Users currently enrolled as Students who are absent from the CSV → removed.
      If a removed user has no other activity enrollments, the user record is also
      deleted.
    • Non-Student enrollments (Instructor, TA, Admin) are never removed.
    • Existing enrollments whose role changed in the CSV are updated.

    Requires a valid instructor Bearer token.
    """
    instructor = require_instructor(request, db)

    activity = db.query(Activity).filter(
        Activity.activity_id == activity_id
    ).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Verify this instructor owns the activity
    if activity not in instructor.activities:
        raise HTTPException(
            status_code=403,
            detail="You are not assigned to this activity",
        )

    # ── Parse CSV ──────────────────────────────────────────────────────
    raw_bytes = await roster.read()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="Roster CSV is empty")

    required_cols = {"Email", "Role"}
    missing = required_cols - set(reader.fieldnames or [])
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV is missing required columns: {missing}",
        )

    for idx, row in enumerate(rows, start=2):
        role = row.get("Role", "").strip()
        if role not in _VALID_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Row {idx}: invalid Role '{role}'. Must be one of {sorted(_VALID_ROLES)}.",
            )

    # ── Build the set of emails in the new roster ──────────────────────
    roster_emails = {
        r["Email"].strip().lower()
        for r in rows
        if r.get("Email", "").strip()
    }

    # ── Remove Student enrollments not in the new roster ──────────────
    removed_count = 0
    current_student_uas = (
        db.query(UserActivity)
        .filter(
            UserActivity.activity_id == activity_id,
            UserActivity.role        == "Student",
        )
        .all()
    )
    for ua in current_student_uas:
        user = db.query(User).filter(User.id == ua.user_id).first()
        if not user:
            continue
        if user.email.lower() not in roster_emails:
            # Delete submissions for this enrollment
            db.query(Submission).filter(
                Submission.user_activity_id == ua.id
            ).delete(synchronize_session=False)
            db.delete(ua)
            db.flush()
            # Delete the user entirely if they have no remaining enrollments
            remaining = (
                db.query(UserActivity)
                .filter(UserActivity.user_id == user.id)
                .count()
            )
            if remaining == 0:
                db.delete(user)
            removed_count += 1

    db.flush()

    # ── Upsert users and enrollments for every row in the new roster ───
    added_count   = 0
    updated_count = 0

    for row in rows:
        email = row.get("Email", "").strip()
        if not email:
            continue

        first = row.get("First Name", "").strip()
        last  = row.get("Last Name",  "").strip()
        sid   = row.get("SID", "").strip() or None

        if first and last:
            name = f"{first} {last}"
        elif first:
            name = first
        elif last:
            name = last
        else:
            name = email

        role = row.get("Role", "").strip()

        # Upsert user
        user = db.query(User).filter(User.email == email).first()
        if user:
            if name and name != user.name:
                user.name = name
            if first:
                user.first_name = first
            if last:
                user.last_name = last
            if sid is not None:
                user.user_id = sid
        else:
            user = User(name=name, email=email,
                        first_name=first or None, last_name=last or None,
                        user_id=sid)
            db.add(user)
            db.flush()

        # Upsert enrollment
        ua = (
            db.query(UserActivity)
            .filter(
                UserActivity.user_id     == user.id,
                UserActivity.activity_id == activity_id,
            )
            .first()
        )
        if ua:
            if ua.role != role:
                ua.role = role
                updated_count += 1
        else:
            db.add(UserActivity(user_id=user.id, activity_id=activity_id, role=role))
            added_count += 1

    db.commit()
    return {
        "status":        "ok",
        "activity_id":   activity_id,
        "added_count":   added_count,
        "removed_count": removed_count,
        "updated_count": updated_count,
    }


@app.get("/api/dashboard-cards", response_class=HTMLResponse)
async def dashboard_cards(request: Request, db: Session = Depends(get_db)):
    """
    Return just the inner HTML for the activity cards belonging to the
    authenticated instructor.  Called by the dashboard JS after a successful
    roster upload to refresh the list without a full page reload.
    """
    instructor = require_instructor(request, db)
    return HTMLResponse(_build_activity_cards(instructor, db))


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
