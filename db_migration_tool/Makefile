# DB Migration Tool - Makefile
# uv 기반 의존성 관리 및 태스크 자동화

.PHONY: help install install-dev sync clean test test-cov lint format check dev build build-mac clean-build

# 기본 변수
PYTHON := uv run python
PYTEST := uv run pytest
RUFF := uv run ruff
MYPY := uv run mypy
PYINSTALLER := uv run pyinstaller

# 색상 정의
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

help: ## 사용 가능한 명령어 표시
	@echo "$(GREEN)DB Migration Tool - 개발 명령어$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'

install: ## 프로덕션 의존성 설치
	@echo "$(GREEN)프로덕션 의존성 설치 중...$(NC)"
	uv sync --no-dev

install-dev: ## 개발 의존성 포함 전체 설치
	@echo "$(GREEN)개발 의존성 포함 전체 설치 중...$(NC)"
	uv sync --all-extras

sync: ## 의존성 동기화 (uv.lock 기반)
	@echo "$(GREEN)의존성 동기화 중...$(NC)"
	uv sync

clean: ## 빌드 아티팩트 및 캐시 삭제
	@echo "$(YELLOW)빌드 아티팩트 제거 중...$(NC)"
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	rm -rf .mypy_cache
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	@echo "$(GREEN)정리 완료$(NC)"

test: ## 테스트 실행
	@echo "$(GREEN)테스트 실행 중...$(NC)"
	$(PYTEST) tests/ -v --tb=short

test-cov: ## 커버리지 포함 테스트 실행
	@echo "$(GREEN)커버리지 포함 테스트 실행 중...$(NC)"
	$(PYTEST) tests/ -v --tb=short --cov=src --cov-report=term-missing --cov-report=html
	@echo "$(GREEN)커버리지 리포트: htmlcov/index.html$(NC)"

test-unit: ## 단위 테스트만 실행
	@echo "$(GREEN)단위 테스트 실행 중...$(NC)"
	$(PYTEST) tests/ -v -m unit

test-integration: ## 통합 테스트만 실행
	@echo "$(GREEN)통합 테스트 실행 중...$(NC)"
	$(PYTEST) tests/ -v -m integration

lint: ## 코드 린트 검사 (ruff)
	@echo "$(GREEN)Ruff 린트 검사 중...$(NC)"
	$(RUFF) check src/ tests/

lint-fix: ## 코드 린트 자동 수정
	@echo "$(GREEN)Ruff 자동 수정 중...$(NC)"
	$(RUFF) check --fix src/ tests/

format: ## 코드 포맷팅 (ruff)
	@echo "$(GREEN)코드 포맷팅 중...$(NC)"
	$(RUFF) format src/ tests/

format-check: ## 포맷팅 검사 (수정 없이)
	@echo "$(GREEN)포맷팅 검사 중...$(NC)"
	$(RUFF) format --check src/ tests/

typecheck: ## 타입 체크 (mypy)
	@echo "$(GREEN)타입 체크 중...$(NC)"
	$(MYPY) src/

check: format-check lint typecheck ## 전체 코드 품질 검사 (format + lint + typecheck)
	@echo "$(GREEN)모든 검사 통과!$(NC)"

dev: ## 개발 모드로 애플리케이션 실행
	@echo "$(GREEN)개발 모드 실행 중...$(NC)"
	$(PYTHON) src/main.py

build-mac: clean ## macOS용 애플리케이션 빌드
	@echo "$(GREEN)macOS 애플리케이션 빌드 중...$(NC)"
	chmod +x build_mac.sh
	./build_mac.sh
	@echo "$(GREEN)빌드 완료: dist/DB\ Migration\ Tool.app$(NC)"

build: clean ## PyInstaller로 빌드 (기본)
	@echo "$(GREEN)PyInstaller 빌드 중...$(NC)"
	$(PYINSTALLER) db_migration_tool.spec
	@echo "$(GREEN)빌드 완료: dist/$(NC)"

clean-build: clean build ## 클린 빌드 (전체 정리 후 빌드)

ci: install-dev check test ## CI 파이프라인 시뮬레이션

# 버전 정보 표시
version: ## 프로젝트 버전 표시
	@echo "$(GREEN)DB Migration Tool$(NC)"
	@$(PYTHON) -c "import toml; print('Version:', toml.load('pyproject.toml')['project']['version'])" 2>/dev/null || echo "Version: 1.0.0"

# 의존성 트리 표시
deps: ## 의존성 트리 표시
	@echo "$(GREEN)의존성 트리:$(NC)"
	uv tree

# 의존성 업데이트
update: ## 의존성 업데이트
	@echo "$(YELLOW)의존성 업데이트 중...$(NC)"
	uv lock --upgrade
	@echo "$(GREEN)uv.lock 업데이트 완료$(NC)"

# Python 버전 확인
python-version: ## Python 버전 확인
	@echo "$(GREEN)Python 버전:$(NC)"
	@$(PYTHON) --version
	@echo "$(GREEN)uv 버전:$(NC)"
	@uv --version
