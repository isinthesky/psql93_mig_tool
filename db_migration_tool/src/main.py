#!/usr/bin/env python3
"""
DB Migration Tool - Main Entry Point
PostgreSQL 파티션 테이블 마이그레이션 도구
"""

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

from src.database.local_db import LocalDatabase
from src.models.profile import ProfileManager
from src.models.saved_connection import SavedConnectionManager
from src.ui.dialogs.master_password_dialog import MasterPasswordDialog
from src.ui.main_window import MainWindow
from src.utils.master_password import MasterPasswordService


def get_resource_path(relative_path):
    """리소스 파일 경로를 가져옵니다 (PyInstaller 호환)"""
    try:
        # PyInstaller가 생성한 임시 폴더
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


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
    app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api="pyside6"))

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


def ensure_authenticated():
    """앱 사용 전 마스터 비밀번호 설정/로그인을 강제합니다."""
    auth_service = MasterPasswordService()

    if not auth_service.is_configured():
        setup_dialog = MasterPasswordDialog(mode="setup")
        if setup_dialog.exec() == 0:
            return None

        password = setup_dialog.get_password()

        try:
            auth_service.setup_master_password(password)
            auth_service.unlock(password)
        except Exception as exc:
            QMessageBox.critical(
                None,
                "설정 오류",
                f"마스터 비밀번호 설정 중 오류가 발생했습니다:\n{str(exc)}",
            )
            return None

        active_cipher = MasterPasswordService.get_active_cipher_suite()
        migrated_profiles = ProfileManager().reencrypt_all_profiles(active_cipher)
        migrated_connections = SavedConnectionManager().reencrypt_all_saved_connections(active_cipher)

        message = "마스터 비밀번호가 설정되었습니다."
        if migrated_profiles or migrated_connections:
            message += (
                f"\n연결 프로필 {migrated_profiles}개, 저장된 연결 {migrated_connections}개를 새 키로 보호했습니다."
            )

        legacy_key_file = MasterPasswordService.get_legacy_key_file_path()
        if legacy_key_file.exists():
            try:
                legacy_key_file.unlink()
            except OSError:
                pass

        QMessageBox.information(None, "설정 완료", message)
        return auth_service

    while True:
        unlock_dialog = MasterPasswordDialog(mode="unlock")
        if unlock_dialog.exec() == 0:
            return None

        if auth_service.unlock(unlock_dialog.get_password()):
            return auth_service

        QMessageBox.warning(None, "로그인 실패", "마스터 비밀번호가 올바르지 않습니다.")


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
            "DB 마이그레이션 도구가 이미 실행 중입니다.\n"
            "기존 창을 확인해 주세요.",
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

    auth_service = ensure_authenticated()
    if auth_service is None:
        return 0

    # 메인 윈도우 생성
    window = MainWindow()

    # 트레이 아이콘 설정
    from src.ui.tray_icon import TrayIconManager

    tray_manager = TrayIconManager(app, window)
    if tray_manager.setup():
        window.tray_icon = tray_manager

        # 시그널 연결
        tray_manager.show_window_requested.connect(lambda: window.show())
        tray_manager.show_history_requested.connect(window.show_history_dialog)
        tray_manager.quit_requested.connect(app.quit)

    # 윈도우 표시
    window.show()

    # 애플리케이션 실행
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
