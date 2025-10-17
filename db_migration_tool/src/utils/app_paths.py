"""애플리케이션 경로 관리 유틸리티

모든 파일 시스템 경로를 중앙집중화하여 관리합니다.
테스트 환경에서는 커스텀 루트 경로를 주입할 수 있습니다.
"""
import os
from pathlib import Path
from typing import Optional
from PySide6.QtCore import QStandardPaths


class AppPaths:
    """애플리케이션 경로 중앙 관리 클래스

    싱글톤 스타일의 경로 캐싱을 제공하며, 테스트 환경에서
    커스텀 루트 경로를 주입할 수 있습니다.

    Examples:
        >>> from src.utils.app_paths import AppPaths
        >>> logs_dir = AppPaths.get_logs_dir()
        >>> db_path = AppPaths.get_db_path()

        # 테스트 환경
        >>> AppPaths.set_custom_root(Path("/tmp/test"))
        >>> AppPaths.get_app_data_dir()  # /tmp/test
        >>> AppPaths.set_custom_root(None)  # 원복
    """

    # 클래스 변수: 경로 캐싱
    _app_data_dir: Optional[Path] = None
    _logs_dir: Optional[Path] = None
    _db_path: Optional[Path] = None
    _temp_dir: Optional[Path] = None

    # 설정: 커스텀 루트 디렉토리 (테스트용)
    _custom_root: Optional[Path] = None

    @classmethod
    def set_custom_root(cls, root: Optional[Path]):
        """커스텀 루트 디렉토리 설정 (테스트용)

        Args:
            root: 커스텀 루트 경로. None이면 기본 경로 사용

        Examples:
            >>> import tempfile
            >>> with tempfile.TemporaryDirectory() as tmpdir:
            ...     AppPaths.set_custom_root(Path(tmpdir))
            ...     # 테스트 수행
            ...     AppPaths.set_custom_root(None)  # 원복
        """
        cls._custom_root = root
        cls._reset_cache()

    @classmethod
    def _reset_cache(cls):
        """경로 캐시 초기화

        커스텀 루트 변경 시 자동으로 호출됩니다.
        """
        cls._app_data_dir = None
        cls._logs_dir = None
        cls._db_path = None
        cls._temp_dir = None

    @classmethod
    def get_app_data_dir(cls) -> Path:
        """애플리케이션 데이터 디렉토리

        플랫폼별 표준 경로를 반환하며, 디렉토리가 없으면 자동 생성합니다.

        Returns:
            애플리케이션 데이터 디렉토리 경로

        Examples:
            - macOS: ~/Library/Application Support/DBMigrationTool
            - Windows: C:\\Users\\username\\AppData\\Local\\DBMigrationTool
            - Linux: ~/.local/share/DBMigrationTool
        """
        if cls._app_data_dir is None:
            if cls._custom_root:
                cls._app_data_dir = cls._custom_root
            else:
                app_data = QStandardPaths.writableLocation(
                    QStandardPaths.AppDataLocation
                )
                if not app_data:
                    # QStandardPaths가 빈 문자열 반환 시 fallback
                    app_data = str(Path.home() / ".db_migration_tool")
                cls._app_data_dir = Path(app_data)

            # 디렉토리 생성
            cls._app_data_dir.mkdir(parents=True, exist_ok=True)

        return cls._app_data_dir

    @classmethod
    def get_logs_dir(cls) -> Path:
        """로그 디렉토리

        Returns:
            로그 파일 디렉토리 경로

        Examples:
            - macOS: ~/Library/Application Support/DBMigrationTool/logs
        """
        if cls._logs_dir is None:
            cls._logs_dir = cls.get_app_data_dir() / "logs"
            cls._logs_dir.mkdir(parents=True, exist_ok=True)

        return cls._logs_dir

    @classmethod
    def get_db_path(cls) -> Path:
        """로컬 데이터베이스 파일 경로

        Returns:
            SQLite 데이터베이스 파일 경로

        Examples:
            - macOS: ~/Library/Application Support/DBMigrationTool/db_migration.db
        """
        if cls._db_path is None:
            cls._db_path = cls.get_app_data_dir() / "db_migration.db"

        return cls._db_path

    @classmethod
    def get_temp_dir(cls) -> Path:
        """임시 파일 디렉토리

        Returns:
            임시 파일 디렉토리 경로

        Examples:
            - macOS: ~/Library/Application Support/DBMigrationTool/temp
        """
        if cls._temp_dir is None:
            cls._temp_dir = cls.get_app_data_dir() / "temp"
            cls._temp_dir.mkdir(parents=True, exist_ok=True)

        return cls._temp_dir

    @classmethod
    def get_log_file(cls, filename: str) -> Path:
        """로그 파일 경로

        Args:
            filename: 로그 파일 이름

        Returns:
            로그 파일 전체 경로

        Examples:
            >>> log_file = AppPaths.get_log_file("migration_20250118.log")
        """
        return cls.get_logs_dir() / filename

    @classmethod
    def get_config_path(cls) -> Path:
        """설정 파일 경로

        Returns:
            설정 파일 경로 (향후 확장용)
        """
        return cls.get_app_data_dir() / "config.json"

    @classmethod
    def ensure_all_dirs(cls):
        """모든 디렉토리 생성 확인

        앱 초기화 시 호출하여 필요한 모든 디렉토리를 미리 생성합니다.
        """
        cls.get_app_data_dir()
        cls.get_logs_dir()
        cls.get_temp_dir()

    @classmethod
    def clean_temp_dir(cls):
        """임시 디렉토리 정리

        임시 디렉토리의 모든 파일과 하위 디렉토리를 삭제합니다.
        실패해도 예외를 발생시키지 않습니다.
        """
        temp_dir = cls.get_temp_dir()
        for item in temp_dir.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    import shutil
                    shutil.rmtree(item)
            except Exception:
                pass  # 실패해도 무시


# 편의 함수
def get_app_data_dir() -> Path:
    """애플리케이션 데이터 디렉토리 (편의 함수)

    Returns:
        애플리케이션 데이터 디렉토리 경로
    """
    return AppPaths.get_app_data_dir()


def get_logs_dir() -> Path:
    """로그 디렉토리 (편의 함수)

    Returns:
        로그 디렉토리 경로
    """
    return AppPaths.get_logs_dir()


def get_db_path() -> Path:
    """DB 파일 경로 (편의 함수)

    Returns:
        SQLite DB 파일 경로
    """
    return AppPaths.get_db_path()
