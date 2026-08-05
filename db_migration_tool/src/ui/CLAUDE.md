# UI 프리젠테이션 레이어

- **문서 경로**: `src/ui/CLAUDE.md`
- **레이어**: 프리젠테이션 (PySide6 UI)
- **역할**: 데스크톱 UI 구성요소의 책임과 코어 레이어와의 상호작용을 정리합니다.

## 책임 범위
- 사용자에게 연결 프로필, 마이그레이션 진행 상황, 로그를 시각적으로 제공합니다.
- PySide6 시그널/슬롯을 통해 코어 워커와 비동기 통신을 조율합니다.
- 다이얼로그를 통해 입력 검증과 단계별 설정을 지원합니다.

## 주요 구성요소
- `main_window.py`: 애플리케이션 메인 창. 프로필 목록, 이력 테이블, 툴바를 구성하고 사용자 이벤트를 처리합니다.
- `theme.py`: 색·서체·간격 디자인 토큰과 전역 QSS를 만듭니다. `main.py`가 qdarkstyle 위에 덧씌웁니다.
- `dialogs/connection_dialog.py`: 소스/대상 엔드포인트 연결 정보를 입력받고 탭 안에서 바로 검증합니다.
- `dialogs/migration_wizard_dialog.py`: PostgreSQL → PostgreSQL COPY 마이그레이션 3단계 마법사입니다.
- `dialogs/file_archive_migration_dialog.py`: PostgreSQL ↔ File Archive 마이그레이션 3단계 마법사입니다.
- `dialogs/history_dialog.py`: 작업 이력을 조회합니다(modeless 싱글톤).
- `dialogs/log_viewer_dialog.py`: 실시간 로그와 이력을 확인하는 창을 제공합니다.
- `dialogs/scan_host.py`: 두 마법사가 공유하는 조회(scan) 골격 — `ScanHostMixin`(세대·수명·재진입),
  `format_row_count()`(추정치 표기), `PARTITION_DISPLAY_LIMIT`, `build_log_retention_hint()`.
- `widgets/instruments.py`: 두 마법사가 공유하는 계기 위젯 — `StatusLamp`(상태 램프), `StepRail`(단계 표시), `MetricReadout`(속도/ETA 등 수치).

## 조회(scan) 규칙
탐색·대상 확인·검증은 DB를 오래 붙잡으므로 반드시 `src/core/scan_workers.py`의 워커로 돌리고,
`ScanHostMixin`을 통해 관리합니다. 직접 동기 호출하거나 `QApplication.processEvents()`를 쓰지 않습니다.

- **세대**: 조회 조건(날짜·항목)이 바뀌면 `_bump_generation()`을 부릅니다. 결과 시그널의 첫 인자가
  세대이므로 늦게 도착한 결과는 `_is_current_generation(gen)`으로 걸러 버립니다.
  늦은 결과를 버리는 것만으로는 부족합니다 — `_on_scan_generation_changed()`에서 **이미 그려진
  목록도 비웁니다.** 남아 있으면 사용자가 새 조건의 결과로 믿습니다.
- **수명**: 실행 중인 `QThread`의 마지막 파이썬 참조가 사라지면 **프로세스가 죽습니다.** 워커를
  `_scan_workers[kind]`에 넣고, 닫기 경로에서 `_prepare_close()`를 부릅니다. `terminate()`는
  쓰지 않습니다(psycopg 커넥션과 파일 락이 남습니다).
- **재진입**: 시작 전 `_is_scan_inflight(kind)`로 막습니다. `isRunning()`만으로는 `run()`이 끝나고
  결과 슬롯이 아직 안 돈 구간을 놓칩니다.
- 워커 시그널은 **람다로 연결하지 않습니다**. 수신자 QObject가 없어 다이얼로그 파괴 시 자동 해제되지 않습니다.

## 숫자 표기 규칙
PostgreSQL 소스의 행 수는 플래너 통계 기반 **추정치**입니다. `format_row_count(count, estimated)`로
표기해 `약 N rows` / `행 수 미상`(통계 없음)을 구분합니다. 0을 `0 rows`로 쓰면 사용자가 빈 파티션으로
읽고 체크를 풉니다. 아카이브 매니페스트의 행 수는 내보낼 때 실측한 값이라 그대로 씁니다.

## 스타일 규칙
- 위젯에서 `setStyleSheet`으로 색을 직접 지정하지 않습니다. `theme.py`의 토큰을 objectName 또는
  `role`/`state` 동적 프로퍼티로 받습니다. (`label.setProperty("role", "hint")`)
- 동적 프로퍼티를 런타임에 바꾸면 `theme.repolish(widget)`을 호출해야 반영됩니다.
- 램프 상태는 `idle`(대기) / `busy`(확인 중·일시정지) / `ok`(정상·실행 중·완료) / `error`(오류·중단) 4가지뿐입니다.
- 한 화면에서 채워진(강조) 버튼은 하나만 둡니다. 나머지는 기본 또는 `variant="chip"`을 씁니다.
- 실행 페이지의 버튼 활성화는 각 핸들러가 아니라 `_set_run_state()` 한 곳에서만 정합니다.

## 실행 상태 전파
두 마법사는 실행 여부가 바뀔 때 `migration_running_changed(bool)`를 발행하고, `MainWindow`가
`set_migration_running()`으로 받아 트레이에 전달합니다. 트레이는 이 값으로 실행 중 아이콘을
바꾸고 종료 시 되묻습니다.

- 발행은 **`_set_run_state()` 안에서만** 합니다. 각 핸들러에서 따로 알리면 한 경로만 빠져도
  트레이가 계속 '실행 중'으로 남습니다.
- 다이얼로그는 트레이를 직접 알지 않습니다. 직접 알면 창을 띄우는 쪽마다 배선을 다시 해야 합니다.
- `MainWindow.start_migration()`은 `exec()`가 어떻게 끝나든 `finally`에서 실행 표시를 내립니다.

## 이벤트 흐름
1. `MainWindow`가 `ProfileManager`, `HistoryManager`를 이용해 초기 데이터를 채웁니다.
2. 사용자가 버튼을 누르면 대응하는 다이얼로그가 열리고, 입력이 완료되면 코어 워커 실행 시그널을 발행합니다.
3. 워커에서 발생한 진행률/로그 시그널을 받아 UI 요소를 업데이트하고, 사용자 조작(일시정지, 재개)을 워커에 전달합니다.

## 협력 모듈
- `src/models`: UI가 표시하는 데이터 모델과 CRUD 동작을 제공합니다.
- `src/core`: 마이그레이션 워커가 시그널을 통해 UI로 결과를 전달합니다.
- `src/utils.enhanced_logger`: UI 로그 창에 출력할 메시지 포맷팅을 담당합니다.
