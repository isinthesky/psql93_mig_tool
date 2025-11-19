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

install-dev: check-uv ## Install development dependencies (PyInstaller)
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

test: install-test ## Run tests with pytest
	@echo "$(CYAN)Running tests...$(NC)"
	$(PYTHON) -m pytest tests/ -v
	@echo "$(GREEN)✓ Tests completed$(NC)"

test-cov: install-test ## Run tests with coverage report
	@echo "$(CYAN)Running tests with coverage...$(NC)"
	$(PYTHON) -m pytest tests/ -v --cov=$(SRC_DIR) --cov-report=html --cov-report=term
	@echo "$(GREEN)✓ Coverage report generated in htmlcov/$(NC)"

run: ## Run application in development mode
	@echo "$(CYAN)Running application...$(NC)"
	$(PYTHON) $(SRC_DIR)/main.py

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
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	$(RMFILE) *.spec
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

.PHONY: info
info: ## Show project information
	@echo "$(CYAN)Project Information$(NC)"
	@echo "  Name: DB Migration Tool"
	@echo "  Platform: $(PLATFORM)"
	@echo "  Python: $(PYTHON)"
	@echo "  Source: $(SRC_DIR)"
	@echo "  Dist: $(DIST_DIR)"
