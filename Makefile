.DEFAULT_GOAL := help
PYTHON ?= python3

.PHONY: help install test lint typecheck format check clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install in editable mode with dev dependencies
	$(PYTHON) -m pip install -e ".[dev]"

test: ## Run tests with pytest
	pytest

lint: ## Run linter (ruff check + format check)
	ruff check .
	ruff format --check .

typecheck: ## Run type checker (mypy)
	mypy src/

format: ## Auto-format code with ruff
	ruff format .

check: lint typecheck test ## Run lint, typecheck, and test in sequence

clean: ## Remove build artifacts and caches
	rm -rf .mypy_cache .ruff_cache .pytest_cache dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
