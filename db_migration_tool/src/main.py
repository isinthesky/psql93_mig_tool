#!/usr/bin/env python3
"""
DB Migration Tool - Main Entry Point
PostgreSQL 파티션 테이블 마이그레이션 도구
"""
import sys
import os
from pathlib import Path

# UTF-8 locale 설정 (Qt 경고 방지)
if sys.platform != 'win32':  # Windows가 아닌 경우에만
    os.environ['LC_ALL'] = 'en_US.UTF-8'
    os.environ['LANG'] = 'en_US.UTF-8'

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTranslator, QLocale
from PySide6.QtGui import QIcon
import qdarkstyle

from src.ui.main_window import MainWindow
from src.database.local_db import LocalDatabase


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
    app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api='pyside6'))
    
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


def main():
    """메인 함수"""
    # 애플리케이션 초기화
    app = initialize_application()
    
    # 로컬 데이터베이스 초기화
    try:
        db = initialize_database()
    except Exception as e:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None,
            "초기화 오류",
            f"데이터베이스 초기화 중 오류가 발생했습니다:\n{str(e)}"
        )
        return 1
    
    # 메인 윈도우 생성 및 표시
    window = MainWindow()
    window.show()
    
    # 애플리케이션 실행
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())