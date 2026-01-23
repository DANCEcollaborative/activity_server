.PHONY: help build up down restart logs logs-app logs-db shell shell-db backup restore clean

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

build: ## Build Docker images
	docker compose build

up: ## Start all services
	docker compose up -d

up-admin: ## Start all services including pgAdmin
	docker compose --profile admin up -d

down: ## Stop all services
	docker compose down

down-volumes: ## Stop all services and remove volumes (CAUTION: destroys data!)
	docker compose down -v

restart: ## Restart all services
	docker compose restart

logs: ## View logs from all services
	docker compose logs -f

logs-app: ## View logs from app service
	docker compose logs -f app

logs-db: ## View logs from database service
	docker compose logs -f db

shell: ## Open shell in app container
	docker compose exec app bash

shell-db: ## Open PostgreSQL shell
	docker compose exec db psql -U activity_user -d activity_db

status: ## Show status of all services
	docker compose ps

stats: ## Show resource usage of containers
	docker compose stats

backup: ## Backup database to backup.sql
	docker compose exec db pg_dump -U activity_user activity_db > backup_$$(date +%Y%m%d_%H%M%S).sql
	@echo "Backup created: backup_$$(date +%Y%m%d_%H%M%S).sql"

restore: ## Restore database from backup.sql (requires backup.sql file)
	@if [ ! -f backup.sql ]; then \
		echo "Error: backup.sql not found"; \
		exit 1; \
	fi
	docker compose exec -T db psql -U activity_user activity_db < backup.sql
	@echo "Database restored from backup.sql"

clean: ## Remove stopped containers and unused images
	docker compose down
	docker system prune -f

rebuild: ## Rebuild and restart services
	docker compose down
	docker compose up -d --build

init: ## Initialize project (first time setup)
	@echo "Creating .env file..."
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo ".env file created. Please edit it with your configuration."; \
	else \
		echo ".env file already exists."; \
	fi
	@echo "Building images..."
	docker compose build
	@echo "Starting services..."
	docker compose up -d
	@echo "Waiting for services to be ready..."
	sleep 10
	@echo "Setup complete! Access the API at http://localhost:8100"
	@echo "API Documentation: http://localhost:8100/docs"