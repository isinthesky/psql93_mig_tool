# 04. 애플리케이션 경로 중앙집중화

## 요약
- `src/utils/logger.py`와 `src/database/local_db.py`는 모두 `QStandardPaths.AppDataLocation`을 직접 호출해 로그/DB 경로를 생성한다.
- 디렉토리 생성 시점이 모듈마다 달라 경로 정책을 변경하거나 테스트 경로를 주입하기 어렵고, 플랫폼별 특수 처리가 분산되어 있다.
- `AppPaths` 유틸리티를 도입해 모든 경로 계산을 한 곳에서 수행하고, 테스트/운영 환경별 커스터마이징을 쉽게 만드는 것이 목표다.

## 현재 Pain Point
- **경로 로직 중복**: 로그 디렉토리, DB 파일 경로 등을 구하는 코드가 여러 파일에 복사돼 있어 수정 시 동기화가 필요.
- **디렉토리 생성 타이밍 불일치**: 어떤 모듈은 경로 반환 시 디렉토리를 생성하고, 어떤 모듈은 호출자가 생성하도록 기대해 예외 발생 가능.
- **테스트 불편**: 테스트에서 임시 경로를 사용하려면 환경 변수를 조작하거나 monkeypatch를 해야 해서 복잡.
- **플랫폼 제약 대응 어려움**: macOS 샌드박스, Windows UAC 등의 요구사항을 반영하려면 모든 모듈을 수정해야 함.

## 목표 아키텍처
```
src/utils/
└── app_paths.py         # 로그, DB, 임시 파일 등 경로 계산

주요 사용처:
- src/utils/logger.py
- src/utils/enhanced_logger.py
- src/database/local_db.py
```
- 싱글톤 스타일의 `AppPaths` 클래스가 경로를 캐싱하고 필요 시 디렉토리를 생성.
- 테스트에서 `AppPaths.set_custom_root(Path)`를 호출해 임시 디렉토리로 전환.
- 편의 함수(`get_logs_dir`, `get_db_path`) 제공으로 호출 코드를 단순화.

## 구현 계획
| 단계 | 작업 | 산출물/검증 포인트 |
|------|------|-------------------|
| 1 | `app_paths.py` 추가 (`AppPaths`, 편의 함수) | `tests/utils/test_app_paths.py` |
| 2 | `logger.py`, `enhanced_logger.py`가 `_get_log_dir` 대신 `AppPaths` 사용 | 로그 파일 경로 동일 유지 |
| 3 | `local_db.py`가 `_get_db_path` 대신 `AppPaths` 사용 | SQLite 파일 경로 동일 유지 |
| 4 | 테스트에서 커스텀 루트 주입/리셋 기능 검증 | tmp_path 사용 |

## 완료 기준
- 로그 파일과 SQLite DB 파일이 기존과 동일한 위치에 생성된다.
- 운영 코드에서 `QStandardPaths`를 직접 호출하는 구문이 제거되고, 모든 경로 계산이 `AppPaths`를 통해 이루어진다.
- 테스트에서 `AppPaths.set_custom_root()`를 사용해 임시 디렉토리를 지정할 수 있으며, 테스트 종료 후 원복된다.

## 위험 & 대응
- **경로 캐시 오염**: 커스텀 루트 설정 후 원복하지 않으면 다른 테스트에 영향 → `pytest` fixture에서 `try/finally`로 `AppPaths.set_custom_root(None)` 적용.
- **플랫폼 차이**: 일부 플랫폼에서 `QStandardPaths`가 빈 문자열을 반환할 수 있음 → fallback으로 `Path.home()`를 사용하고 로그를 남김.
- **권한 문제**: 디렉토리 생성 권한이 없을 경우 초기화 실패 → 예외 발생 시 사용자에게 친절한 메시지를 띄우고 수동 경로 지정 옵션 제공.

## 테스트 전략
- **단위 테스트**
  - 기본 경로가 실제 파일 시스템상 유효한지 검증.
  - 커스텀 루트 설정/해제 시 경로 캐시가 재생성되는지 확인.
  - 로그/DB/임시 디렉토리가 존재하지 않을 때 자동 생성되는지 테스트.
- **통합 테스트**
  - 로거 초기화 및 로컬 DB 초기화 흐름에서 `AppPaths` 호출이 정상 동작하는지 확인.
  - macOS/Windows CI 환경이 있다면 smoke 테스트를 추가.

## 롤아웃/롤백 노트
- `AppPaths` 도입 후 기존 `_get_log_dir`, `_get_db_path` 메서드를 제거하므로 PR에서 호출 경로가 모두 교체되었는지 확인.
- 문제 발생 시 `AppPaths` 도입 커밋을 되돌리고 기존 메서드를 복원하면 롤백 가능.
- 추후 빌드 아티팩트나 캐시 경로가 추가되면 `AppPaths`에 헬퍼를 추가하고, 관련 문서/테스트를 갱신한다.
