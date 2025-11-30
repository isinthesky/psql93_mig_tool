"""연결 설정 UI ↔ Dict 매핑 유틸리티

ConnectionDialog에서 UI 위젯과 Dict 간 변환 로직을 중앙집중화합니다.
"""

from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QSpinBox

# 호환 모드 상수
COMPAT_MODE_AUTO = "auto"
COMPAT_MODE_9_3 = "9.3"
COMPAT_MODE_16 = "16"

# UI 표시용 호환 모드 매핑
COMPAT_MODE_LABELS = {
    COMPAT_MODE_AUTO: "자동 감지",
    COMPAT_MODE_9_3: "PostgreSQL 9.3",
    COMPAT_MODE_16: "PostgreSQL 16",
}

# UI 레이블 → 값 역매핑
COMPAT_LABEL_TO_MODE = {v: k for k, v in COMPAT_MODE_LABELS.items()}


class ConnectionMapper:
    """DB 연결 설정 UI 컴포넌트와 Dict 간 변환 헬퍼

    Examples:
        >>> from PySide6.QtWidgets import QLineEdit, QSpinBox, QCheckBox
        >>> host = QLineEdit()
        >>> host.setText("localhost")
        >>> port = QSpinBox()
        >>> port.setValue(5432)
        >>> # ... 다른 위젯들
        >>> config = ConnectionMapper.ui_to_profile_config(host, port, ...)
    """

    @staticmethod
    def ui_to_profile_config(
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ) -> dict[str, Any]:
        """UI 위젯 → 프로필 저장용 Dict

        Args:
            host: 호스트 입력 위젯
            port: 포트 입력 위젯
            database: 데이터베이스 입력 위젯
            username: 사용자명 입력 위젯
            password: 비밀번호 입력 위젯
            ssl: SSL 체크박스
            compat_mode: 호환 모드 콤보박스 (선택)

        Returns:
            프로필 저장에 사용할 딕셔너리

        Examples:
            >>> config = ConnectionMapper.ui_to_profile_config(
            ...     host, port, database, username, password, ssl, compat_mode
            ... )
            >>> print(config['host'])  # 'localhost'
        """
        config = {
            "host": host.text().strip() or "localhost",
            "port": port.value(),
            "database": database.text().strip(),
            "username": username.text().strip(),
            "password": password.text(),
            "ssl": ssl.isChecked(),
        }

        # 호환 모드 추가
        if compat_mode is not None:
            label = compat_mode.currentText()
            config["compat_mode"] = COMPAT_LABEL_TO_MODE.get(label, COMPAT_MODE_AUTO)
        else:
            config["compat_mode"] = COMPAT_MODE_AUTO

        return config

    @staticmethod
    def ui_to_psycopg_config(
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
    ) -> dict[str, Any]:
        """UI 위젯 → psycopg 연결용 Dict

        Args:
            host: 호스트 입력 위젯
            port: 포트 입력 위젯
            database: 데이터베이스 입력 위젯
            username: 사용자명 입력 위젯
            password: 비밀번호 입력 위젯
            ssl: SSL 체크박스

        Returns:
            psycopg.connect()에 사용할 딕셔너리
            (키 이름이 psycopg 파라미터와 일치)

        Examples:
            >>> config = ConnectionMapper.ui_to_psycopg_config(...)
            >>> import psycopg
            >>> conn = psycopg.connect(**config)
        """
        config = {
            "host": host.text().strip() or "localhost",
            "port": port.value(),
            "dbname": database.text().strip(),  # psycopg는 'dbname' 사용
            "user": username.text().strip(),  # psycopg는 'user' 사용
            "password": password.text(),
        }

        if ssl.isChecked():
            config["sslmode"] = "require"

        return config

    @staticmethod
    def ui_to_validation_config(
        host: QLineEdit, port: QSpinBox, database: QLineEdit, username: QLineEdit
    ) -> dict[str, Any]:
        """UI 위젯 → 검증용 Dict

        Args:
            host: 호스트 입력 위젯
            port: 포트 입력 위젯
            database: 데이터베이스 입력 위젯
            username: 사용자명 입력 위젯

        Returns:
            ConnectionValidator.validate_connection_config()에 사용할 딕셔너리
            (비밀번호 불필요)

        Examples:
            >>> config = ConnectionMapper.ui_to_validation_config(...)
            >>> from src.utils.validators import ConnectionValidator
            >>> valid, msg = ConnectionValidator.validate_connection_config(config)
        """
        return {
            "host": host.text().strip() or "localhost",
            "port": port.value(),
            "database": database.text().strip(),
            "username": username.text().strip(),
        }

    @staticmethod
    def profile_config_to_ui(config: dict[str, Any]) -> tuple[str, int, str, str, str, bool, str]:
        """프로필 Dict → UI 값 튜플

        Args:
            config: 프로필 설정 딕셔너리

        Returns:
            (host, port, database, username, password, ssl, compat_mode) 튜플

        Examples:
            >>> config = {'host': 'localhost', 'port': 5432, ...}
            >>> host, port, db, user, pwd, ssl, compat = ConnectionMapper.profile_config_to_ui(config)
        """
        return (
            config.get("host", "localhost"),
            config.get("port", 5432),
            config.get("database", ""),
            config.get("username", ""),
            config.get("password", ""),
            config.get("ssl", False),
            config.get("compat_mode", COMPAT_MODE_AUTO),
        )

    @staticmethod
    def set_ui_from_config(
        config: dict[str, Any],
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ):
        """프로필 Dict → UI 위젯 설정

        Args:
            config: 프로필 설정 딕셔너리
            host: 호스트 입력 위젯
            port: 포트 입력 위젯
            database: 데이터베이스 입력 위젯
            username: 사용자명 입력 위젯
            password: 비밀번호 입력 위젯
            ssl: SSL 체크박스
            compat_mode: 호환 모드 콤보박스 (선택)

        Examples:
            >>> ConnectionMapper.set_ui_from_config(
            ...     profile.source_config,
            ...     host_edit, port_spin, db_edit, user_edit, pwd_edit, ssl_check, compat_combo
            ... )
        """
        host.setText(config.get("host", "localhost"))
        port.setValue(config.get("port", 5432))
        database.setText(config.get("database", ""))
        username.setText(config.get("username", ""))
        password.setText(config.get("password", ""))
        ssl.setChecked(config.get("ssl", False))

        # 호환 모드 설정
        if compat_mode is not None:
            mode = config.get("compat_mode", COMPAT_MODE_AUTO)
            label = COMPAT_MODE_LABELS.get(mode, COMPAT_MODE_LABELS[COMPAT_MODE_AUTO])
            index = compat_mode.findText(label)
            if index >= 0:
                compat_mode.setCurrentIndex(index)


