#!/usr/bin/env python3
"""
DB Migration Tool - Main Entry Point
PostgreSQL 파티션 테이블 마이그레이션 도구
"""

import logging
import os
import sys

# UTF-8 locale 설정 (Qt 경고 방지)
if sys.platform != "win32":  # Windows가 아닌 경우에만
    os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["LANG"] = "en_US.UTF-8"

import qdarkstyle
from PySide6.QtCore import QSharedMemory, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from src.core.worker_registry import FlushStep, ShutdownCoordinator
from src.database.local_db import LocalDatabase
from src.ui.main_window import MainWindow
from src.ui.theme import build_stylesheet


def get_resource_path(relative_path):
    """리소스 파일 경로를 가져옵니다 (PyInstaller 호환)"""
    # PyInstaller 번들로 실행될 때만 sys._MEIPASS(임시 압축 해제 폴더)가 생긴다.
    # 타입 스텁에는 없는 속성이라 getattr로 읽는다.
    base_path = getattr(sys, "_MEIPASS", None) or os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# 전역 스타일은 src/ui/theme.py 의 디자인 토큰에서 생성한다.
# (색/서체/간격을 한 곳에서만 정의하기 위해 이 파일의 APP_STYLE을 옮김)


def initialize_application():
    """애플리케이션 초기화"""
    # High DPI 설정 (PySide6에서는 기본으로 활성화됨)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # 애플리케이션 생성
    app = QApplication(sys.argv)
    app.setApplicationName("DB Migration Tool")
    app.setOrganizationName("DBMigration")
    app.setApplicationDisplayName("DB 마이그레이션 도구")

    # 다크 테마 적용
    app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api="pyside6") + build_stylesheet())

    # 애플리케이션 아이콘 설정
    icon_path = get_resource_path("resources/icons/app.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    return app


def initialize_database():
    """로컬 데이터베이스 초기화"""
    db = LocalDatabase()
    db.initialize()
    return db


# ── 앱 종료 (감사 M-13) ─────────────────────────────────────────────
# 순서: 워커 전체 stop → 실행 중 쿼리 cancel(H-02) → 제한 시간 대기 → flush → 이벤트 루프 종료.
# 워커·조정자 규약은 src/core/worker_registry.py.


def flush_logger() -> None:
    """DB 로그 큐를 끝까지 쓰고(writer 스레드 종료) 파일 로그 핸들러를 비운다."""
    from src.utils.enhanced_logger import enhanced_logger

    enhanced_logger.close()
    for handler in logging.getLogger("DBMigration").handlers:
        handler.flush()


def close_local_db() -> None:
    """이미 열린 로컬 이력 DB 엔진만 정리한다. 없으면 새로 열지 않는다."""
    from src.database import local_db

    db = local_db._db_instance
    if db is not None:
        db.close()


def default_flush_steps() -> list[FlushStep]:
    # 로거가 먼저다: DB 로그 writer가 로컬 DB에 마지막 batch를 쓴 뒤 엔진을 닫는다.
    # 아카이브 manifest는 워커의 finally가 save()로 flush하며, 조정자의 대기가 그것을 기다린다.
    return [("logger", flush_logger), ("local_db", close_local_db)]


def build_shutdown_coordinator(app, window, tray_manager) -> ShutdownCoordinator:
    """트레이 종료·창 닫기를 종료 조정자에 연결한다. `app.quit()`을 직접 부르는 경로를 두지 않는다."""
    from src.core import worker_registry as registry_module

    coordinator = ShutdownCoordinator(
        registry_module.worker_registry,
        # exit()는 창을 닫지 않고 (중첩 포함) 모든 이벤트 루프를 끝낸다. quit()은 창마다 closeEvent를
        # 돌려 '실행 중에는 닫을 수 없습니다' 같은 확인 창으로 종료를 붙잡을 수 있다.
        quit_app=lambda forced: app.exit(0),
        flush_steps=default_flush_steps(),
    )
    window.shutdown_coordinator = coordinator
    if tray_manager is not None:
        tray_manager.quit_requested.connect(coordinator.request_shutdown)
        coordinator.shutdown_started.connect(tray_manager.show_shutting_down)
        coordinator.shutdown_finished.connect(lambda _forced: tray_manager.cleanup())
    return coordinator


def finalize_exit(coordinator, exit_code: int) -> int:
    """이벤트 루프가 끝난 뒤 호출한다.

    트레이를 거치지 않고 루프가 끝났으면(OS 세션 종료 등) 여기서 막고 기다리며 정리한다.
    강제 종료(제한 시간 초과)면 아직 도는 QThread가 파이썬 종료 절차에서 파괴돼 abort하지 않도록
    로그만 닫고 곧바로 프로세스를 끝낸다. DB 서버는 끊긴 연결의 미커밋 배치를 롤백한다.
    """
    if not coordinator.is_finished:
        coordinator.shutdown_blocking()
    if coordinator.forced:
        logging.shutdown()
        os._exit(exit_code)
    return exit_code


def main():
    """메인 함수"""
    # 애플리케이션 초기화
    app = initialize_application()

    # --- 싱글 인스턴스 가드 ---
    shared_mem = QSharedMemory("DBMigrationTool_SingleInstance")
    if not shared_mem.create(1):
        QMessageBox.warning(
            None,
            "중복 실행",
            "DB 마이그레이션 도구가 이미 실행 중입니다.\n기존 창을 확인해 주세요.",
        )
        return 0
    # shared_mem은 프로세스 종료 시 OS가 자동 해제 (Windows)
    # --------------------------

    # 트레이 아이콘 사용 시 윈도우 닫힘으로 종료 방지
    app.setQuitOnLastWindowClosed(False)

    # 로컬 데이터베이스 초기화
    try:
        initialize_database()
    except Exception as e:
        QMessageBox.critical(
            None, "초기화 오류", f"데이터베이스 초기화 중 오류가 발생했습니다:\n{str(e)}"
        )
        return 1

    # 메인 윈도우 생성
    window = MainWindow()

    # 라이선스 확인 — DB 초기화 뒤, 창을 띄우기 전에 딱 한 번만 한다.
    # 실행 중에는 다시 확인하지 않는다. 마이그레이션이 도는 도중 만료됐다고
    # 작업을 끊으면 소스와 대상이 어긋난 채로 남는다.
    from src.licensing import check_failed_state, check_license

    try:
        license_state = check_license()
    except Exception as e:
        # check_license()는 예외를 던지지 않지만, 만약을 위해 여기서도 제한 모드로 닫는다.
        # 제한 모드도 중단된 마이그레이션의 재개는 허용하므로 도구가 잠기지 않는다.
        # (None을 넘기면 메인 창이 '제한 아님'으로 읽는다 — 감사 H-07 fail-open)
        print(f"Warning: 라이선스 확인 실패 ({e})")
        license_state = check_failed_state(e)

    window.set_license_state(license_state)

    if license_state is not None and license_state.is_restricted:
        window.show_license_dialog()

    # 트레이 아이콘 설정
    from src.ui.tray_icon import TrayIconManager

    tray = TrayIconManager(app, window)
    tray_manager: TrayIconManager | None = None
    if tray.setup():
        window.tray_icon = tray
        tray_manager = tray

        # 시그널 연결 (종료는 build_shutdown_coordinator가 조정자에 연결한다 — app.quit 직결 금지)
        tray.show_window_requested.connect(lambda: window.show())
        tray.show_history_requested.connect(window.show_history_dialog)

    coordinator = build_shutdown_coordinator(app, window, tray_manager)

    # 윈도우 표시
    window.show()

    # 애플리케이션 실행
    return finalize_exit(coordinator, app.exec())


if __name__ == "__main__":
    sys.exit(main())
