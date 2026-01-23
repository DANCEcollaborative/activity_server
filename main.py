"""
Database models for the activity server
Install: pip install sqlalchemy psycopg2-binary
"""

from sqlalchemy import create_engine, Column, String, Integer, ForeignKey, LargeBinary, Float, Table, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from datetime import datetime
import os
"""
Main FastAPI application for the activity server
Install: pip install fastapi uvicorn python-multipart pydantic

Run: uvicorn main:app --host 0.0.0.0 --port 8100
"""

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
import io
import os
import base64
import json
from models import db, Activity, UserSubmission, Instructor

# Initialize FastAPI
app = FastAPI(title="Activity Server API", version="1.0.0")

# CORS middleware for portal access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OAuth bearer token security
security = HTTPBearer()

# Google OAuth configuration
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')

# Initialize database on startup
@app.on_event("startup")
def startup_event():
    db.create_tables()

# Dependency to get database session
def get_db():
    database = db.get_session()
    try:
        yield database
    finally:
        database.close()

# Pydantic models for API
class SubmissionCreate(BaseModel):
    user: str
    name: str
    activity: str
    email: Optional[str] = None
    prequiz_token: Optional[str] = None
    postquiz_token: Optional[str] = None

class ActivityCreate(BaseModel):
    activity_id: str
    activity_name: str
    enabled: bool = True

class InstructorCreate(BaseModel):
    email: str
    name: Optional[str] = None
    activity_id: str

class ScoreUpdate(BaseModel):
    activity_id: str
    user: str
    score: float

# Helper functions
def verify_google_token(token: str) -> dict:
    """Verify Google OAuth token and return user info"""
    try:
        # JWT has 3 parts: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError("Invalid token format")
        
        # Decode payload (add padding if needed)
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        user_info = json.loads(decoded)
        
        return user_info
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

def get_current_instructor(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_db)
) -> Instructor:
    """Verify instructor from Google OAuth token"""
    token = credentials.credentials
    
    try:
        user_info = verify_google_token(token)
        email = user_info.get('email')
        
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")
        
        # Check if email is in instructors table
        instructor = session.query(Instructor).filter(Instructor.email == email).first()
        
        if not instructor:
            raise HTTPException(
                status_code=403, 
                detail=f"Email {email} is not authorized as an instructor"
            )
        
        return instructor
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

# API Endpoints

@app.get("/")
def root():
    return {"message": "Activity Server API", "docs": "/docs"}

# Submission API
@app.post("/api/submit")
async def submit_assignment(
    user: str = Form(...),
    name: str = Form(...),
    activity: str = Form(...),
    email: Optional[str] = Form(None),
    prequiz_token: Optional[str] = Form(None),
    postquiz_token: Optional[str] = Form(None),
    notebook: UploadFile = File(...),
    session: Session = Depends(get_db)
):
    """Submit a notebook for grading"""
    
    # Verify activity exists
    activity_obj = session.query(Activity).filter(Activity.activity_id == activity).first()
    if not activity_obj:
        raise HTTPException(status_code=404, detail=f"Activity '{activity}' not found")
    
    # Read notebook content
    notebook_content = await notebook.read()
    
    # Check if submission already exists
    existing = session.query(UserSubmission).filter(
        UserSubmission.activity_id == activity,
        UserSubmission.user == user
    ).first()
    
    if existing:
        # Update existing submission
        existing.name = name
        existing.email = email
        existing.prequiz_token = prequiz_token
        existing.postquiz_token = postquiz_token
        existing.notebook = notebook_content
        existing.notebook_filename = notebook.filename
        existing.score = None  # Reset score on resubmission
    else:
        # Create new submission
        submission = UserSubmission(
            activity_id=activity,
            user=user,
            name=name,
            email=email,
            prequiz_token=prequiz_token,
            postquiz_token=postquiz_token,
            notebook=notebook_content,
            notebook_filename=notebook.filename
        )
        session.add(submission)
    
    session.commit()
    
    return {"status": "success", "message": "Submission received", "user": user, "activity": activity}

