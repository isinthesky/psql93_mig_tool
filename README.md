# PostgreSQL 파티션 테이블 마이그레이션 도구

PostgreSQL 9.3의 일별 파티션 테이블(point_history)을 효율적으로 마이그레이션하는 GUI 도구입니다.

## 주요 기능

- 📊 **파티션 자동 탐색**: 날짜 범위로 파티션 테이블 자동 검색
- 🚀 **고성능 마이그레이션**: COPY 명령 기반 대용량 데이터 전송
- 📈 **실시간 모니터링**: 진행률, 속도, 예상 시간 표시
- 🔐 **안전한 연결 관리**: 프로필별 암호화된 연결 정보 저장
- ⏸️ **일시정지/재개**: 중단된 작업 이어서 진행 가능
- 📝 **상세 로깅**: 모든 작업 이력 기록 및 조회

## 시스템 요구사항

- Python 3.9 이상
- PostgreSQL 9.3 이상 (소스 및 대상)
- Windows 10/11 또는 macOS 10.15 이상
- 최소 RAM: 4GB (권장: 8GB 이상)

## 설치 방법

### 1. 바이너리 다운로드 (권장)

[Releases](https://github.com/isinthesky/psql93_mig_tool/releases) 페이지에서 운영체제에 맞는 실행 파일을 다운로드하세요.

- Windows: `DB_Migration_Tool.exe`
- macOS: `DB_Migration_Tool.app`

### 2. 소스 코드 실행

#### 저장소 클론
```bash
git clone https://github.com/isinthesky/psql93_mig_tool.git
cd psql93_mig_tool/db_migration_tool
```

#### 가상환경 생성 및 활성화
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

#### 의존성 설치
```bash
pip install -r requirements.txt
```

#### 실행
```bash
python -m src.main
```

## 사용 방법

### 1. 연결 프로필 생성

1. 메인 화면에서 "새 프로필" 버튼 클릭
2. 프로필 이름 입력 (예: "프로덕션 마이그레이션")
3. 소스 데이터베이스 정보 입력:
   - 호스트: PostgreSQL 서버 주소
   - 포트: 5432 (기본값)
   - 데이터베이스: 데이터베이스 이름
   - 사용자명/비밀번호: 접속 정보
4. 대상 데이터베이스 정보 입력
5. "연결 테스트" 후 "저장"

### 2. 마이그레이션 실행

1. 생성한 프로필 선택 후 "마이그레이션 시작" 클릭
2. 달력에서 날짜 범위 선택
3. 파티션 목록 확인
4. "시작" 버튼 클릭

### 3. 진행 상황 모니터링

- **전체 진행률**: 완료된 파티션 수 / 전체 파티션 수
- **현재 파티션**: 처리 중인 파티션과 행 수
- **처리 속도**: 초당 처리 행 수 (rows/sec)
- **전송 속도**: 초당 데이터 전송량 (MB/sec)
- **예상 완료 시간**: 현재 속도 기준 남은 시간

### 4. 문제 해결

#### 연결 실패
- 방화벽 설정 확인
- PostgreSQL 서버의 pg_hba.conf 설정 확인
- 네트워크 연결 상태 확인

#### 마이그레이션 중단
- 프로그램을 다시 실행하면 중단된 지점부터 재개 가능
- 체크포인트가 자동 저장됨

#### 성능 최적화
- 네트워크 대역폭이 충분한 환경에서 실행
- 소스/대상 서버의 리소스 모니터링
- 필요시 PostgreSQL 설정 조정 (work_mem, maintenance_work_mem)

## 주의사항

- **데이터 무결성**: 마이그레이션 전 백업 권장
- **중복 방지**: 대상 테이블에 기존 데이터가 있으면 확인 메시지 표시
- **리소스 사용**: 대용량 데이터 처리 시 네트워크 및 디스크 I/O 부하 고려
- **시간대**: 모든 시간은 서버 시간대 기준

## 기술 스택

- **언어**: Python 3.9+
- **GUI**: PySide6 (Qt6)
- **데이터베이스**: psycopg3 (PostgreSQL 어댑터)
- **테마**: QDarkStyle
- **빌드**: PyInstaller

## 라이선스

MIT License - 자세한 내용은 [LICENSE](LICENSE) 파일 참조

## 기여하기

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 문의 및 지원

- 이슈 트래커: [GitHub Issues](https://github.com/isinthesky/psql93_mig_tool/issues)
- 이메일: [your-email@example.com]

## 업데이트 내역

### v1.0.0 (2024-01-07)
- 초기 릴리스
- COPY 기반 고성능 마이그레이션 엔진
- GUI 인터페이스
- 체크포인트 및 재개 기능

---

**참고**: 이 도구는 PostgreSQL 9.3의 트리거 기반 파티션 테이블을 위해 특별히 설계되었습니다. 
다른 파티션 구조의 경우 수정이 필요할 수 있습니다.