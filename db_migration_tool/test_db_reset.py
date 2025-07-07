#!/usr/bin/env python3
"""
데이터베이스 리셋 스크립트
SQLite 데이터베이스를 삭제하고 새로 생성합니다.
"""
import os
import shutil
from pathlib import Path
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

def reset_database():
    """데이터베이스 파일 삭제 및 재생성"""
    # Qt 애플리케이션 임시 생성 (경로 확인용)
    app = QApplication([])
    
    # 데이터베이스 경로 가져오기
    app_data_dir = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    db_path = os.path.join(app_data_dir, "db_migration.db")
    
    print(f"데이터베이스 경로: {db_path}")
    
    # 백업 생성
    if os.path.exists(db_path):
        backup_path = db_path + ".backup"
        print(f"기존 DB 백업 중: {backup_path}")
        shutil.copy2(db_path, backup_path)
        
        # 기존 DB 삭제
        print("기존 DB 삭제 중...")
        os.remove(db_path)
        print("삭제 완료!")
    else:
        print("기존 DB 파일이 없습니다.")
    
    print("\n애플리케이션을 다시 실행하면 새 데이터베이스가 생성됩니다.")

if __name__ == "__main__":
    reset_database()