# Activity Server Setup Guide

Complete setup guide for starting from scratch with the activity server.

## Prerequisites

- Ubuntu 20.04 or 22.04 (or any Linux with Docker)
- Docker and Docker Compose installed
- Google OAuth Client ID (instructions below)
- 10 GB free disk space

## Quick Start (15 minutes)

### Step 1: Create Project Directory (1 min)

```bash
# Create main directory
mkdir ~/activity_server
cd ~/activity_server

# Create subdirectories
mkdir -p portal logs
```

### Step 2: Save All Files (3 min)

Save the following files from artifacts to your project directory:

```
~/activity_server/
├── docker-compose.yml          # Multi-container orchestration
├── Dockerfile                  # App container definition
├── models.py                   # Database models
├── main.py                    # FastAPI application
├── requirements.txt           # Python dependencies
├── .env                       # Environment config (create from .env.example)
├── .dockerignore              # Build optimization (optional)
└── portal/
    ├── Dockerfile.portal      # Portal container
    ├── nginx-portal.conf      # Web server config
    └── index.html            # Student portal
```

### Step 3: Get Google OAuth Client ID (5 min)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable "Google Identity Services API"
4. Go to "Credentials" → "Create Credentials" → "OAuth 2.0 Client ID"
5. Configure OAuth consent screen if needed
6. Choose "Web application"
7. Add Authorized JavaScript origins:
   - `http://localhost:8100`
   - `http://localhost:3000`
8. Add Authorized redirect URIs:
   - `http://localhost:8100`
   - `http://localhost:3000`
9. Click "Create" and copy the **Client ID**

### Step 4: Configure Environment (2 min)

```bash
cd ~/activity_server

# Create .env file
cat > .env << 'EOF'
# Database
POSTGRES_DB=activity_db
POSTGRES_USER=activity_user
POSTGRES_PASSWORD=activity_pass

# Application
DATABASE_URL=postgresql://activity_user:activity_pass@db:5432/activity_db

# Google OAuth - PASTE YOUR CLIENT ID HERE
GOOGLE_CLIENT_ID=your-actual-client-id.apps.googleusercontent.com

# Server
APP_PORT=8100
APP_WORKERS=4
EOF

# Edit .env and paste your actual Client ID
nano .env
```

### Step 5: Configure Portal (2 min)

```bash
# Edit portal/index.html
nano portal/index.html

# Find and update these two lines:
# Line ~204: data-client_id="YOUR_GOOGLE_CLIENT_ID"
# Line ~220: const GOOGLE_CLIENT_ID = 'YOUR_GOOGLE_CLIENT_ID';

# Replace YOUR_GOOGLE_CLIENT_ID with your actual Client ID
```

### Step 6: Build and Start (2 min)

```bash
cd ~/activity_server

# Build and start all services
docker compose up -d --build

# Check status
docker compose ps

# Should show:
# NAME              STATUS        PORTS
# activity_app      Up           0.0.0.0:8100->8100/tcp
# activity_db       Up           5432/tcp
# student_portal    Up           0.0.0.0:3000->80/tcp
```

### Step 7: Verify Running

```bash
# Test API
curl http://localhost:8100/

# Should return:
# {"message":"Activity Server API","docs":"/docs"}

# Check portal
open http://localhost:3000

# Check dashboard
open http://localhost:8100/dashboard
```

## Detailed File Setup

### Core Application Files

#### 1. models.py
Database models with Google OAuth support:
- `Activity` table: activity_id, activity_name, enabled, grading_notebook
- `UserSubmission` table: user, name, email, prequiz_token, postquiz_token, notebook, score
- `Instructor` table: email, name (no passwords!)

#### 2. main.py
FastAPI application with:
- Google OAuth authentication for instructors
- Student submission API
- Activity management API
- Instructor dashboard
- CORS enabled for portal

#### 3. requirements.txt
Python dependencies:
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
sqlalchemy==2.0.23
psycopg2-binary==2.9.9
python-multipart==0.0.6
pydantic==2.5.0
requests==2.31.0
```

### Docker Configuration

#### 4. Dockerfile
Python 3.11 slim image with:
- PostgreSQL client
- Python dependencies
- Health checks
- Runs on port 8100

#### 5. docker-compose.yml
Three services:
- `db`: PostgreSQL 15 (port 5432, internal only)
- `app`: Activity server (port 8100)
- `portal`: Student portal (port 3000)

Optional:
- `pgadmin`: Database management UI (port 5050, profile: admin)

### Portal Files

#### 6. portal/Dockerfile.portal
Nginx alpine image serving static HTML

#### 7. portal/nginx-portal.conf
Web server configuration with CORS

#### 8. portal/index.html
Student portal with:
- Google OAuth sign-in
- Course selection from database
- JupyterLab launch integration

## Initial Data Setup

### Create Your First Activity

```bash
# Create a dummy grading notebook
echo '{"cells":[]}' > grading_test.ipynb

