# 02. History/Checkpoint Repository 리팩토링

## 요약
- `src/models/history.py:70-204`의 `HistoryManager`와 `CheckpointManager`는 SQLAlchemy 세션 스코프, CRUD 패턴, 예외 처리 로직을 거의 동일하게 반복한다.
- 세션 스코프가 각 메서드 안에서 직접 관리되기 때문에 테스트에서 in-memory DB나 mock 세션을 주입하기 어렵고, 신규 엔티티가 추가될 때마다 동일한 코드를 복사해야 하는 문제가 있다.
- 목표는 **공통 베이스 리포지토리**를 도입하고, 매니저는 비즈니스 규칙에 집중하도록 분리하는 것이다.

## 현재 Pain Point
- **세션 스코프 중복**: `with self.db.session_scope()` 블록이 모든 메서드에 등장. 세션 커스터마이징(예: read-only, autocommit)이 사실상 불가.
- **트랜잭션 가시성 부족**: 예외 처리 로깅이 자동화되어 있지 않아 오류 상황 분석이 어렵다.
- **테스트 비용 증가**: 매번 실제 SQLite 파일을 생성해야 하고, 세션 주입을 위한 별도 코드가 필요.
- **확장성 한계**: 향후 `MigrationResult`, `RetryQueue` 등의 테이블이 추가되면 동일한 CRUD 패턴을 또 복사해야 함.

## 목표 아키텍처
```
src/database/
├── local_db.py          # SQLAlchemy 모델, 세션 팩토리
├── repository.py        # BaseRepository + 구체 리포지토리

src/models/
└── history.py           # Manager는 리포지토리 활용
```
- `BaseRepository`는 공통 CRUD, 세션 헬퍼, 로깅 훅을 제공.
- `HistoryRepository`, `CheckpointRepository`는 전용 쿼리(정렬, 상태 필터)를 정의.
- 매니저는 리포지토리 결과를 DTO (`MigrationHistoryItem`, `CheckpointItem`)로 변환하는 역할만 수행.

## 구현 계획
| 단계 | 작업 | 산출물/검증 포인트 |
|------|------|-------------------|
| 1 | `BaseRepository` 초안 작성 (create/get/update/delete, session helper) | `tests/database/test_repository_base.py` |
| 2 | `HistoryRepository`, `CheckpointRepository` 구현 | 조건부 조회, 정렬 메서드 유효성 테스트 |
| 3 | `HistoryManager`, `CheckpointManager`가 리포지토리를 주입받도록 리팩토링 | 시그니처 유지, 기존 테스트 통과 |
| 4 | 로깅/예외 래퍼 도입 (선택) | 실패 시 로그 남기고 예외 재전파 |
| 5 | 문서/예제 업데이트 | 새로 도입된 패턴 설명 |

## 완료 기준
- 매니저 클래스에서 더 이상 직접 `session_scope()`를 호출하지 않는다.
- 모든 CRUD 경로가 새 리포지토리를 통해 동작하며, 반환 DTO (`MigrationHistoryItem`, `CheckpointItem`) 구조는 기존과 동일하다.
- 기존 단위 테스트(`tests` 폴더) 및 E2E 플로우가 수정 없이 통과한다.
- 신규 단위 테스트를 통해 `BaseRepository`의 커버리지가 90% 이상 확보된다.

## 위험 & 대응
- **세션 경합**: 리포지토리 내부에서 세션을 새로 여닫는 구조가 서비스 레이어와 충돌할 수 있음 → 필요 시 외부 세션 주입을 허용하는 `with repository.session() as session` 헬퍼 제공.
- **Implicit flush**: `session.merge` 사용 시 이전 동작과 다른 flush 타이밍이 발생할 수 있음 → 명시적으로 `session.flush()` 호출 및 테스트로 검증.
- **DTO 부조화**: 리포지토리 반환값이 SQLAlchemy 모델이므로 DTO 변환이 누락될 위험 → 매니저에서 DTO 변환을 강제하고 타입 힌트로 체크.

## 테스트 전략
- **단위 테스트**
  - `BaseRepository` create/get/update/delete 시나리오.
  - 실패 케이스(존재하지 않는 ID 업데이트, unique 제약 등) 처리 확인.
- **통합 테스트**
  - 기존 `HistoryManager` 플로우(생성 → 상태 업데이트 → 완료) 리팩토링 후에도 동일한 데이터를 반환하는지 비교.
  - 체크포인트 재개 시나리오(미완료 체크포인트 조회) 회귀 테스트.
- **회귀 테스트 체크리스트**
  - 마이그레이션 실행 후 UI에서 이력 목록이 정상 표시되는지.
  - 실패 이력이 있을 때 재시작 기능이 정상 동작하는지.

## 롤아웃/롤백 노트
- 리팩토링은 한 번에 적용하되, `HistoryManager`와 `CheckpointManager`를 각각 다른 커밋으로 분리해 문제 발생 시 부분 롤백이 가능하게 한다.
- 신규 리포지토리를 사용하지 않는 경로가 남아있지 않은지 PR 단계에서 grep/단위 테스트로 확인한다.
- 다른 모듈(예: UI, 워커)이 리포지토리를 직접 사용하도록 확장할 계획이 있다면 추후 follow-up 문서를 추가한다.

## 후속 과제 제안
- `ProfileManager` 또한 유사한 CRUD 패턴을 반복하므로 동일한 베이스 리포지토리로 통합 고려.
- 리포지토리에 감사(audit) 로그 후킹 지점 추가해서 CRUD 이벤트 모니터링 용이하도록 확장.
