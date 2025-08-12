.PHONY: help install install-dev format lint typecheck check test clean

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install the project dependencies
	uv sync

install-dev: ## Install project with development dependencies
	uv sync --extra dev

format: ## Format code with ruff
	uv run ruff format .

lint: ## Lint code with ruff
	uv run ruff check .

lint-fix: ## Lint and fix code with ruff
	uv run ruff check --fix .

typecheck: ## Type check with mypy
	uv run mypy .

check: lint typecheck ## Run all checks (lint + typecheck)

test: check ## Run all tests and checks
	@echo "✓ All checks passed"

clean: ## Clean up temporary files
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +

# Development workflow
dev-setup: install-dev ## Set up development environment
	@echo "Development environment ready!"
	@echo "Run 'make check' to validate code"
	@echo "Run 'make format' to format code"