# Create activity
curl -X POST "http://localhost:8100/api/activity" \
  -F "activity_id=python101" \
  -F "activity_name=Introduction to Python" \
  -F "enabled=true" \
  -F "grading_notebook=@grading_test.ipynb"

# Response:
# {"status":"success","activity_id":"python101","activity_name":"Introduction to Python"}
```

### Add Yourself as Instructor

```bash
# Replace with YOUR Google email
curl -X POST "http://localhost:8100/api/instructor" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your.email@gmail.com",
    "name": "Your Name",
    "activity_id": "python101"
  }'

# Response:
# {"status":"success","message":"Instructor 'your.email@gmail.com' added to activity 'python101'"}
```

### Add Test Student

```bash
# Create dummy student notebook
echo '{"cells":[]}' > student_test.ipynb

# Add student submission
curl -X POST "http://localhost:8100/api/submit" \
  -F "user=student001" \
  -F "name=Test Student" \
  -F "activity=python101" \
  -F "email=student.test@university.edu" \
  -F "notebook=@student_test.ipynb"

# Response:
# {"status":"success","message":"Submission received","user":"student001","activity":"python101"}
```

## Testing the Setup

### Test 1: API Endpoints

```bash
# Root endpoint
curl http://localhost:8100/

# List activities
curl http://localhost:8100/api/activities

# Get activities for email
curl http://localhost:8100/api/activities/by-email/student.test@university.edu

# API documentation
open http://localhost:8100/docs
```

### Test 2: Instructor Dashboard

```bash
# Open dashboard
open http://localhost:8100/dashboard

# Should see:
# 1. "Instructor Dashboard" title
# 2. Google Sign-In button
# 3. No password prompt

# Sign in with your Google account (the one you added as instructor)

# After sign in, should see:
# - Your name/email at top
# - "Introduction to Python" activity
# - Test Student submission
# - Download link
```

### Test 3: Student Portal

```bash
# First, add student with YOUR Google email for testing
curl -X POST "http://localhost:8100/api/submit" \
  -F "user=testuser" \
  -F "name=Your Name" \
  -F "activity=python101" \
  -F "email=your.email@gmail.com" \
  -F "notebook=@student_test.ipynb"

# Open portal
open http://localhost:3000

# Should see:
# 1. "JupyterLab Bot Chat" title
# 2. Google Sign-In button

# Sign in with Google

# After sign in, should see:
# - Your name and email
# - Course dropdown with "Introduction to Python"
# - "Start JupyterLab" button
```

## Verification Checklist

### Database
- [ ] PostgreSQL container running
- [ ] Database `activity_db` created
- [ ] Tables created (activities, user_submissions, instructors)

### API Server
- [ ] App container running on port 8100
- [ ] API responds at http://localhost:8100/
- [ ] API docs at http://localhost:8100/docs
- [ ] Can create activities
- [ ] Can add instructors
- [ ] Can submit assignments

### Instructor Dashboard
- [ ] Dashboard loads at http://localhost:8100/dashboard
- [ ] Shows Google Sign-In button
- [ ] Can sign in with instructor email
- [ ] Shows activities after login
- [ ] Shows student submissions
- [ ] Can download notebooks
- [ ] Logout works

### Student Portal
- [ ] Portal loads at http://localhost:3000
- [ ] Shows Google Sign-In button
- [ ] Can sign in with Google
- [ ] Shows courses from database
- [ ] Shows "no courses" when appropriate
- [ ] Can select course
- [ ] JupyterLab launch button works

## Access Points

| Service | URL | Description |
|---------|-----|-------------|
| API | http://localhost:8100 | Main API endpoint |
| API Docs | http://localhost:8100/docs | Interactive API documentation |
| Instructor Dashboard | http://localhost:8100/dashboard | Instructor interface |
| Student Portal | http://localhost:3000 | Student sign-in and course selection |
| Database | localhost:5433 | PostgreSQL (internal) |
| pgAdmin | http://localhost:5050 | Database management UI (optional) |

## Common Commands

```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# View logs
docker compose logs -f
docker compose logs -f app
docker compose logs -f portal

# Restart a service
docker compose restart app

# Rebuild after code changes
docker compose up -d --build

# Access database
docker compose exec db psql -U activity_user -d activity_db

# Check service status
docker compose ps

