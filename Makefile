.PHONY: build run down clean logs seed reset

build:
	@echo "Building..."
	@docker compose up --build

run:
	@echo "Running..."
	@docker compose up -d

down:
	@echo "Stopping..."
	@docker compose down

clean:
	@echo "Cleaning (volumes included)..."
	@docker compose down -v

seed:
	@echo "Seeding database..."
	@docker compose exec -T flow-api python -m app.seed

reset: clean run
	@echo "Waiting for database to be ready..."
	@sleep 5
	@$(MAKE) seed
	@echo "Reset complete. Admin user: admin / admin123"

logs:
	@echo "Showing logs..."
	@docker compose logs -f