class ConnectionWidgetSet:
    """DB 연결 UI 위젯 세트 (타입 안전성 향상)

    여러 위젯을 하나의 객체로 묶어 관리합니다.

    Attributes:
        host: 호스트 입력 위젯
        port: 포트 입력 위젯
        database: 데이터베이스 입력 위젯
        username: 사용자명 입력 위젯
        password: 비밀번호 입력 위젯
        ssl: SSL 체크박스
        compat_mode: 호환 모드 콤보박스 (선택)

    Examples:
        >>> widgets = ConnectionWidgetSet(host_edit, port_spin, ...)
        >>> config = widgets.to_profile_config()
        >>> widgets.load_from_config(saved_config)
    """

    def __init__(
        self,
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ):
        """ConnectionWidgetSet 초기화

        Args:
            host: 호스트 입력 위젯
            port: 포트 입력 위젯
            database: 데이터베이스 입력 위젯
            username: 사용자명 입력 위젯
            password: 비밀번호 입력 위젯
            ssl: SSL 체크박스
            compat_mode: 호환 모드 콤보박스 (선택)
        """
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.ssl = ssl
        self.compat_mode = compat_mode

    def to_profile_config(self) -> dict[str, Any]:
        """프로필 저장용 Dict 반환

        Returns:
            프로필 저장용 딕셔너리
        """
        return ConnectionMapper.ui_to_profile_config(
            self.host, self.port, self.database, self.username, self.password, self.ssl,
            self.compat_mode
        )

    def to_psycopg_config(self) -> dict[str, Any]:
        """psycopg 연결용 Dict 반환

        Returns:
            psycopg 연결용 딕셔너리
        """
        return ConnectionMapper.ui_to_psycopg_config(
            self.host, self.port, self.database, self.username, self.password, self.ssl
        )

    def to_validation_config(self) -> dict[str, Any]:
        """검증용 Dict 반환

        Returns:
            검증용 딕셔너리 (비밀번호 제외)
        """
        return ConnectionMapper.ui_to_validation_config(
            self.host, self.port, self.database, self.username
        )

    def load_from_config(self, config: dict[str, Any]):
        """프로필 Dict에서 UI 위젯 설정

        Args:
            config: 프로필 설정 딕셔너리
        """
        ConnectionMapper.set_ui_from_config(
            config, self.host, self.port, self.database, self.username, self.password, self.ssl,
            self.compat_mode
        )