# Add activity
@app.post("/api/activity")
async def create_activity(
    activity_id: str = Form(...),
    activity_name: str = Form(...),
    enabled: bool = Form(True),
    grading_notebook: UploadFile = File(...),
    session: Session = Depends(get_db)
):
    """Create a new activity with grading notebook"""
    
    # Check if activity already exists
    existing = session.query(Activity).filter(Activity.activity_id == activity_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Activity already exists")
    
    grading_content = await grading_notebook.read()
    
    activity = Activity(
        activity_id=activity_id,
        activity_name=activity_name,
        enabled=enabled,
        grading_notebook=grading_content,
        grading_notebook_filename=grading_notebook.filename
    )
    
    session.add(activity)
    session.commit()
    
    return {"status": "success", "activity_id": activity_id, "activity_name": activity_name}

# Delete activity
@app.delete("/api/activity/{activity_id}")
async def delete_activity(
    activity_id: str,
    session: Session = Depends(get_db)
):
    """Delete an activity and all associated submissions"""
    
    activity = session.query(Activity).filter(Activity.activity_id == activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    
    session.delete(activity)
    session.commit()
    
    return {"status": "success", "message": f"Activity '{activity_id}' deleted"}

# Add instructor to activity
@app.post("/api/instructor")
async def add_instructor(
    data: InstructorCreate,
    session: Session = Depends(get_db)
):
    """Add an instructor to an activity"""
    
    # Check if activity exists
    activity = session.query(Activity).filter(Activity.activity_id == data.activity_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    
    # Check if instructor exists, create if not
    instructor = session.query(Instructor).filter(Instructor.email == data.email).first()
    
    if not instructor:
        instructor = Instructor(
            email=data.email,
            name=data.name
        )
        session.add(instructor)
    else:
        # Update name if provided
        if data.name:
            instructor.name = data.name
    
    # Add instructor to activity if not already added
    if instructor not in activity.instructors:
        activity.instructors.append(instructor)
    
    session.commit()
    
    return {"status": "success", "message": f"Instructor '{data.email}' added to activity '{data.activity_id}'"}

# Update score
@app.put("/api/score")
async def update_score(
    data: ScoreUpdate,
    session: Session = Depends(get_db)
):
    """Update the score for a user's submission"""
    
    submission = session.query(UserSubmission).filter(
        UserSubmission.activity_id == data.activity_id,
        UserSubmission.user == data.user
    ).first()
    
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    submission.score = data.score
    session.commit()
    
    return {"status": "success", "activity": data.activity_id, "user": data.user, "score": data.score}

# Get activities by user email
@app.get("/api/activities/by-email/{email}")
async def get_activities_by_email(
    email: str,
    session: Session = Depends(get_db)
):
    """Get all enabled activities for a user by email"""
    
    # Find all submissions with this email
    submissions = session.query(UserSubmission).filter(
        UserSubmission.email == email
    ).all()
    
    # Get unique activity IDs
    activity_ids = list(set([sub.activity_id for sub in submissions]))
    
    # Get activity details (only enabled ones)
    activities = session.query(Activity).filter(
        Activity.activity_id.in_(activity_ids),
        Activity.enabled == True
    ).all()
    
    return {
        "email": email,
        "activities": [
            {
                "activity_id": act.activity_id,
                "activity_name": act.activity_name,
                "enabled": act.enabled
            }
            for act in activities
        ]
    }

# List all activities
@app.get("/api/activities")
async def list_activities(
    enabled_only: bool = True,
    session: Session = Depends(get_db)
):
    """List all activities"""
    
    query = session.query(Activity)
    if enabled_only:
        query = query.filter(Activity.enabled == True)
    
    activities = query.all()
    
    return {
        "activities": [
            {
                "activity_id": act.activity_id,
                "activity_name": act.activity_name,
                "enabled": act.enabled,
                "instructor_count": len(act.instructors),
                "submission_count": len(act.users)
            }
            for act in activities
        ]
    }

# Instructor dashboard
@app.get("/dashboard", response_class=HTMLResponse)
async def instructor_dashboard(
    request: Request,
    session: Session = Depends(get_db)
):
    """Web interface for instructors to view submissions - requires Google OAuth"""
    
    # Get token from cookie or query parameter
    token = request.cookies.get('instructor_token') or request.query_params.get('token')
    
    if not token:
        return get_instructor_login_page()
    
    try:
        user_info = verify_google_token(token)
        email = user_info.get('email')
        
        instructor = session.query(Instructor).filter(Instructor.email == email).first()
        if not instructor:
            return get_instructor_login_page(error="Email not authorized as instructor")
        
    except Exception as e:
        return get_instructor_login_page(error="Invalid or expired token")
    
    # Get all activities for this instructor
    activities = instructor.activities
    
    if not activities:
        return f"""
        <html>
        <head>
            <title>Activity Dashboard</title>
            {get_dashboard_styles()}
        </head>
        <body>
            <div class="header">
                <h1>Activity Dashboard</h1>
                <div class="user-info">
                    <span>👤 {instructor.name or instructor.email}</span>
                    <a href="/dashboard/logout" class="logout-btn">Logout</a>
                </div>
            </div>
            <div class="container">
                <h2>No activities assigned</h2>
                <p>Contact an administrator to assign activities to your account.</p>
            </div>
        </body>
        </html>
        """
    
    # Build HTML
    html = f"""
    <html>
    <head>
        <title>Activity Dashboard</title>
        {get_dashboard_styles()}
        <script>
            function toggleActivity(id) {{
                var content = document.getElementById(id);
                content.classList.toggle('active');
            }}
        </script>
    </head>
    <body>
        <div class="header">
            <h1>Activity Dashboard</h1>
            <div class="user-info">
                <span>👤 {instructor.name or instructor.email}</span>
                <a href="/dashboard/logout" class="logout-btn">Logout</a>
            </div>
        </div>
        <div class="container">
    """
    
    for idx, activity in enumerate(activities):
        collapsible = len(activities) > 1
        activity_div_id = f"activity_{idx}"
        
        enabled_badge = "🟢 Enabled" if activity.enabled else "🔴 Disabled"
        
        if collapsible:
            html += f'''
            <h2 onclick="toggleActivity('{activity_div_id}')" class="activity-header">
                ▼ {activity.activity_name} <span class="badge">{enabled_badge}</span>
            </h2>
            <div id="{activity_div_id}" class="collapsible-content">
            '''
        else:
            html += f'<h2>{activity.activity_name} <span class="badge">{enabled_badge}</span></h2>'
        
        html += f'<p class="activity-id">Activity ID: <code>{activity.activity_id}</code></p>'
        
        html += """
        <table>
            <tr>
                <th>Name</th>
                <th>User</th>
                <th>Email</th>
                <th>Score</th>
                <th>Notebook</th>
            </tr>
        """
        
        for submission in activity.users:
            score_display = f"{submission.score:.2f}" if submission.score is not None else "Not graded"
            email_display = submission.email or "—"
            html += f"""
            <tr>
                <td>{submission.name}</td>
                <td>{submission.user}</td>
                <td>{email_display}</td>
                <td>{score_display}</td>
                <td><a href="/download/{activity.activity_id}/{submission.user}?token={token}">Download</a></td>
            </tr>
            """
        
        html += "</table>"
        
        if collapsible:
            html += "</div>"
    
    html += """
        </div>
    </body>
    </html>
    """
    
    return html

def get_dashboard_styles():
    """Return CSS styles for dashboard"""
    return """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: Arial, sans-serif; 
            background: #f5f5f5;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .header h1 { font-size: 24px; }
        .user-info {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        .logout-btn {
            color: white;
            text-decoration: none;
            padding: 8px 16px;
            background: rgba(255,255,255,0.2);
            border-radius: 4px;
            transition: background 0.3s;
        }
        .logout-btn:hover {
            background: rgba(255,255,255,0.3);
        }
        .container {
            max-width: 1200px;
            margin: 40px auto;
            padding: 0 20px;
        }
        h2 { 
            color: #333; 
            margin-top: 30px;
            margin-bottom: 10px;
        }
        .activity-header {
            cursor: pointer; 
            background: #f0f0f0; 
            padding: 15px; 
            border-radius: 8px;
            transition: background 0.3s;
        }
        .activity-header:hover { 
            background: #e0e0e0; 
        }
        .activity-id {
            color: #666;
            margin-bottom: 15px;
            font-size: 14px;
        }
        .activity-id code {
            background: #f0f0f0;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: monospace;
        }
        .badge {
            font-size: 12px;
            padding: 4px 8px;
            border-radius: 4px;
            background: rgba(255,255,255,0.2);
            font-weight: normal;
        }
        table { 
            border-collapse: collapse; 
            width: 100%; 
            margin-bottom: 30px; 
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        th, td { 
            border: 1px solid #ddd; 
            padding: 12px; 
            text-align: left; 
        }
        th { 
            background-color: #667eea; 
            color: white;
            font-weight: 600;
        }
        tr:nth-child(even) { 
            background-color: #f9f9f9; 
        }
        tr:hover {
            background-color: #f0f4ff;
        }
        .collapsible-content { 
            display: none; 
        }
        .collapsible-content.active { 
            display: block; 
        }
        a { 
            color: #667eea; 
            text-decoration: none; 
        }
        a:hover { 
            text-decoration: underline; 
        }
    </style>
    """

def get_instructor_login_page(error=None):
    """Return Google OAuth login page for instructors"""
    error_html = f'<div class="error">{error}</div>' if error else ''
    
    return f"""
    <html>
    <head>
        <title>Instructor Login</title>
        <script src="https://accounts.google.com/gsi/client" async defer></script>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: Arial, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
            }}
            .container {{
                background: white;
                border-radius: 12px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                padding: 40px;
                max-width: 400px;
                width: 100%;
                text-align: center;
            }}
            h1 {{
                color: #333;
                margin-bottom: 20px;
            }}
            .subtitle {{
                color: #666;
                margin-bottom: 30px;
            }}
            .error {{
                background: #ffebee;
                color: #c62828;
                padding: 12px;
                border-radius: 6px;
                margin-bottom: 20px;
                border-left: 4px solid #c62828;
            }}
            #googleSignInDiv {{
                display: flex;
                justify-content: center;
                margin-top: 20px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎓 Instructor Dashboard</h1>
            <p class="subtitle">Sign in to view and manage submissions</p>
            {error_html}
            
            <div id="g_id_onload"
                 data-client_id="{GOOGLE_CLIENT_ID}"
                 data-callback="handleCredentialResponse"
                 data-auto_prompt="false">
            </div>
            <div id="googleSignInDiv"></div>
        </div>
        
        <script>
            function handleCredentialResponse(response) {{
                document.cookie = `instructor_token=${{response.credential}}; path=/; max-age=3600`;
                window.location.href = '/dashboard';
            }}
            
            window.onload = function() {{
                google.accounts.id.initialize({{
                    client_id: '{GOOGLE_CLIENT_ID}',
                    callback: handleCredentialResponse
                }});
                
                google.accounts.id.renderButton(
                    document.getElementById('googleSignInDiv'),
                    {{ theme: 'outline', size: 'large', text: 'signin_with', shape: 'rectangular' }}
                );
            }};
            
            window.handleCredentialResponse = handleCredentialResponse;
        </script>
    </body>
    </html>
    """

@app.get("/dashboard/logout")
async def instructor_logout():
    """Logout instructor"""
    response = HTMLResponse(content="""
        <html>
        <head>
            <meta http-equiv="refresh" content="2;url=/dashboard">
            <style>
                body {
                    font-family: Arial, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    text-align: center;
                }
            </style>
        </head>
        <body>
            <div>
                <h1>✓ Logged out</h1>
                <p>Redirecting to login...</p>
            </div>
        </body>
        </html>
    """)
    response.delete_cookie("instructor_token")
    return response

# Download notebook endpoint
@app.get("/download/{activity_id}/{user}")
async def download_notebook(
    activity_id: str,
    user: str,
    token: str = None,
    session: Session = Depends(get_db)
):
    """Download a student's notebook - requires instructor authentication"""
    
    if not token:
        raise HTTPException(status_code=401, detail="Authentication token required")
    
    try:
        user_info = verify_google_token(token)
        email = user_info.get('email')
        
        instructor = session.query(Instructor).filter(Instructor.email == email).first()
        if not instructor:
            raise HTTPException(status_code=403, detail="Not authorized as instructor")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    
    # Verify instructor has access to this activity
    activity = session.query(Activity).filter(Activity.activity_id == activity_id).first()
    if not activity or instructor not in activity.instructors:
        raise HTTPException(status_code=403, detail="Access denied to this activity")
    
    # Get submission
    submission = session.query(UserSubmission).filter(
        UserSubmission.activity_id == activity_id,
        UserSubmission.user == user
    ).first()
    
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    
    return StreamingResponse(
        io.BytesIO(submission.notebook),
        media_type="application/x-ipynb+json",
        headers={"Content-Disposition": f"attachment; filename={submission.notebook_filename}"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)

Base = declarative_base()

# Association table for activity instructors
activity_instructors = Table(
    'activity_instructors',
    Base.metadata,
    Column('activity_id', String, ForeignKey('activities.activity_id')),
    Column('instructor_id', Integer, ForeignKey('instructors.id'))
)

class Activity(Base):
    __tablename__ = 'activities'
    
    activity_id = Column(String, primary_key=True)
    activity_name = Column(String, nullable=False)  # Display name for activity
    enabled = Column(Boolean, default=True)  # Enable/disable activity
    grading_notebook = Column(LargeBinary)  # Store .ipynb file content
    grading_notebook_filename = Column(String)
    
    # Relationships
    users = relationship("UserSubmission", back_populates="activity", cascade="all, delete-orphan")
    instructors = relationship("Instructor", secondary=activity_instructors, back_populates="activities")

class UserSubmission(Base):
    __tablename__ = 'user_submissions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(String, ForeignKey('activities.activity_id'))
    user = Column(String, nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)  # User email for Google OAuth
    prequiz_token = Column(String, nullable=True)  # Pre-quiz token
    postquiz_token = Column(String, nullable=True)  # Post-quiz token
    notebook = Column(LargeBinary)  # Store .ipynb file content
    notebook_filename = Column(String)
    score = Column(Float, nullable=True)
    submitted_at = Column(String, default=lambda: datetime.utcnow().isoformat())
    
    # Relationships
    activity = relationship("Activity", back_populates="users")
    
    # Ensure unique user per activity
    __table_args__ = (
        {'sqlite_autoincrement': True},
    )

class Instructor(Base):
    __tablename__ = 'instructors'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)  # Google email for OAuth
    name = Column(String, nullable=True)  # Display name from Google
    
    # Relationships
    activities = relationship("Activity", secondary=activity_instructors, back_populates="instructors")

# Database connection and session management
class Database:
    def __init__(self, db_url=None):
        if db_url is None:
            # Default to PostgreSQL
            db_url = os.getenv(
                'DATABASE_URL',
                'postgresql://activity_user:activity_pass@localhost/activity_db'
            )
        
        self.engine = create_engine(db_url)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
    
    def create_tables(self):
        Base.metadata.create_all(bind=self.engine)
    
    def get_session(self):
        return self.SessionLocal()

# Initialize database
db = Database()