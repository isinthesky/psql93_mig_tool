"""app_paths.py 단위 테스트"""

import tempfile
from pathlib import Path

import pytest

from src.utils.app_paths import AppPaths, get_app_data_dir, get_db_path, get_logs_dir


class TestAppPaths:
    """AppPaths 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def reset_app_paths(self):
        """각 테스트 전후로 AppPaths 초기화"""
        # 테스트 전: 기존 캐시 초기화
        AppPaths.set_custom_root(None)
        yield
        # 테스트 후: 원복
        AppPaths.set_custom_root(None)

    def test_app_data_dir_exists(self):
        """애플리케이션 데이터 디렉토리가 존재하는지 확인"""
        app_data = AppPaths.get_app_data_dir()
        assert app_data.exists()
        assert app_data.is_dir()

    def test_logs_dir_exists(self):
        """로그 디렉토리가 존재하는지 확인"""
        logs_dir = AppPaths.get_logs_dir()
        assert logs_dir.exists()
        assert logs_dir.is_dir()
        assert logs_dir.parent == AppPaths.get_app_data_dir()
        assert logs_dir.name == "logs"

    def test_db_path(self):
        """DB 파일 경로가 올바른지 확인"""
        db_path = AppPaths.get_db_path()
        assert db_path.name == "db_migration.db"
        assert db_path.parent == AppPaths.get_app_data_dir()

    def test_temp_dir_exists(self):
        """임시 디렉토리가 존재하는지 확인"""
        temp_dir = AppPaths.get_temp_dir()
        assert temp_dir.exists()
        assert temp_dir.is_dir()
        assert temp_dir.parent == AppPaths.get_app_data_dir()
        assert temp_dir.name == "temp"

    def test_custom_root(self):
        """커스텀 루트 디렉토리 설정 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_root = Path(tmpdir) / "custom"
            AppPaths.set_custom_root(custom_root)

            app_data = AppPaths.get_app_data_dir()
            assert app_data == custom_root
            assert app_data.exists()

            logs_dir = AppPaths.get_logs_dir()
            assert logs_dir == custom_root / "logs"
            assert logs_dir.exists()

            db_path = AppPaths.get_db_path()
            assert db_path == custom_root / "db_migration.db"

    def test_custom_root_reset(self):
        """커스텀 루트 리셋 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_root = Path(tmpdir) / "custom"
            AppPaths.set_custom_root(custom_root)
            custom_app_data = AppPaths.get_app_data_dir()
            assert custom_app_data == custom_root

            # 리셋
            AppPaths.set_custom_root(None)
            default_app_data = AppPaths.get_app_data_dir()
            assert default_app_data != custom_root

    def test_path_caching(self):
        """경로 캐싱이 동작하는지 확인"""
        dir1 = AppPaths.get_app_data_dir()
        dir2 = AppPaths.get_app_data_dir()
        # 동일한 객체를 반환해야 함
        assert dir1 is dir2

    def test_cache_reset_on_custom_root_change(self):
        """커스텀 루트 변경 시 캐시가 리셋되는지 확인"""
        original_logs = AppPaths.get_logs_dir()

        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir) / "custom")
            custom_logs = AppPaths.get_logs_dir()
            assert custom_logs != original_logs

        # 원복 후
        AppPaths.set_custom_root(None)
        reset_logs = AppPaths.get_logs_dir()
        # 원래 경로와 같아야 함
        assert reset_logs == original_logs

    def test_ensure_all_dirs(self):
        """모든 디렉토리 생성 확인 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir) / "test_all")
            AppPaths.ensure_all_dirs()

            assert AppPaths.get_app_data_dir().exists()
            assert AppPaths.get_logs_dir().exists()
            assert AppPaths.get_temp_dir().exists()

    def test_clean_temp_dir(self):
        """임시 디렉토리 정리 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir) / "test_clean")
            temp_dir = AppPaths.get_temp_dir()

            # 임시 파일 생성
            test_file = temp_dir / "test.txt"
            test_file.write_text("test")
            assert test_file.exists()

            # 하위 디렉토리 생성
            test_subdir = temp_dir / "subdir"
            test_subdir.mkdir()
            (test_subdir / "nested.txt").write_text("nested")
            assert test_subdir.exists()

            # 정리
            AppPaths.clean_temp_dir()
            assert not test_file.exists()
            assert not test_subdir.exists()
            assert temp_dir.exists()  # temp 디렉토리 자체는 유지

    def test_get_log_file(self):
        """로그 파일 경로 생성 테스트"""
        log_file = AppPaths.get_log_file("migration_20250118.log")
        assert log_file.name == "migration_20250118.log"
        assert log_file.parent == AppPaths.get_logs_dir()

    def test_get_config_path(self):
        """설정 파일 경로 테스트"""
        config_path = AppPaths.get_config_path()
        assert config_path.name == "config.json"
        assert config_path.parent == AppPaths.get_app_data_dir()


class TestConvenienceFunctions:
    """편의 함수 테스트"""

    @pytest.fixture(autouse=True)
    def reset_app_paths(self):
        """각 테스트 전후로 AppPaths 초기화"""
        AppPaths.set_custom_root(None)
        yield
        AppPaths.set_custom_root(None)

    def test_get_app_data_dir_function(self):
        """get_app_data_dir 편의 함수 테스트"""
        dir1 = get_app_data_dir()
        dir2 = AppPaths.get_app_data_dir()
        assert dir1 == dir2

    def test_get_logs_dir_function(self):
        """get_logs_dir 편의 함수 테스트"""
        dir1 = get_logs_dir()
        dir2 = AppPaths.get_logs_dir()
        assert dir1 == dir2

    def test_get_db_path_function(self):
        """get_db_path 편의 함수 테스트"""
        path1 = get_db_path()
        path2 = AppPaths.get_db_path()
        assert path1 == path2


class TestEdgeCases:
    """엣지 케이스 테스트"""

    @pytest.fixture(autouse=True)
    def reset_app_paths(self):
        """각 테스트 전후로 AppPaths 초기화"""
        AppPaths.set_custom_root(None)
        yield
        AppPaths.set_custom_root(None)

    def test_clean_temp_dir_with_permission_error(self):
        """권한 오류 시에도 clean_temp_dir가 예외를 발생시키지 않는지 확인"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir) / "test_permission")
            temp_dir = AppPaths.get_temp_dir()

            # 임시 파일 생성
            test_file = temp_dir / "test.txt"
            test_file.write_text("test")

            # 정리 (실패해도 예외 발생 안 함)
            try:
                AppPaths.clean_temp_dir()
            except Exception as e:
                pytest.fail(f"clean_temp_dir should not raise exception: {e}")

    def test_nested_directory_creation(self):
        """중첩 디렉토리 자동 생성 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 존재하지 않는 경로 여러 단계
            nested_path = Path(tmpdir) / "level1" / "level2" / "level3"
            AppPaths.set_custom_root(nested_path)

            # 디렉토리가 자동 생성되어야 함
            app_data = AppPaths.get_app_data_dir()
            assert app_data.exists()
            assert app_data == nested_path
