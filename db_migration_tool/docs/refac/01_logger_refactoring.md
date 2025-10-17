# 01. 로거 중복 제거 계획

## 요약
- `src/utils/logger.py:11-82`와 `src/utils/enhanced_logger.py:28-229`가 모두 `logging.Logger` 설정, 파일 핸들러 생성, 로그 포맷 정의를 중복 수행하고 있음.
- 확장 로거가 기본 로거의 인스턴스를 소유하면서도 별도의 핸들러를 생성하기 때문에 설정 불일치와 핸들러 중복 등록 문제를 유발.
- 로그 파일/DB 저장 경로 계산 역시 여러 곳에서 반복되어 운영 환경별 정책 변경이 어려움.

## 현재 Pain Point
- **핸들러 중복 등록**: `EnhancedLogger` 초기화 시 기본 로거에 이미 존재하는 핸들러가 중첩되어 로그가 2회 이상 기록되는 사례가 Report됨.
- **포맷 일관성 저하**: 로그 포맷을 변경하려면 두 파일을 동시에 수정해야 하고, 실수로 일부만 반영되는 경우가 있음.
- **테스트 어려움**: 단위 테스트에서 파일 시스템 접근을 가짜로 만들기 어렵고, DB 큐 스레드가 테스트 종료 시까지 살아있어 리소스 누수가 발생.
- **경로 정책 분산**: `QStandardPaths` 호출이 로거/로컬 DB 등 여러 곳에 흩어져 있어 macOS 샌드박스, Windows UAC 등의 요구사항을 반영하기 힘듦.

## 목표 아키텍처
```
src/utils/
├── app_paths.py          # 경로 관리 (공용)
├── logger_config.py      # 로거 설정/핸들러 팩토리
├── logger_mixins.py      # 민감정보 마스킹 & DB 큐 처리
├── logger.py             # MigrationLogger (경량 래퍼)
└── enhanced_logger.py    # EnhancedLogger (믹스인 조합)
```
- `MigrationLogger`는 `LoggerConfig`가 반환한 핸들러들을 사용해 단일 로거 인스턴스를 초기화.
- `EnhancedLogger`는 `MigrationLogger` 인스턴스를 주입받고, 민감정보 마스킹과 비동기 DB 적재를 믹스인으로 조합.
- 경로 관련 코드는 전부 `AppPaths` 유틸을 통해 획득하여 플랫폼별 정책을 한 곳에서 변경 가능하게 함.

## 구현 계획
| 단계 | 작업 내용 | 산출물/체크포인트 |
|------|-----------|------------------|
| 1 | `AppPaths`, `LoggerConfig`, `LoggerMixins` 초안 작성 (테스트 포함) | 새 파일 3개, pytest |
| 2 | `MigrationLogger` 리팩토링 (`LoggerConfig` 사용) | 기존 테스트 통과, 로그 포맷 변경 없음 |
| 3 | `EnhancedLogger` 리팩토링 (믹스인 연결, SUCCESS 레벨 유지) | DB 큐 정상 작동, 세션 ID 관리 동일 |
| 4 | 통합 확인 (`log_emitter`, `BaseMigrationWorker`) | UI 로그, 파일 로그, DB 로그 모두 정상 |
| 5 | 문서/릴리즈 노트 업데이트 | 사용자 영향 없음으로 공지 |

## 완료 기준 (Acceptance Criteria)
- 로그 파일 경로가 기존과 동일하게 생성된다 (`~/…/logs/migration_YYYYMMDD.log`).
- 기존 API (`logger.debug`, `enhanced_logger.success` 등)의 시그니처와 동작이 바뀌지 않는다.
- 다중 초기화 시 핸들러가 중복 추가되지 않는다 (pytest에서 assert len(logger.handlers) == 1).
- 민감정보 마스킹 패턴이 이전과 동일하거나 강화되어 있다.
- DB 로그 큐가 종료 시 안전하게 join되어 리소스 경고가 발생하지 않는다.

## 위험 & 대응
- **핸들러 누락**: 콘솔 핸들러 도입 시 빠뜨릴 수 있음 → `LoggerConfig.setup_logger`에 필수 핸들러 리스트를 전달하고 테스트에서 검증.
- **스레드 누수**: 믹스인 리팩토링으로 `close()` 호출이 누락될 가능성 → context manager 기반 유틸 추가 검토, 종료 테스트 추가.
- **성능 저하**: 마스킹/DB 큐에서 불필요한 호출 증가 → 배치 크기와 슬립 간격을 기존과 동일하게 유지하고, 필요 시 설정값으로 외부화.
- **경로 의존성**: `QStandardPaths`가 반환하는 경로가 환경마다 달라지는 문제 → `AppPaths.set_custom_root()`를 도입해 테스트 환경에서 경로 주입.

## 테스트 전략
- **단위 테스트**
  - `tests/utils/test_logger_config.py`: 핸들러 생성, 포맷, 파일 경로 검증.
  - `tests/utils/test_logger_mixins.py`: 마스킹 패턴, 세션 ID 포맷, 큐 플러시 테스트.
- **통합 테스트**
  - 기존 `tests` 폴더에 로그 발생 후 파일/DB 두 곳에 기록되는지 확인하는 시나리오 추가.
  - `pytest`에서 `capsys` 또는 `tmp_path`를 활용해 로그 파일 내용을 검증.
- **수동 검증 체크리스트**
  - UI에서 마이그레이션 실행 → 로그 뷰어, 로그 파일, SQLite `logs` 테이블 모두에서 동일한 메시지 확인.
  - 민감정보 포함 로그 발생 → 마스킹 적용 여부 확인.

## 배포/롤백 노트
- 롤백 시에는 `logger.py`와 `enhanced_logger.py`를 이전 버전으로 되돌리고 신규 유틸리티 파일들을 제거하면 된다.
- 기능 토글이 없으므로 배포 시점에 QA 완료 후 바로 릴리즈하는 것이 바람직하다.
- 추후 콘솔 핸들러, JSON 로그 등 추가 요구사항이 들어오면 `LoggerConfig`에 새로운 핸들러 팩토리 메서드를 추가하는 방식으로 확장한다.