# View container stats
docker compose stats
```

## Database Operations

### View Data

```bash
# Connect to database
docker compose exec db psql -U activity_user -d activity_db

# List tables
\dt

# View activities
SELECT activity_id, activity_name, enabled FROM activities;

# View instructors
SELECT id, email, name FROM instructors;

# View submissions
SELECT user, name, email, activity_id, score FROM user_submissions;

# Exit
\q
```

### Backup and Restore

```bash
# Backup
docker compose exec db pg_dump -U activity_user activity_db > backup.sql

# Restore
docker compose exec -T db psql -U activity_user activity_db < backup.sql
```

## Troubleshooting

### Problem: Containers won't start

```bash
# Check logs
docker compose logs

# Check if ports are available
sudo lsof -i :8100
sudo lsof -i :3000
sudo lsof -i :5433

# Remove containers and try again
docker compose down -v
docker compose up -d --build
```

### Problem: "GOOGLE_CLIENT_ID not set"

```bash
# Check environment variable
docker compose exec app env | grep GOOGLE_CLIENT_ID

# If empty, check .env file
cat .env

# Make sure .env has:
# GOOGLE_CLIENT_ID=your-actual-client-id.apps.googleusercontent.com

# Restart
docker compose restart app
```

### Problem: Google Sign-In button doesn't appear

**Check 1:** Client ID in portal/index.html
```bash
grep GOOGLE_CLIENT_ID portal/index.html
# Should show your actual Client ID, not "YOUR_GOOGLE_CLIENT_ID"
```

**Check 2:** Authorized origins in Google Console
- Must include: `http://localhost:8100` and `http://localhost:3000`

**Check 3:** Browser console (F12)
- Look for JavaScript errors

### Problem: "Email not authorized as instructor"

```bash
# Check instructors in database
docker compose exec db psql -U activity_user -d activity_db -c "SELECT * FROM instructors;"

# Add your email
curl -X POST "http://localhost:8100/api/instructor" \
  -H "Content-Type: application/json" \
  -d '{"email":"YOUR_ACTUAL_EMAIL","name":"Your Name","activity_id":"python101"}'
```

### Problem: Portal shows "no courses"

```bash
# Check if student email is in database
docker compose exec db psql -U activity_user -d activity_db -c \
  "SELECT * FROM user_submissions WHERE email='your.email@gmail.com';"

# If not found, add a submission with your email
curl -X POST "http://localhost:8100/api/submit" \
  -F "user=testuser" \
  -F "name=Test" \
  -F "activity=python101" \
  -F "email=your.email@gmail.com" \
  -F "notebook=@test.ipynb"
```

### Problem: CORS errors in browser

The main.py already has CORS configured for localhost:3000.

If using different URLs, update main.py:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://your-domain.com"],
    ...
)
```

## Security Notes

### For Production

1. **Change database password**
   - Edit `.env`
   - Change `POSTGRES_PASSWORD` to something strong

2. **Use HTTPS**
   - Setup nginx reverse proxy
   - Get SSL certificate (Let's Encrypt)

3. **Restrict database access**
   - Don't expose port 5432 externally
   - Remove from `ports:` in docker-compose.yml

4. **Enable server-side token verification**
   - Install: `pip install google-auth`
   - Update `verify_google_token()` in main.py

5. **Regular backups**
   - Set up automated database backups
   - Store backups securely

## Next Steps

### After Basic Setup Works

1. **Add more activities**
   ```bash
   curl -X POST "http://localhost:8100/api/activity" \
     -F "activity_id=data_science" \
     -F "activity_name=Data Science Fundamentals" \
     -F "enabled=true" \
     -F "grading_notebook=@grading.ipynb"
   ```

2. **Add more instructors**
   ```bash
   curl -X POST "http://localhost:8100/api/instructor" \
     -H "Content-Type: application/json" \
     -d '{"email":"prof@university.edu","name":"Professor","activity_id":"data_science"}'
   ```

3. **Implement automated grading** (Step C)
   - Docker container per submission
   - Execute notebooks
   - Extract scores

4. **Add admin interface**
   - View all data
   - Edit any field
   - Manage users

5. **Deploy to production**
   - Get domain name
   - Setup SSL/HTTPS
   - Configure firewall
   - Enable backups

## Summary

You now have:
✅ PostgreSQL database with activity schema
✅ FastAPI application with Google OAuth
✅ Instructor dashboard (OAuth login)
✅ Student portal (OAuth login, database-driven courses)
✅ All running in Docker
✅ Complete API for managing activities and submissions

**Time to complete:** ~15 minutes  
**Complexity:** Low (with Docker experience)  
**Result:** Fully functional activity management system

---

**Need help?** Check logs with `docker compose logs -f`