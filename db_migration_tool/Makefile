# DB Migration Tool - Makefile
# Cross-platform build automation using uv

.DEFAULT_GOAL := help

# Platform detection
ifeq ($(OS),Windows_NT)
	PLATFORM := Windows
	VENV_BIN := .venv/Scripts
	PYTHON := $(VENV_BIN)/python.exe
	UV := uv
	RM := cmd /c rmdir /s /q
	RMFILE := cmd /c del /f /q
	MKDIR := cmd /c mkdir
	PATHSEP := \\
else
	PLATFORM := $(shell uname -s)
	VENV_BIN := .venv/bin
	PYTHON := $(VENV_BIN)/python
	UV := uv
	RM := rm -rf
	RMFILE := rm -f
	MKDIR := mkdir -p
	PATHSEP := /
endif

# Project variables
APP_NAME := DBMigrationTool
SRC_DIR := src
DIST_DIR := dist
BUILD_DIR := build
SPEC_FILE_WIN := db_migration_tool.spec
SPEC_FILE_MAC := build_mac.spec

# Tool shortcuts for uv run
PYTEST := uv run pytest
RUFF := uv run ruff
MYPY := uv run mypy

# Colors for output
CYAN := \033[0;36m
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

.PHONY: help setup install install-dev install-test build clean test run check-uv

help: ## Show this help message
	@echo "$(CYAN)DB Migration Tool - Make Commands$(NC)"
	@echo ""
	@echo "$(GREEN)Available targets:$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-15s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)Platform: $(PLATFORM)$(NC)"

check-uv: ## Check if uv is installed
	@command -v uv >/dev/null 2>&1 || { echo "$(RED)Error: uv is not installed. Install it from https://github.com/astral-sh/uv$(NC)"; exit 1; }
	@echo "$(GREEN)✓ uv is installed$(NC)"

setup: check-uv ## Setup development environment (create venv + install dependencies)
	@echo "$(CYAN)Setting up development environment...$(NC)"
	$(UV) venv
	@echo "$(GREEN)✓ Virtual environment created$(NC)"
	$(UV) pip install -e .
	@echo "$(GREEN)✓ Core dependencies installed$(NC)"
	@echo "$(YELLOW)Run 'make install-dev' to install development tools$(NC)"

install: check-uv ## Install core dependencies only
	@echo "$(CYAN)Installing core dependencies...$(NC)"
	$(UV) pip install -e .
	@echo "$(GREEN)✓ Dependencies installed$(NC)"

install-dev: check-uv ## Install development dependencies (PyInstaller, ruff, mypy)
	@echo "$(CYAN)Installing development dependencies...$(NC)"
	$(UV) pip install -e ".[dev]"
	@echo "$(GREEN)✓ Development dependencies installed$(NC)"

install-test: check-uv ## Install test dependencies
	@echo "$(CYAN)Installing test dependencies...$(NC)"
	$(UV) pip install -e ".[test]"
	@echo "$(GREEN)✓ Test dependencies installed$(NC)"

install-all: check-uv ## Install all dependencies (core + dev + test)
	@echo "$(CYAN)Installing all dependencies...$(NC)"
	$(UV) pip install -e ".[dev,test]"
	@echo "$(GREEN)✓ All dependencies installed$(NC)"

sync: check-uv ## Sync dependencies (based on uv.lock)
	@echo "$(CYAN)Syncing dependencies...$(NC)"
	uv sync
	@echo "$(GREEN)✓ Dependencies synced$(NC)"

build: install-dev ## Build executable with PyInstaller
ifeq ($(PLATFORM),Windows)
	@echo "$(CYAN)Building Windows executable...$(NC)"
	@if not exist $(SPEC_FILE_WIN) ( \
		echo "$(YELLOW)Generating PyInstaller spec file...$(NC)" && \
		$(PYTHON) -m PyInstaller --name=$(APP_NAME) \
			--windowed \
			--onefile \
			--icon=resources/icons/app.ico \
			--add-data "resources;resources" \
			$(SRC_DIR)/main.py \
	)
	@echo "$(CYAN)Running PyInstaller...$(NC)"
	$(PYTHON) -m PyInstaller $(SPEC_FILE_WIN) --clean
	@if exist $(DIST_DIR)\$(APP_NAME).exe ( \
		echo "$(GREEN)✓ Build successful: $(DIST_DIR)\$(APP_NAME).exe$(NC)" \
	) else ( \
		echo "$(RED)✗ Build failed$(NC)" && exit 1 \
	)
else
	@echo "$(CYAN)Building macOS application...$(NC)"
	@if [ ! -f $(SPEC_FILE_MAC) ]; then \
		echo "$(YELLOW)Generating PyInstaller spec file...$(NC)"; \
		$(PYTHON) -m PyInstaller --name="DB Migration Tool" \
			--windowed \
			--onefile \
			--icon=resources/icons/app.icns \
			--add-data "resources:resources" \
			$(SRC_DIR)/main.py; \
	fi
	@echo "$(CYAN)Running PyInstaller...$(NC)"
	$(PYTHON) -m PyInstaller $(SPEC_FILE_MAC) --clean
	@if [ -d "$(DIST_DIR)/DB Migration Tool.app" ]; then \
		echo "$(GREEN)✓ Build successful: $(DIST_DIR)/DB Migration Tool.app$(NC)"; \
	else \
		echo "$(RED)✗ Build failed$(NC)"; exit 1; \
	fi
endif

build-mac: clean ## macOS build using shell script
	@echo "$(CYAN)Building macOS application...$(NC)"
	chmod +x build_mac.sh
	./build_mac.sh
	@echo "$(GREEN)✓ Build complete: dist/DB Migration Tool.app$(NC)"

test: install-test ## Run tests with pytest
	@echo "$(CYAN)Running tests...$(NC)"
	$(PYTEST) tests/ -v --tb=short
	@echo "$(GREEN)✓ Tests completed$(NC)"

test-cov: install-test ## Run tests with coverage report
	@echo "$(CYAN)Running tests with coverage...$(NC)"
	$(PYTEST) tests/ -v --tb=short --cov=$(SRC_DIR) --cov-report=html --cov-report=term-missing
	@echo "$(GREEN)✓ Coverage report generated in htmlcov/$(NC)"

test-unit: ## Run unit tests only
	@echo "$(CYAN)Running unit tests...$(NC)"
	$(PYTEST) tests/ -v -m unit

test-integration: ## Run integration tests only
	@echo "$(CYAN)Running integration tests...$(NC)"
	$(PYTEST) tests/ -v -m integration

lint: ## Check code with ruff
	@echo "$(CYAN)Running ruff lint...$(NC)"
	$(RUFF) check $(SRC_DIR)/ tests/

lint-fix: ## Auto-fix lint issues
	@echo "$(CYAN)Running ruff auto-fix...$(NC)"
	$(RUFF) check --fix $(SRC_DIR)/ tests/

format: ## Format code with ruff
	@echo "$(CYAN)Formatting code...$(NC)"
	$(RUFF) format $(SRC_DIR)/ tests/

format-check: ## Check formatting (no changes)
	@echo "$(CYAN)Checking format...$(NC)"
	$(RUFF) format --check $(SRC_DIR)/ tests/

typecheck: ## Type check with mypy
	@echo "$(CYAN)Running mypy...$(NC)"
	$(MYPY) $(SRC_DIR)/

check: format-check lint typecheck ## Run all quality checks (format + lint + typecheck)
	@echo "$(GREEN)✓ All checks passed!$(NC)"

run: ## Run application in development mode
	@echo "$(CYAN)Running application...$(NC)"
	$(PYTHON) $(SRC_DIR)/main.py

dev: run ## Alias for run

clean: ## Clean build artifacts and cache
	@echo "$(CYAN)Cleaning build artifacts...$(NC)"
ifeq ($(PLATFORM),Windows)
	@if exist $(DIST_DIR) $(RM) $(DIST_DIR)
	@if exist $(BUILD_DIR) $(RM) $(BUILD_DIR)
	@if exist __pycache__ $(RM) __pycache__
	@if exist $(SRC_DIR)\__pycache__ $(RM) $(SRC_DIR)\__pycache__
	@if exist *.spec $(RMFILE) *.spec
	@for /d /r %%i in (__pycache__) do @if exist "%%i" $(RM) "%%i"
	@for /d /r %%i in (*.egg-info) do @if exist "%%i" $(RM) "%%i"
else
	$(RM) $(DIST_DIR) $(BUILD_DIR)
	$(RM) .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
endif
	@echo "$(GREEN)✓ Cleaned$(NC)"

clean-all: clean ## Clean everything including venv
	@echo "$(CYAN)Cleaning virtual environment...$(NC)"
ifeq ($(PLATFORM),Windows)
	@if exist .venv $(RM) .venv
else
	$(RM) .venv
endif
	@echo "$(GREEN)✓ All cleaned$(NC)"

ci: install-all check test ## CI pipeline simulation

deps: ## Show dependency tree
	@echo "$(CYAN)Dependency tree:$(NC)"
	uv tree

update: ## Update dependencies
	@echo "$(YELLOW)Updating dependencies...$(NC)"
	uv lock --upgrade
	@echo "$(GREEN)✓ uv.lock updated$(NC)"

version: ## Show project version
	@echo "$(CYAN)DB Migration Tool$(NC)"
	@$(PYTHON) -c "import toml; print('Version:', toml.load('pyproject.toml')['project']['version'])" 2>/dev/null || echo "Version: 1.0.0"

python-version: ## Show Python version
	@echo "$(CYAN)Python version:$(NC)"
	@$(PYTHON) --version
	@echo "$(CYAN)uv version:$(NC)"
	@uv --version

.PHONY: info
info: ## Show project information
	@echo "$(CYAN)Project Information$(NC)"
	@echo "  Name: DB Migration Tool"
	@echo "  Platform: $(PLATFORM)"
	@echo "  Python: $(PYTHON)"
	@echo "  Source: $(SRC_DIR)"
	@echo "  Dist: $(DIST_DIR)"
