# Grading Server - Dockerized Setup

A complete automated grading system for Jupyter notebooks with Docker deployment.

## 🚀 Quick Start

```bash
# Clone or create project directory
mkdir grading_server && cd grading_server

# Copy all project files (Dockerfile, docker-compose.yml, models.py, main.py, etc.)

# Create environment file
cp .env.example .env

# Build and start (using Make - optional)
make init

# OR manually with docker compose
docker compose up -d

# Check status
docker compose ps
```

Access the server at:
- **API**: http://localhost:8100
- **API Docs**: http://localhost:8100/docs
- **Dashboard**: http://localhost:8100/dashboard

## 📋 Prerequisites

- Docker 20.10+
- Docker Compose 2.0+
- Ubuntu 20.04/22.04 (or any Linux with Docker support)

## 🏗️ Architecture

```
┌─────────────────┐
│   Client        │
│  (Browser/API)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────┐
│  FastAPI App    │────▶│  PostgreSQL  │
│   (Port 8100)   │     │   (Port 5433)│
└─────────────────┘     └──────────────┘
         │
         ▼
┌─────────────────┐
│  Docker Engine  │  (for future grading containers)
└─────────────────┘
```

## 📁 Project Structure

```
grading_server/
├── Dockerfile              # App container definition
├── docker-compose.yml      # Multi-container orchestration
├── .dockerignore          # Files to exclude from build
├── .env.example           # Environment template
├── .env                   # Your environment config (create this)
├── requirements.txt       # Python dependencies
├── models.py              # Database models
├── main.py                # FastAPI application
├── Makefile              # Convenience commands (optional)
└── README.md             # This file
```

## 🔧 Configuration

### Environment Variables (.env)

```bash
# Database
POSTGRES_DB=grading_db
POSTGRES_USER=grading_user
POSTGRES_PASSWORD=change_this_password  # ⚠️ CHANGE IN PRODUCTION!

# Application
DATABASE_URL=postgresql://grading_user:grading_pass@db:5432/grading_db
APP_PORT=8100
APP_WORKERS=4
```

## 🎯 Usage

### Using Make Commands (Recommended)

```bash
# View all available commands
make help

# Start services
make up

# View logs
make logs

# Stop services
make down

# Backup database
make backup

# Access database shell
make shell-db
```

### Using Docker Compose Directly

```bash
# Start services
docker compose up -d

# View logs
docker compose logs -f

# Stop services
docker compose down

# Restart services
docker compose restart

# Execute command in container
docker compose exec app python --version
```

## 📊 Database Management

### Backup

```bash
# Using Make
make backup

# OR manually
docker compose exec db pg_dump -U grading_user grading_db > backup.sql
```

### Restore

```bash
# Place backup.sql in project directory, then:
make restore

# OR manually
docker compose exec -T db psql -U grading_user grading_db < backup.sql
```

### Access Database CLI

```bash
make shell-db

# OR
docker compose exec db psql -U grading_user -d grading_db
```

## 🔌 API Endpoints

### Create Activity
```bash
curl -X POST "http://localhost:8100/api/activity" \
  -F "activity_id=homework1" \
  -F "grading_notebook=@grading.ipynb"
```

### Add Instructor
```bash
curl -X POST "http://localhost:8100/api/instructor" \
  -H "Content-Type: application/json" \
  -d '{
    "instructor": "prof_smith",
    "password": "secure_pass",
    "activity_id": "homework1"
  }'
```

### Submit Assignment
```bash
curl -X POST "http://localhost:8100/api/submit" \
  -F "user=student123" \
  -F "name=John Doe" \
  -F "activity=homework1" \
  -F "notebook=@submission.ipynb"
```

### Update Score
```bash
curl -X PUT "http://localhost:8100/api/score" \
  -H "Content-Type: application/json" \
  -d '{
    "activity_id": "homework1",
    "user": "student123",
    "score": 95.5
  }'
```

### View Dashboard
```
http://localhost:8100/dashboard
```
Login with instructor credentials (HTTP Basic Auth).

## 🛡️ Security Best Practices

### For Production Deployment

1. **Change Default Passwords**
   ```bash
   # Edit .env file
   nano .env
   # Change POSTGRES_PASSWORD
   ```

2. **Use HTTPS** - Setup reverse proxy with SSL
   ```bash
   # Example with nginx
   sudo apt install nginx certbot python3-certbot-nginx
   ```

3. **Firewall Configuration**
   ```bash
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo ufw deny 8100/tcp  # Don't expose app port directly
   sudo ufw deny 5432/tcp  # Don't expose database port
   ```

4. **Use Docker Secrets** for sensitive data
   ```yaml
   # In docker-compose.yml
   secrets:
     db_password:
       file: ./secrets/db_password.txt
   ```

5. **Regular Updates**
   ```bash
   docker compose pull
   docker compose up -d --build
   ```

## 🐛 Troubleshooting

### Services won't start
```bash
# Check logs
docker compose logs

# Check if ports are available
sudo lsof -i :8100
sudo lsof -i :5433
```

### Database connection errors
```bash
# Verify database is running
docker compose ps db

# Check database logs
docker compose logs db

# Test connection
docker compose exec db pg_isready -U grading_user
```

### Container crashes on startup
```bash
# View full logs
docker compose logs app --tail 100

# Check resource usage
docker compose stats

# Rebuild from scratch
docker compose down -v
docker compose up -d --build
```

### Permission errors with Docker socket
```bash
# Add user to docker group
sudo usermod -aG docker $USER

# Log out and back in, then:
docker compose restart
```

## 📈 Monitoring

### View Container Stats
```bash
make stats
# OR
docker compose stats
```

### Check Health
```bash
# App health
curl http://localhost:8100/

# Database health
docker compose exec db pg_isready -U grading_user
```

## 🔄 Updates and Maintenance

### Update Application Code
```bash
# After modifying main.py or models.py
docker compose up -d --build app
```

### Update Dependencies
```bash
# Edit requirements.txt, then:
docker compose build --no-cache app
docker compose up -d app
```

### Clean Up
```bash
# Remove unused containers and images
make clean

# Complete cleanup (removes volumes - DESTROYS DATA!)
make down-volumes
```

## 🎓 Next Steps

- [ ] Implement Step C (Docker-based grading containers)
- [ ] Add rate limiting
- [ ] Setup monitoring (Prometheus/Grafana)
- [ ] Configure automated backups
- [ ] Setup CI/CD pipeline
- [ ] Add user authentication beyond basic auth
- [ ] Implement WebSocket for real-time updates

## 📝 Notes

- The Docker socket is mounted for future grading container functionality (Step C)
- PostgreSQL data persists in a Docker volume named `postgres_data`
- Logs can be found with `docker compose logs`
- pgAdmin is available optionally with `--profile admin`

## 📞 Support

For issues or questions:
1. Check logs: `docker compose logs -f`
2. Review API docs: http://localhost:8100/docs
3. Verify environment variables in `.env`

## 📜 License

[Your License Here]