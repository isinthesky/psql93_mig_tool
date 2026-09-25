"""공용 연결 파라미터 빌더 테스트 (감사 H-05, H-02/H-04 연결 단계)

- H-05: SSL 프로필은 서버 인증서와 hostname을 검증해야 한다(`verify-full` 기본).
  검증 없는 `require`는 명시적 위험 승인이 있을 때만, 경고·감사 로그와 함께 허용한다.
- H-02: 모든 연결에 connect_timeout이 있어야 연결 단계가 무한히 붙잡히지 않는다.
- H-04: 연결 직후 search_path가 `public`으로 제한되어야 선행 스키마의 동명 객체를 잡지 않는다.
- 모든 연결 지점이 이 빌더를 거쳐야 한다(한 곳이라도 빠지면 그 경로만 `require`로 남는다).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.database import connection_params as cp
from src.database.connection_params import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    SEARCH_PATH_OPTIONS,
    ConnectionConfigError,
    build_libpq_params,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

MODERN_LIBPQ = 170000  # sslrootcert=system 지원(16+)
OLD_LIBPQ = 150000


def _base(**overrides):
    config = {
        "kind": "postgres",
        "host": "db.example.com",
        "port": 5445,
        "database": "bms30",
        "username": "migtool",
        "password": "pw",
        "ssl": False,
    }
    config.update(overrides)
    return config


@pytest.fixture
def ca_file(tmp_path):
    path = tmp_path / "root.crt"
    path.write_text("-----BEGIN CERTIFICATE-----\n")
    return path


# ── 기본(SSL 미사용) ─────────────────────────────────────────


class TestPlainConnection:
    def test_ssl_off_does_not_set_sslmode(self):
        """SSL을 쓰지 않는 기존 프로필은 동작이 바뀌면 안 된다(libpq 기본값 유지)."""
        params = build_libpq_params(_base(), libpq_version=MODERN_LIBPQ)

        assert "sslmode" not in params
        assert "sslrootcert" not in params
        assert params["host"] == "db.example.com"
        assert params["port"] == 5445
        assert params["dbname"] == "bms30"
        assert params["user"] == "migtool"
        assert params["password"] == "pw"

    def test_missing_keys_fall_back_to_defaults(self):
        """키가 빠져 None이 넘어가면 libpq 기본값으로 엉뚱한 DB에 붙을 수 있다."""
        params = build_libpq_params({}, libpq_version=MODERN_LIBPQ)

        assert params["host"] == "localhost"
        assert params["port"] == 5432
        assert params["dbname"] == ""
        assert params["user"] == ""
        assert params["password"] == ""

    def test_ssl_fields_ignored_when_ssl_off(self, ca_file):
        """SSL 체크가 꺼져 있으면 남아 있는 세부 설정이 연결을 바꾸지 않는다."""
        params = build_libpq_params(
            _base(ssl=False, sslmode="require", sslrootcert=str(ca_file)),
            libpq_version=MODERN_LIBPQ,
        )
        assert "sslmode" not in params
        assert "sslrootcert" not in params


# ── connect_timeout (H-02 연결 단계) ─────────────────────────


class TestConnectTimeout:
    def test_default_timeout_is_ten_seconds(self):
        params = build_libpq_params(_base(), libpq_version=MODERN_LIBPQ)
        assert DEFAULT_CONNECT_TIMEOUT_SECONDS == 10
        assert params["connect_timeout"] == 10

    def test_call_site_default_is_used(self):
        params = build_libpq_params(_base(), connect_timeout=5, libpq_version=MODERN_LIBPQ)
        assert params["connect_timeout"] == 5

    def test_profile_setting_overrides_call_site_default(self):
        params = build_libpq_params(
            _base(connect_timeout=30), connect_timeout=5, libpq_version=MODERN_LIBPQ
        )
        assert params["connect_timeout"] == 30

    @pytest.mark.parametrize("bad", [0, -1, "abc", 1.5, True])
    def test_invalid_timeout_is_rejected(self, bad):
        """0은 libpq에서 '무한 대기'다. 잘못된 값이 조용히 무한 대기가 되면 안 된다."""
        with pytest.raises(ConnectionConfigError):
            build_libpq_params(_base(connect_timeout=bad), libpq_version=MODERN_LIBPQ)


# ── search_path (H-04 연결 단계) ─────────────────────────────


class TestSearchPath:
    def test_search_path_restricted_to_public(self):
        params = build_libpq_params(_base(), libpq_version=MODERN_LIBPQ)
        assert params["options"] == "-c search_path=public"
        assert SEARCH_PATH_OPTIONS == "-c search_path=public"

    def test_search_path_applied_with_ssl_too(self, ca_file):
        params = build_libpq_params(
            _base(ssl=True, sslrootcert=str(ca_file)), libpq_version=MODERN_LIBPQ
        )
        assert params["options"] == "-c search_path=public"


# ── TLS 서버 신원 검증 (H-05) ────────────────────────────────


class TestTlsVerification:
    def test_legacy_ssl_profile_defaults_to_verify_full_with_system_ca(self):
        """기존 'SSL 사용' 체크 프로필은 require가 아니라 verify-full로 연결해야 한다."""
        params = build_libpq_params(_base(ssl=True), libpq_version=MODERN_LIBPQ)

        assert params["sslmode"] == "verify-full"
        assert params["sslrootcert"] == "system"

    def test_explicit_ca_file_is_used(self, ca_file):
        params = build_libpq_params(
            _base(ssl=True, sslrootcert=str(ca_file)), libpq_version=OLD_LIBPQ
        )
        assert params["sslmode"] == "verify-full"
        assert params["sslrootcert"] == str(ca_file)

    def test_blank_ca_path_means_not_set(self):
        params = build_libpq_params(_base(ssl=True, sslrootcert="   "), libpq_version=MODERN_LIBPQ)
        assert params["sslrootcert"] == "system"

    def test_missing_ca_file_is_clear_error(self, tmp_path):
        with pytest.raises(ConnectionConfigError, match="CA"):
            build_libpq_params(
                _base(ssl=True, sslrootcert=str(tmp_path / "nope.crt")),
                libpq_version=MODERN_LIBPQ,
            )

    def test_old_libpq_without_ca_is_clear_error(self):
        """libpq 16 미만은 sslrootcert=system을 모른다. 조용히 require로 낮추면 안 된다."""
        with pytest.raises(ConnectionConfigError) as exc:
            build_libpq_params(_base(ssl=True), libpq_version=OLD_LIBPQ)
        message = str(exc.value)
        assert "CA" in message
        assert "15" in message  # 설치된 libpq 버전을 알려 준다

    def test_verify_ca_requires_ca_file(self):
        """libpq는 sslrootcert=system을 verify-full에서만 허용한다."""
        with pytest.raises(ConnectionConfigError, match="CA"):
            build_libpq_params(_base(ssl=True, sslmode="verify-ca"), libpq_version=MODERN_LIBPQ)

    def test_verify_ca_with_ca_file(self, ca_file):
        params = build_libpq_params(
            _base(ssl=True, sslmode="verify-ca", sslrootcert=str(ca_file)),
            libpq_version=MODERN_LIBPQ,
        )
        assert params["sslmode"] == "verify-ca"
        assert params["sslrootcert"] == str(ca_file)

    @pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "bogus"])
    def test_unknown_or_weak_modes_are_rejected(self, mode):
        with pytest.raises(ConnectionConfigError):
            build_libpq_params(_base(ssl=True, sslmode=mode), libpq_version=MODERN_LIBPQ)

    def test_require_without_risk_ack_is_rejected(self):
        with pytest.raises(ConnectionConfigError, match="위험"):
            build_libpq_params(_base(ssl=True, sslmode="require"), libpq_version=MODERN_LIBPQ)

    def test_require_with_risk_ack_is_allowed_and_audited(self):
        audit = MagicMock()
        params = build_libpq_params(
            _base(ssl=True, sslmode="require", ssl_allow_insecure=True),
            libpq_version=MODERN_LIBPQ,
            audit=audit,
        )

        assert params["sslmode"] == "require"
        assert "sslrootcert" not in params
        audit.assert_called_once()
        audit_message = audit.call_args.args[0]
        assert "db.example.com" in audit_message
        assert "require" in audit_message
        assert "pw" not in audit_message.split()  # 비밀번호는 감사 로그에 남기지 않는다

    def test_default_audit_goes_to_audit_logger(self, caplog, monkeypatch):
        monkeypatch.setattr(cp, "_emit_app_log", lambda level, message: None)
        with caplog.at_level(logging.WARNING, logger=cp.AUDIT_LOGGER_NAME):
            build_libpq_params(
                _base(ssl=True, sslmode="require", ssl_allow_insecure=True),
                libpq_version=MODERN_LIBPQ,
            )
        records = [r for r in caplog.records if r.name == cp.AUDIT_LOGGER_NAME]
        assert records and records[0].levelno == logging.WARNING
        assert "password" not in records[0].getMessage()

    def test_secure_mode_is_not_audited(self, ca_file):
        audit = MagicMock()
        build_libpq_params(
            _base(ssl=True, sslrootcert=str(ca_file)), libpq_version=MODERN_LIBPQ, audit=audit
        )
        audit.assert_not_called()

    def test_validate_tls_settings_reports_problem_without_raising(self):
        assert cp.validate_tls_settings(_base(), libpq_version=MODERN_LIBPQ) is None
        message = cp.validate_tls_settings(
            _base(ssl=True, sslmode="require"), libpq_version=MODERN_LIBPQ
        )
        assert message and "위험" in message


# ── 설치된 드라이버의 libpq 버전 분기 ─────────────────────────


class TestLibpqVersion:
    def test_reads_psycopg_runtime_libpq(self, monkeypatch):
        import psycopg

        monkeypatch.setattr(psycopg.pq, "version", lambda: 150004)
        assert cp.libpq_version("psycopg") == 150004

    def test_reads_psycopg2_runtime_libpq(self, monkeypatch):
        import psycopg2.extensions

        monkeypatch.setattr(psycopg2.extensions, "libpq_version", lambda: 140010)
        assert cp.libpq_version("psycopg2") == 140010

    def test_builder_branches_on_the_driver_that_will_connect(self, monkeypatch):
        """psycopg와 psycopg2는 각자 다른 libpq를 번들한다. 연결할 드라이버 기준으로 판단한다."""
        import psycopg
        import psycopg2.extensions

        monkeypatch.setattr(psycopg.pq, "version", lambda: 170000)
        monkeypatch.setattr(psycopg2.extensions, "libpq_version", lambda: 150000)

        assert build_libpq_params(_base(ssl=True), driver="psycopg")["sslrootcert"] == "system"
        with pytest.raises(ConnectionConfigError):
            build_libpq_params(_base(ssl=True), driver="psycopg2")


# ── 연결 헬퍼 ────────────────────────────────────────────────


class TestConnectHelpers:
    def test_connect_psycopg_passes_built_params(self, monkeypatch):
        import psycopg

        captured = {}
        monkeypatch.setattr(psycopg, "connect", lambda **kw: captured.update(kw) or "conn")

        assert cp.connect_psycopg(_base(), connect_timeout=5) == "conn"
        assert captured["connect_timeout"] == 5
        assert captured["options"] == "-c search_path=public"
        assert captured["dbname"] == "bms30"

    def test_connect_psycopg2_passes_built_params(self, monkeypatch):
        import psycopg2

        captured = {}
        monkeypatch.setattr(psycopg2, "connect", lambda **kw: captured.update(kw) or "conn")

        assert cp.connect_psycopg2(_base()) == "conn"
        assert captured["connect_timeout"] == 10
        assert captured["options"] == "-c search_path=public"

    def test_config_error_happens_before_any_network_call(self, monkeypatch):
        import psycopg

        connect = MagicMock()
        monkeypatch.setattr(psycopg, "connect", connect)
        with pytest.raises(ConnectionConfigError):
            cp.connect_psycopg(_base(ssl=True, sslmode="require"))
        connect.assert_not_called()


# ── 모든 연결 지점이 빌더를 쓰는가 ──────────────────────────

_ALLOWED_DIRECT_CONNECT = {Path("database/connection_params.py")}
_FORBIDDEN_CALLS = {
    ("psycopg", "connect"),
    ("psycopg2", "connect"),
}
_FORBIDDEN_NAMES = {"ConnectionPool", "AsyncConnectionPool", "NullConnectionPool"}


def _src_files():
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


class TestAllConnectionSitesUseBuilder:
    def test_no_direct_driver_connect_outside_builder(self):
        offenders = []
        for path in _src_files():
            rel = path.relative_to(SRC_ROOT)
            if rel in _ALLOWED_DIRECT_CONNECT:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and (func.value.id, func.attr) in _FORBIDDEN_CALLS
                ):
                    offenders.append(f"{rel}:{node.lineno} {func.value.id}.{func.attr}")
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in _FORBIDDEN_NAMES:
                    offenders.append(f"{rel}:{node.lineno} {name}")
        assert offenders == [], "연결은 connection_params 헬퍼로만 만든다: " + ", ".join(offenders)

    def test_no_driver_connect_imported_by_name(self):
        """`from psycopg import connect`로 우회하면 위 검사를 빠져나간다."""
        offenders = []
        for path in _src_files():
            rel = path.relative_to(SRC_ROOT)
            if rel in _ALLOWED_DIRECT_CONNECT:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in {
                    "psycopg",
                    "psycopg2",
                    "psycopg_pool",
                }:
                    names = {a.name for a in node.names}
                    if names & ({"connect"} | _FORBIDDEN_NAMES):
                        offenders.append(f"{rel}:{node.lineno}")
        assert offenders == []

    def test_create_engine_only_for_local_sqlite(self):
        offenders = []
        for path in _src_files():
            rel = path.relative_to(SRC_ROOT)
            if "create_engine(" in path.read_text(encoding="utf-8") and rel != Path(
                "database/local_db.py"
            ):
                offenders.append(str(rel))
        assert offenders == []

    def test_no_hardcoded_sslmode_in_connection_sites(self):
        """빌더 밖에서 sslmode 값을 직접 채우면 그 경로만 검증 없이 연결된다.

        `config["sslmode"] = "require"`, `{"sslmode": "require"}`, `connect(sslmode=...)`를 찾는다.
        """
        offenders = []
        for path in _src_files():
            rel = path.relative_to(SRC_ROOT)
            if rel in _ALLOWED_DIRECT_CONNECT:
                continue
            # connection_mapper.ui_to_psycopg_config는 연결에 쓰이지 않는 레거시 dict 변환기다
            # (아래 테스트가 호출되지 않음을 보장한다).
            if rel == Path("ui/dialogs/connection_mapper.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "sslmode"
                        ):
                            offenders.append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values, strict=True):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "sslmode"
                            and isinstance(v, ast.Constant)
                        ):
                            offenders.append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.Call):
                    if any(kw.arg == "sslmode" for kw in node.keywords):
                        offenders.append(f"{rel}:{node.lineno}")
        assert offenders == []

    def test_legacy_mapper_psycopg_config_is_not_used_to_connect(self):
        users = []
        for path in _src_files():
            rel = path.relative_to(SRC_ROOT)
            if rel == Path("ui/dialogs/connection_mapper.py"):
                continue
            if "ui_to_psycopg_config" in path.read_text(encoding="utf-8"):
                users.append(str(rel))
        assert users == []


# ── 각 연결 지점이 실제로 빌더 결과로 연결하는가 ─────────────


def _legacy_ssl_config():
    return _base(ssl=True)  # 기존 'SSL 사용' 체크 프로필


@pytest.fixture
def modern_libpq(monkeypatch):
    import psycopg
    import psycopg2.extensions

    monkeypatch.setattr(psycopg.pq, "version", lambda: MODERN_LIBPQ)
    monkeypatch.setattr(psycopg2.extensions, "libpq_version", lambda: MODERN_LIBPQ)


def _assert_secure(kwargs):
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"] == "system"
    assert kwargs["options"] == "-c search_path=public"
    assert kwargs["connect_timeout"] > 0


@pytest.fixture
def psycopg_connect(monkeypatch):
    import psycopg

    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    return connect


@pytest.fixture
def psycopg2_connect(monkeypatch):
    import psycopg2

    connect = MagicMock()
    monkeypatch.setattr(psycopg2, "connect", connect)
    return connect


@pytest.mark.usefixtures("modern_libpq")
class TestConnectionSites:
    def test_copy_migration_worker(self, psycopg2_connect):
        from src.core.copy_migration_worker import CopyMigrationWorker

        stub = MagicMock()
        CopyMigrationWorker._create_psycopg2_connection(stub, _legacy_ssl_config())
        _assert_secure(psycopg2_connect.call_args.kwargs)

    def test_copy_migration_worker_connect_timeout(self, psycopg2_connect):
        from src.core.copy_migration_worker import CopyMigrationWorker

        CopyMigrationWorker._create_psycopg2_connection(MagicMock(), _base())
        assert psycopg2_connect.call_args.kwargs["connect_timeout"] == 10

    def test_legacy_migration_worker(self, psycopg_connect):
        from src.core.migration_worker import MigrationWorker

        MigrationWorker._create_connection(MagicMock(), _legacy_ssl_config())
        _assert_secure(psycopg_connect.call_args.kwargs)

    def test_partition_discovery(self, psycopg_connect):
        from src.core.partition_discovery import PartitionDiscovery

        PartitionDiscovery(_legacy_ssl_config())._create_connection()
        _assert_secure(psycopg_connect.call_args.kwargs)

    def test_file_archive_worker(self, psycopg2_connect):
        from src.core.file_archive_workers import PostgresToFileArchiveWorker

        PostgresToFileArchiveWorker._create_psycopg2_connection(_legacy_ssl_config())
        _assert_secure(psycopg2_connect.call_args.kwargs)

    def test_target_completed_scan_worker(self, psycopg_connect):
        from src.core.scan_workers import TargetCompletedScanWorker

        conn = psycopg_connect.return_value
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
        TargetCompletedScanWorker(1, "postgres", _legacy_ssl_config(), ["t"]).execute()
        _assert_secure(psycopg_connect.call_args.kwargs)

    def test_row_count_verify_worker(self, psycopg_connect):
        from src.core.scan_workers import RowCountVerifyWorker

        worker = RowCountVerifyWorker(1, _legacy_ssl_config(), _legacy_ssl_config(), [])
        worker.execute()
        assert psycopg_connect.call_count == 2
        for call in psycopg_connect.call_args_list:
            _assert_secure(call.kwargs)

    def test_postgres_utils_quick_check(self, psycopg_connect):
        from src.database.postgres_utils import PostgresOptimizer

        ok, _msg = PostgresOptimizer.check_connection_quick(_legacy_ssl_config())
        assert ok is True
        _assert_secure(psycopg_connect.call_args.kwargs)
        assert psycopg_connect.call_args.kwargs["connect_timeout"] == 5

    def test_postgres_utils_quick_check_reports_config_error(self, psycopg_connect):
        from src.database.postgres_utils import PostgresOptimizer

        ok, msg = PostgresOptimizer.check_connection_quick(_base(ssl=True, sslmode="require"))
        assert ok is False
        assert "위험" in msg
        psycopg_connect.assert_not_called()


# ── 연결 대화상자: SSL 모드·CA 입력 (H-05 UI) ────────────────


@pytest.fixture
def isolated_presets(monkeypatch):
    """저장된 연결(로컬 사용자 DB)에 손대지 않는다."""
    from src.ui.dialogs import connection_dialog as dialog_mod

    manager = MagicMock()
    manager.get_all.return_value = []
    monkeypatch.setattr(dialog_mod, "SavedConnectionManager", lambda: manager)
    return manager


@pytest.fixture
def conn_dialog(qapp, isolated_presets):
    from src.ui.dialogs.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog(None)
    yield dialog
    dialog.deleteLater()


def _fill_basic(widgets):
    widgets["host"].setText("db.example.com")
    widgets["database"].setText("bms30")
    widgets["username"].setText("migtool")
    widgets["password"].setText("pw")


@pytest.mark.usefixtures("isolated_presets")
class TestConnectionDialogTls:
    def test_ssl_widgets_exist_and_default_to_verify_full(self, conn_dialog):
        w = conn_dialog.endpoint_widgets["source"]
        assert w["sslmode"].currentData() == "verify-full"
        assert w["sslrootcert"].text() == ""
        assert not w["ssl_allow_insecure"].isChecked()

    def test_tls_widgets_follow_ssl_checkbox(self, conn_dialog):
        w = conn_dialog.endpoint_widgets["source"]
        w["ssl"].setChecked(False)
        assert not w["sslmode"].isEnabled()
        assert not w["sslrootcert"].isEnabled()
        w["ssl"].setChecked(True)
        assert w["sslmode"].isEnabled()
        assert w["sslrootcert"].isEnabled()

    def test_risk_ack_only_shown_for_require(self, conn_dialog):
        w = conn_dialog.endpoint_widgets["source"]
        w["ssl"].setChecked(True)
        assert w["ssl_allow_insecure"].isHidden()
        w["sslmode"].setCurrentIndex(w["sslmode"].findData("require"))
        assert not w["ssl_allow_insecure"].isHidden()

    def test_ssl_off_profile_config_keeps_legacy_shape(self, conn_dialog):
        _fill_basic(conn_dialog.endpoint_widgets["source"])
        config = conn_dialog._get_endpoint_profile_config("source")
        assert config["ssl"] is False
        assert "sslmode" not in config
        assert "sslrootcert" not in config

    def test_ssl_on_profile_config_carries_mode_and_ca(self, conn_dialog, ca_file):
        w = conn_dialog.endpoint_widgets["source"]
        _fill_basic(w)
        w["ssl"].setChecked(True)
        w["sslrootcert"].setText(str(ca_file))
        config = conn_dialog._get_endpoint_profile_config("source")

        assert config["ssl"] is True
        assert config["sslmode"] == "verify-full"
        assert config["sslrootcert"] == str(ca_file)
        assert config["ssl_allow_insecure"] is False
        params = build_libpq_params(config, libpq_version=MODERN_LIBPQ)
        assert params["sslrootcert"] == str(ca_file)

    def test_legacy_ssl_profile_loads_as_verify_full(self, qapp):
        """기존 'SSL 사용' 체크만 있는 프로필을 열면 require가 아니라 verify-full로 보인다."""
        from src.models.profile import ConnectionProfile
        from src.ui.dialogs.connection_dialog import ConnectionDialog

        profile = ConnectionProfile(
            id=1,
            name="legacy",
            source_config=_base(ssl=True),
            target_config=_base(ssl=False),
        )
        dialog = ConnectionDialog(None, profile)
        try:
            w = dialog.endpoint_widgets["source"]
            assert w["ssl"].isChecked()
            assert w["sslmode"].currentData() == "verify-full"
            assert w["sslrootcert"].text() == ""
            assert dialog._get_endpoint_profile_config("source")["sslmode"] == "verify-full"
        finally:
            dialog.deleteLater()

    def test_saved_tls_settings_round_trip(self, qapp, ca_file):
        from src.models.profile import ConnectionProfile
        from src.ui.dialogs.connection_dialog import ConnectionDialog

        source = _base(
            ssl=True, sslmode="require", sslrootcert=str(ca_file), ssl_allow_insecure=True
        )
        profile = ConnectionProfile(id=1, name="p", source_config=source, target_config=_base())
        dialog = ConnectionDialog(None, profile)
        try:
            w = dialog.endpoint_widgets["source"]
            assert w["sslmode"].currentData() == "require"
            assert w["sslrootcert"].text() == str(ca_file)
            assert w["ssl_allow_insecure"].isChecked()
            config = dialog._get_endpoint_profile_config("source")
            assert config["sslmode"] == "require"
            assert config["ssl_allow_insecure"] is True
        finally:
            dialog.deleteLater()

    def test_changing_tls_settings_invalidates_test_result(self, conn_dialog):
        w = conn_dialog.endpoint_widgets["source"]
        w["ssl"].setChecked(True)
        for change in (
            lambda: w["sslmode"].setCurrentIndex(w["sslmode"].findData("verify-ca")),
            lambda: w["sslrootcert"].setText("C:/ca.crt"),
            lambda: w["ssl_allow_insecure"].setChecked(True),
        ):
            w["result_lamp"].set_state("ok", "연결됨")
            change()
            assert w["result_lamp"].state == "idle"

    def test_connection_test_uses_builder(self, conn_dialog, modern_libpq, psycopg_connect):
        w = conn_dialog.endpoint_widgets["source"]
        _fill_basic(w)
        w["ssl"].setChecked(True)
        cursor = psycopg_connect.return_value.__enter__.return_value.cursor.return_value
        cursor.__enter__.return_value.fetchone.return_value = ("PostgreSQL 16.2 on x86_64",)

        conn_dialog.test_connection("source")

        _assert_secure(psycopg_connect.call_args.kwargs)
        assert psycopg_connect.call_args.kwargs["connect_timeout"] == 7

    def test_connection_test_refuses_unapproved_require(self, conn_dialog, psycopg_connect):
        w = conn_dialog.endpoint_widgets["source"]
        _fill_basic(w)
        w["ssl"].setChecked(True)
        w["sslmode"].setCurrentIndex(w["sslmode"].findData("require"))

        conn_dialog.test_connection("source")

        psycopg_connect.assert_not_called()
        assert w["result_lamp"].state == "error"

    def test_save_blocked_for_unapproved_require(self, conn_dialog, monkeypatch):
        from src.ui.dialogs import connection_dialog as dialog_mod

        warnings = []
        monkeypatch.setattr(
            dialog_mod.QMessageBox, "warning", lambda *a, **k: warnings.append(a[1:])
        )
        conn_dialog.name_edit.setText("tls")
        for side in ("source", "target"):
            _fill_basic(conn_dialog.endpoint_widgets[side])
        conn_dialog.endpoint_widgets["target"]["database"].setText("temp")
        w = conn_dialog.endpoint_widgets["source"]
        w["ssl"].setChecked(True)
        w["sslmode"].setCurrentIndex(w["sslmode"].findData("require"))

        conn_dialog.accept()

        assert warnings, "위험 승인 없는 require 설정은 저장되면 안 됩니다"
        assert "위험" in warnings[-1][1]
        assert conn_dialog.result() != dialog_mod.QDialog.DialogCode.Accepted


# ── 실제 TLS 서버 대상 검증 (H-05 완료 판정) ─────────────────
# 로컬 PostgreSQL 바이너리(initdb/pg_ctl)로 일회용 TLS 서버를 띄운다. 없으면 건너뛴다.
# 실행: pytest -m integration tests/database/test_connection_params.py
#   (바이너리 위치는 PATH 또는 DBMIG_TEST_PG_BIN)


def _find_pg_bin() -> Path | None:
    import os
    import shutil

    candidates = [os.environ.get("DBMIG_TEST_PG_BIN")]
    found = shutil.which("initdb")
    candidates.append(str(Path(found).parent) if found else None)
    candidates += [f"/opt/homebrew/opt/postgresql@{v}/bin" for v in (17, 16, 15)]
    candidates += [f"/usr/lib/postgresql/{v}/bin" for v in (17, 16, 15)]
    for c in candidates:
        if c and (Path(c) / "initdb").exists() and (Path(c) / "pg_ctl").exists():
            return Path(c)
    return None


def _make_ca(common_name: str):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_server_cert(ca_key, ca_cert, dns_name: str):
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _write_pem(path: Path, *, cert=None, key=None) -> Path:
    from cryptography.hazmat.primitives import serialization

    if cert is not None:
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    else:
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    import socket
    import subprocess

    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("로컬 PostgreSQL 바이너리(initdb/pg_ctl)가 없습니다")

    root = tmp_path_factory.mktemp("tls_pg")
    ca_key, ca_cert = _make_ca("dbmig-test-ca")
    srv_key, srv_cert = _make_server_cert(ca_key, ca_cert, "localhost")
    # 클라이언트가 엉뚱한 CA를 신뢰 = 서버 인증서가 신뢰 CA 밖에서 온 경우(중간자)와 같은 검증 경로
    _rogue_key, rogue_ca = _make_ca("dbmig-rogue-ca")

    files = {
        "ca": _write_pem(root / "ca.crt", cert=ca_cert),
        "rogue_ca": _write_pem(root / "rogue_ca.crt", cert=rogue_ca),
        "server_crt": _write_pem(root / "server.crt", cert=srv_cert),
        "server_key": _write_pem(root / "server.key", key=srv_key),
    }

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    data = root / "data"
    subprocess.run(
        [str(pg_bin / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    opts = (
        f"-p {port} -c listen_addresses=localhost -c unix_socket_directories='' -c ssl=on "
        f"-c ssl_cert_file={files['server_crt']} -c ssl_key_file={files['server_key']}"
    )
    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(data),
            "-o",
            opts,
            "-w",
            "-l",
            str(root / "log"),
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield {"port": port, **files}
    finally:
        subprocess.run(
            [str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
        )


def _tls_config(server, **overrides):
    config = {
        "host": "localhost",
        "port": server["port"],
        "database": "postgres",
        "username": "postgres",
        "password": "",
        "ssl": True,
        "connect_timeout": 5,
    }
    config.update(overrides)
    return config


def _ssl_in_use(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        return bool(cur.fetchone()[0])


@pytest.mark.integration
class TestRealTlsServer:
    @pytest.mark.parametrize("connect", [cp.connect_psycopg, cp.connect_psycopg2])
    def test_correct_ca_and_hostname_succeeds(self, tls_server, connect):
        conn = connect(_tls_config(tls_server, sslrootcert=str(tls_server["ca"])))
        try:
            assert _ssl_in_use(conn)
            with conn.cursor() as cur:
                cur.execute("SHOW search_path")
                assert cur.fetchone()[0] == "public"
        finally:
            conn.close()

    @pytest.mark.parametrize("connect", [cp.connect_psycopg, cp.connect_psycopg2])
    def test_wrong_ca_fails(self, tls_server, connect):
        """다른 CA(중간자 인증서를 서명할 수 있는 CA)를 신뢰하면 연결이 거부된다."""
        with pytest.raises(Exception, match="certificate verify failed"):
            connect(_tls_config(tls_server, sslrootcert=str(tls_server["rogue_ca"])))

    def test_hostname_mismatch_fails_under_verify_full(self, tls_server):
        """인증서는 localhost용이다. 127.0.0.1로 접속하면 verify-full이 거부한다."""
        with pytest.raises(Exception, match="does not match host name|server certificate"):
            cp.connect_psycopg(
                _tls_config(tls_server, host="127.0.0.1", sslrootcert=str(tls_server["ca"]))
            )

    def test_hostname_mismatch_allowed_only_under_verify_ca(self, tls_server):
        conn = cp.connect_psycopg(
            _tls_config(
                tls_server,
                host="127.0.0.1",
                sslmode="verify-ca",
                sslrootcert=str(tls_server["ca"]),
            )
        )
        conn.close()

    def test_private_ca_not_trusted_by_system_store(self, tls_server):
        """CA를 지정하지 않으면 시스템 저장소로 검증하므로 사설 CA 인증서는 거부된다."""
        with pytest.raises(Exception, match="certificate verify failed"):
            cp.connect_psycopg(_tls_config(tls_server))

    def test_require_connects_only_with_ack_and_is_audited(self, tls_server):
        with pytest.raises(ConnectionConfigError):
            cp.connect_psycopg(_tls_config(tls_server, sslmode="require"))

        audit = MagicMock()
        params = build_libpq_params(
            _tls_config(tls_server, sslmode="require", ssl_allow_insecure=True), audit=audit
        )
        import psycopg

        with psycopg.connect(**params) as conn:
            assert _ssl_in_use(conn)
        audit.assert_called_once()
