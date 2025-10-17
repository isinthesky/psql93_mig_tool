# 03. Connection Dialog 중복 제거 계획

## 요약
- `src/ui/dialogs/connection_dialog.py`는 같은 UI 입력을 세 번(`get_profile_data`, `test_connection`, `accept`) 서로 다른 딕셔너리 구조로 변환한다.
- 필드가 추가되거나 키 이름이 바뀔 때 누락이 잦고, `dbname` vs `database`처럼 psycopg 파라미터와 저장용 파라미터가 혼재되어 유지보수 비용이 높다.
- 목표는 **단일 매핑 계층**을 도입해 UI ↔ 도메인 ↔ psycopg 변환을 명확히 분리하는 것이다.

## 현재 Pain Point
- **중복 변환**: 동일한 위젯 값을 세 번 읽어 다른 딕셔너리를 생성. 포트 기본값, SSL 설정 처리 방식도 제각각.
- **검증/저장 분리**: `accept()`에서 검증용 딕셔너리를 직접 구성하다 보니 저장용 딕셔너리와 불일치가 발생.
- **테스트 난이도**: 현재 구조에서는 위젯을 직접 초기화해 모든 경로를 테스트해야 하므로 케이스가 늘어날수록 테스트 코드가 폭증.
- **키 네이밍 혼동**: psycopg는 `dbname`, `user`, `password`를 기대하지만 저장용 딕셔너리는 `database`, `username`, `password`를 사용. 변환 누락 시 연결 테스트만 실패하거나 저장만 실패하는 문제가 생김.

## 목표 아키텍처
```
src/ui/dialogs/
├── connection_dialog.py       # UI 로직, 매퍼 호출
└── connection_mapper.py       # UI ↔ Dict 변환 책임
```
- `ConnectionWidgetSet`: 위젯 레퍼런스를 묶어 타입 안전성 확보.
- `ConnectionMapper`: 프로필 저장용, psycopg 연결용, 검증용 딕셔너리 생성을 담당하는 정적 메서드 제공.
- UI 로직은 매퍼와 위젯 세트의 결과만 사용하여 중복을 제거.

## 구현 계획
| 단계 | 작업 | 산출물/검증 포인트 |
|------|------|-------------------|
| 1 | `connection_mapper.py` 초안 작성 (`ConnectionMapper`, `ConnectionWidgetSet`) | `tests/ui/dialogs/test_connection_mapper.py` |
| 2 | `connection_dialog.py` 리팩토링 (위젯 생성 → 위젯 세트 반환, 매퍼 사용) | 기존 시그니처 유지, UI 동작 확인 |
| 3 | 검증 로직 통일 (`accept`, `test_connection`, `get_profile_data`) | 중복 코드 삭제, 딕셔너리 구조 일치 |
| 4 | UI 테스트/회귀 테스트 | 프로필 저장·편집·연결 테스트 정상 통과 |

## 완료 기준
- UI ↔ Dict 변환 로직은 `connection_mapper.py` 한 곳에서만 관리된다.
- 기존 공개 메서드(`get_profile_data`, `test_connection`, `accept`)의 리턴값/동작이 동일하다.
- 새 필드를 추가할 때 `ConnectionWidgetSet`과 `ConnectionMapper`만 수정하면 모든 경로가 업데이트된다.
- 자동화된 Qt 테스트(`pytest-qt`)로 프로필 생성/편집 플로우가 통과한다.

## 위험 & 대응
- **매퍼 누락**: 새로운 위젯 필드가 추가되었을 때 매퍼 업데이트를 잊을 수 있음 → PR 템플릿에 “매퍼 업데이트 여부” 체크를 추가.
- **psycopg 파라미터 오류**: 키 이름을 잘못 매핑할 수 있음 → 매퍼 단위 테스트에서 psycopg `connect` 호출을 patch 하여 전달 인자를 검증.
- **UI 회귀**: 탭 구성/위젯 연결이 깨질 수 있음 → `ConnectionWidgetSet` 초기화 시 타입 힌트와 어서션으로 방지.

## 테스트 전략
- **단위 테스트**
  - `ConnectionMapper.ui_to_profile_config` 입력/출력 검증.
  - `ui_to_psycopg_config`에서 SSL 체크 시 `sslmode='require'` 설정 확인.
  - `ConnectionWidgetSet`이 UI 위젯을 정확히 업데이트하는지 테스트.
- **통합 테스트 (`pytest-qt`)**
  - 프로필 생성 → 저장된 딕셔너리 확인.
  - 기존 프로필 로드 → UI 값이 DTO와 일치하는지 확인.
  - 연결 테스트 버튼 클릭 → psycopg 연결 함수가 예상 딕셔너리로 호출되는지 mocking.
- **수동 체크리스트**
  - SSL 체크박스 토글 시 저장/테스트 모두 동일하게 반영되는지.
  - 프로필 이름 미입력, 필수 필드 누락 등의 검증 메시지가 기존과 동일한지.

## 롤아웃/롤백 노트
- 리팩토링은 UI 코드이므로 빠른 회귀 확인을 위해 QA에서 주요 경로(프로필 생성/삭제/편집/테스트)를 수행해야 한다.
- 문제 발생 시 `connection_mapper.py` 삭제 및 `connection_dialog.py` 복원으로 즉시 롤백 가능하다.
- 장기적으로는 다른 다이얼로그(`migration_dialog.py`)에도 동일 패턴을 도입해 UI 일관성을 확보하는 것을 권장한다.
