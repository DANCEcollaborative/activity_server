#!/bin/bash

# Activity Server - Automated Setup Script
# Run with: bash setup.sh

set -e  # Exit on error

echo "========================================"
echo "  Activity Server - Docker Setup"
echo "========================================"
echo ""

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    echo -e "${RED}Please do not run this script as root${NC}"
    exit 1
fi

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check for Docker
echo "Checking for Docker..."
if ! command_exists docker; then
    echo -e "${YELLOW}Docker not found. Installing Docker...${NC}"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    echo -e "${GREEN}Docker installed successfully!${NC}"
    echo -e "${YELLOW}Note: You may need to log out and back in for group changes to take effect${NC}"
else
    echo -e "${GREEN}Docker is already installed${NC}"
fi

# Check for Docker Compose
echo "Checking for Docker Compose..."
if ! command_exists docker compose version; then
    echo -e "${YELLOW}Docker Compose not found. Installing...${NC}"
    sudo apt-get update
    sudo apt-get install -y docker-compose-plugin
    echo -e "${GREEN}Docker Compose installed successfully!${NC}"
else
    echo -e "${GREEN}Docker Compose is already installed${NC}"
fi

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo ""
    echo "Creating .env file..."
    
    if [ -f .env.example ]; then
        cp .env.example .env
    else
        cat > .env << EOF
# Database Configuration
POSTGRES_DB=activity_db
POSTGRES_USER=activity_user
POSTGRES_PASSWORD=activity_pass@rY_$(openssl rand -hex 8)

# Application Configuration
DATABASE_URL=postgresql://activity_user:activity_pass@db:5432/activity_db

# pgAdmin Configuration
PGADMIN_DEFAULT_EMAIL=admin@activity.local
PGADMIN_DEFAULT_PASSWORD=admin_$(openssl rand -hex 8)

# Server Configuration
APP_PORT=8100
APP_WORKERS=4
EOF
    fi
    
    echo -e "${GREEN}.env file created${NC}"
    echo -e "${YELLOW}IMPORTANT: Please review and update passwords in .env file${NC}"
else
    echo -e "${GREEN}.env file already exists${NC}"
fi

# Check if all required files exist
echo ""
echo "Checking for required files..."
REQUIRED_FILES=("Dockerfile" "docker-compose.yml" "models.py" "main.py" "requirements.txt")
MISSING_FILES=0

for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo -e "${RED}Missing required file: $file${NC}"
        MISSING_FILES=$((MISSING_FILES + 1))
    fi
done

if [ $MISSING_FILES -gt 0 ]; then
    echo -e "${RED}Please ensure all required files are present before continuing${NC}"
    exit 1
fi

echo -e "${GREEN}All required files present${NC}"

# Create logs directory
mkdir -p logs

# Build and start services
echo ""
echo "Building Docker images..."
docker compose build

echo ""
echo "Starting services..."
docker compose up -d

echo ""
echo "Waiting for services to be ready..."
sleep 10

# Check if services are running
echo ""
echo "Checking service status..."
docker compose ps

# Test API endpoint
echo ""
echo "Testing API connection..."
if curl -s http://localhost:8100/ > /dev/null; then
    echo -e "${GREEN}API is responding!${NC}"
else
    echo -e "${RED}API is not responding. Check logs with: docker compose logs app${NC}"
fi

# Display access information
echo ""
echo "========================================"
echo -e "${GREEN}Setup Complete!${NC}"
echo "========================================"
echo ""
echo "Access your activity server at:"
echo "  - API: http://localhost:8100"
echo "  - API Documentation: http://localhost:8100/docs"
echo "  - Dashboard: http://localhost:8100/dashboard"
echo ""
echo "Useful commands:"
echo "  - View logs: docker compose logs -f"
echo "  - Stop server: docker compose down"
echo "  - Restart: docker compose restart"
echo "  - Database shell: docker compose exec db psql -U activity_user -d activity_db"
echo ""
echo "Next steps:"
echo "  1. Review and update passwords in .env file"
echo "  2. Create your first activity with the API"
echo "  3. Add instructors to activities"
echo "  4. Start accepting submissions!"
echo ""
echo "For detailed documentation, see README.md"
echo ""

# Offer to show logs
read -p "Would you like to view the logs? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker compose logs -f
fi