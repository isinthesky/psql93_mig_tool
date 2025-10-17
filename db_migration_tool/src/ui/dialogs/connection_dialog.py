"""
연결 설정 다이얼로그
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QSpinBox, QPushButton, QTabWidget,
    QWidget, QMessageBox, QCheckBox, QDialogButtonBox, QLabel
)
from PySide6.QtCore import Qt
import psycopg

from src.models.profile import ConnectionProfile
from src.utils.validators import ConnectionValidator
from src.utils.logger import logger
from .connection_mapper import ConnectionMapper


class ConnectionDialog(QDialog):
    """연결 설정 다이얼로그"""
    
    def __init__(self, parent=None, profile: ConnectionProfile = None):
        super().__init__(parent)
        self.profile = profile
        self.is_edit_mode = profile is not None
        
        self.setup_ui()
        if self.is_edit_mode:
            self.load_profile_data()
            
    def setup_ui(self):
        """UI 초기화"""
        self.setWindowTitle("연결 편집" if self.is_edit_mode else "새 연결")
        self.setModal(True)
        self.resize(500, 400)
        
        layout = QVBoxLayout(self)
        
        # 프로필 이름
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("프로필 이름:"))
        self.name_edit = QLineEdit()
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)
        
        # 탭 위젯
        self.tab_widget = QTabWidget()
        
        # 소스 DB 탭
        self.source_tab = self.create_db_tab("소스")
        self.tab_widget.addTab(self.source_tab, "소스 데이터베이스")
        
        # 대상 DB 탭
        self.target_tab = self.create_db_tab("대상")
        self.tab_widget.addTab(self.target_tab, "대상 데이터베이스")
        
        layout.addWidget(self.tab_widget)
        
        # 버튼
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        
        # 연결 테스트 버튼 추가
        self.test_source_btn = QPushButton("소스 테스트")
        self.test_source_btn.clicked.connect(lambda: self.test_connection("source"))
        button_box.addButton(self.test_source_btn, QDialogButtonBox.ActionRole)
        
        self.test_target_btn = QPushButton("대상 테스트")
        self.test_target_btn.clicked.connect(lambda: self.test_connection("target"))
        button_box.addButton(self.test_target_btn, QDialogButtonBox.ActionRole)
        
        layout.addWidget(button_box)
        
    def create_db_tab(self, db_type: str):
        """데이터베이스 탭 생성"""
        widget = QWidget()
        layout = QFormLayout(widget)
        
        # 호스트
        host_edit = QLineEdit()
        host_edit.setPlaceholderText("localhost")
        layout.addRow("호스트:", host_edit)
        
        # 포트
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(5432)
        layout.addRow("포트:", port_spin)
        
        # 데이터베이스
        database_edit = QLineEdit()
        database_edit.setPlaceholderText("데이터베이스명")
        layout.addRow("데이터베이스:", database_edit)
        
        # 사용자명
        username_edit = QLineEdit()
        username_edit.setPlaceholderText("사용자명")
        layout.addRow("사용자명:", username_edit)
        
        # 비밀번호
        password_edit = QLineEdit()
        password_edit.setEchoMode(QLineEdit.Password)
        password_edit.setPlaceholderText("비밀번호")
        layout.addRow("비밀번호:", password_edit)
        
        # SSL 사용
        ssl_check = QCheckBox("SSL 연결 사용")
        layout.addRow("", ssl_check)
        
        # 위젯 참조 저장
        if db_type == "소스":
            self.source_host = host_edit
            self.source_port = port_spin
            self.source_database = database_edit
            self.source_username = username_edit
            self.source_password = password_edit
            self.source_ssl = ssl_check
        else:
            self.target_host = host_edit
            self.target_port = port_spin
            self.target_database = database_edit
            self.target_username = username_edit
            self.target_password = password_edit
            self.target_ssl = ssl_check
            
        return widget
        
    def load_profile_data(self):
        """프로필 데이터 로드 (ConnectionMapper 활용)"""
        if not self.profile:
            return

        self.name_edit.setText(self.profile.name)

        # 소스 설정 (ConnectionMapper 활용)
        ConnectionMapper.set_ui_from_config(
            self.profile.source_config,
            self.source_host, self.source_port, self.source_database,
            self.source_username, self.source_password, self.source_ssl
        )

        # 대상 설정 (ConnectionMapper 활용)
        ConnectionMapper.set_ui_from_config(
            self.profile.target_config,
            self.target_host, self.target_port, self.target_database,
            self.target_username, self.target_password, self.target_ssl
        )
        
    def get_profile_data(self):
        """프로필 데이터 가져오기 (ConnectionMapper 활용)"""
        return {
            'name': self.name_edit.text().strip(),
            'source_config': ConnectionMapper.ui_to_profile_config(
                self.source_host, self.source_port, self.source_database,
                self.source_username, self.source_password, self.source_ssl
            ),
            'target_config': ConnectionMapper.ui_to_profile_config(
                self.target_host, self.target_port, self.target_database,
                self.target_username, self.target_password, self.target_ssl
            )
        }
        
    def test_connection(self, db_type: str):
        """연결 테스트 (ConnectionMapper 활용)"""
        if db_type == "source":
            config = ConnectionMapper.ui_to_psycopg_config(
                self.source_host, self.source_port, self.source_database,
                self.source_username, self.source_password, self.source_ssl
            )
        else:
            config = ConnectionMapper.ui_to_psycopg_config(
                self.target_host, self.target_port, self.target_database,
                self.target_username, self.target_password, self.target_ssl
            )
                
        try:
            # 연결 테스트
            conn = psycopg.connect(**config)
            
            # 버전 확인
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                
                # PostgreSQL 9.3 호환성 확인
                if "PostgreSQL 9." not in version and "PostgreSQL 10." not in version:
                    QMessageBox.warning(
                        self,
                        "버전 경고",
                        f"PostgreSQL 버전이 9.3과 다릅니다:\n{version}\n\n"
                        "호환성 문제가 발생할 수 있습니다."
                    )
                    
            conn.close()
            
            QMessageBox.information(
                self,
                "연결 성공",
                f"{db_type} 데이터베이스 연결에 성공했습니다.\n\n{version}"
            )
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "연결 실패",
                f"{db_type} 데이터베이스 연결에 실패했습니다:\n\n{str(e)}"
            )
            
    def accept(self):
        """다이얼로그 확인 (ConnectionMapper 활용)"""
        # 프로필 이름 검증
        name = self.name_edit.text().strip()
        valid, msg = ConnectionValidator.validate_profile_name(name)
        if not valid:
            QMessageBox.warning(self, "입력 오류", msg)
            return

        # 소스 설정 검증 (ConnectionMapper 활용)
        source_config = ConnectionMapper.ui_to_validation_config(
            self.source_host, self.source_port,
            self.source_database, self.source_username
        )
        valid, msg = ConnectionValidator.validate_connection_config(source_config)
        if not valid:
            QMessageBox.warning(self, "소스 DB 오류", msg)
            return

        # 대상 설정 검증 (ConnectionMapper 활용)
        target_config = ConnectionMapper.ui_to_validation_config(
            self.target_host, self.target_port,
            self.target_database, self.target_username
        )
        valid, msg = ConnectionValidator.validate_connection_config(target_config)
        if not valid:
            QMessageBox.warning(self, "대상 DB 오류", msg)
            return

        logger.info(f"연결 프로필 저장: {name}")
        super().accept()