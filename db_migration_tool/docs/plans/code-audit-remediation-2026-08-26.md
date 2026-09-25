# 전체 코드 감사 및 보완 계획

- 작성일: 2026-08-26
- 감사 기준: `main` / `3813ac3` / 애플리케이션 1.2.5
- 범위: `src`, `tests`, `tools`, PyInstaller 설정, Inno Setup 설치 프로그램, 빌드 스크립트
- 현재 상태: 보완 구현 main 통합(`b6dd474`) — 항목별 상태는 §8 종합 상태표. 재감사 전이라 운영 승인은 보류

## 1. 결론

독립 검토자 3명이 데이터 정합성·동시성/UI 상태·보안/배포 영역을 나누어 검토했고, 각 검토자가 다른 영역의 주요 판정을 다시 확인했다. 새 이슈가 발견될 때마다 확정 목록에 편입한 뒤 다시 포화 검토했으며, 마지막 두 독립 검토에서 **추가로 분류되지 않은 Critical/High/Medium 이슈는 0건**이었다.

코드를 수정하지 말라는 조건에 따라 확정 이슈 자체는 아직 남아 있다.

| 등급 | 확정 건수 | 현재 의미 |
|---|---:|---|
| Critical | 2 | 정상 입력 또는 정상 부하에서도 조용한 데이터 누락 가능 |
| High | 9 | 결과 오판, 대상 분산, 스냅샷 불일치, 보안 통제 우회 가능 |
| Medium | 14 | 복구성·무결성·배포 신뢰·운영 안정성 저하 |
| 미분류 신규 C/H/M | 0 | 마지막 반복에서 추가 발견 없음 |

따라서 현재 릴리스는 데이터 이관용 운영 배포에 적합하다고 승인할 수 없다. 아래 계획을 구현하고 모든 종료 조건을 통과한 뒤 다시 독립 감사를 수행해야 실제 잔여 C/H/M 0건을 선언할 수 있다.

## 2. 검증 기준과 실행 결과

### 2.1 자동 검증 기준선

| 검증 | 결과 |
|---|---|
| `python -m pytest -q` | 570 passed, 1 skipped |
| `ruff check src tests tools` | 통과 |
| `mypy src` | 통과 |
| 전체 테스트 커버리지 | 65.01% |

핵심 실행 경로의 커버리지가 낮다.

| 모듈 | 커버리지 |
|---|---:|
| `copy_migration_worker.py` | 16.69% |
| `table_creator.py` | 24.36% |
| `migration_worker.py` | 30.88% |
| `file_archive_workers.py` | 50.69% |

실제 PostgreSQL을 사용하는 통합 테스트 1건도 현재 건너뛰어진다. 기존 단위 테스트 통과는 아래 트랜잭션·COPY 스트리밍·종료 수명주기 결함을 검출하지 못한다.

### 2.2 직접 재현한 핵심 결함

1. 크기 1인 COPY 큐를 채운 뒤 `close()`하면 큐에 데이터가 남아 있어도 다음 `read()`가 빈 문자열을 반환했다.
2. PostgreSQL CSV의 따옴표 내부 줄바꿈을 포함한 유효 입력에서 물리 줄 수가 행 수로 계산되고, 다음 배치 기준 키가 데이터 열 값으로 바뀌었다.
3. `installer/build_installer.bat`는 현재 인코딩/줄바꿈 조합에서 `cmd.exe`가 명령을 깨진 문자열로 해석해 종료 코드 1로 실패했다.
4. 현재 배포 EXE와 설치 프로그램은 Windows Authenticode 서명이 없었다.

## 3. 확정 이슈 목록

### 3.1 Critical

| ID | 문제와 근거 | 보완 방향 | 완료 판정 |
|---|---|---|---|
| C-01 | `CopyStreamBuffer.close()`가 큐 포화 시 취소 상태를 설정하고 `read()`가 남은 데이터를 비우지 않고 EOF를 반환한다. 대상에는 앞부분만 반영되지만 생산자 행 수와 마지막 키로 checkpoint가 전진할 수 있다. `src/core/copy_migration_worker.py:87-145,803-857` | EOF와 취소를 별도 상태로 분리하고, 정상 종료에서는 큐를 끝까지 소비하도록 backpressure-safe 프로토콜을 사용한다. 대상 반영 행 수와 checkpoint 전진을 같은 불변식으로 묶는다. | 작은 큐·느린 소비자·반복 실행에서 모든 바이트와 행이 보존되고, 대상 commit 이후의 실제 마지막 키만 checkpoint에 기록됨 |
| C-02 | COPY payload를 `split("\n")`, `split(",")`로 해석해 CSV 따옴표 내부 쉼표·줄바꿈·escaped quote·chunk boundary를 처리하지 못한다. 유효한 문자열 열 때문에 잘못된 재개 키가 기록되어 다음 범위를 건너뛸 수 있다. `src/core/copy_migration_worker.py:147-186,728-869` | payload에서 키를 재파싱하지 않는다. 같은 source transaction에서 마지막 PK를 별도 조회하거나, PostgreSQL CSV 규칙과 chunk 경계를 보존하는 증분 parser를 사용한다. | quoted comma/newline/quote, CRLF, multibyte 경계, 임의 chunk 크기 property test와 실제 PostgreSQL 재개 테스트에서 중복·누락 0 |

### 3.2 High

| ID | 문제와 근거 | 보완 방향 | 완료 판정 |
|---|---|---|---|
| H-01 | `skip_on_error` 경로가 실패 checkpoint를 남기고 예외를 삼킨 뒤 UI에서 history를 `completed`로 바꾼다. 재개 조회는 `running`/`failed`만 찾으므로 실패 작업이 자동 재개에서 사라진다. `copy_migration_worker.py:338-360`, `migration_wizard_dialog.py:1824-1892`, `repository.py:201-218` | 성공/부분 성공/실패를 명시적 결과 객체로 반환하고, 실패 checkpoint가 하나라도 있으면 history를 `partial` 또는 `failed`로 유지한다. | 오류 주입 후 완료 알림이 나오지 않고, 재실행 시 실패 작업이 정확히 다시 선택됨 |
| H-02 | Python COPY의 작업 취소가 실행 중인 COPY/commit과 연결 수립을 중단하지 못한다. 기본 `stop()`은 플래그만 바꾸며 연결 전에는 취소할 참조도 없다. `base_migration_worker.py:125-135`, `copy_migration_worker.py:383-398,805-820,1005-1023` | 모든 연결에 timeout을 적용하고, worker별 connection cancel/rollback과 생산자 종료를 구현한다. commit 경계와 취소 결과를 명확히 정의한다. | connect/COPY/commit 각 단계 취소가 제한 시간 안에 끝나고 thread·connection·producer가 남지 않음 |
| H-03 | Python COPY가 기본 READ COMMITTED의 여러 statement snapshot을 사용한다. 작업 중 source 변경을 막지 않아 단일 시점에 존재하지 않은 혼합 결과나 지난 키 범위 누락이 가능하다. `copy_migration_worker.py:377-399,727-820` | 전체 계획에 일관된 snapshot을 사용하거나, source 쓰기 중단을 강제·검증하고 UI에 전제 조건을 표시한다. | 동시 insert/update/delete 통합 테스트에서 선택한 스냅샷과 대상 결과가 정확히 일치 |
| H-04 | 탐색은 `public`을 지정하지만 COPY, COUNT, TRUNCATE, DDL 일부는 relation을 한정하지 않는다. `search_path` 선행 스키마의 같은 이름 객체를 처리할 수 있다. `partition_discovery.py:157-170`, `table_creator.py:287-303,364-409`, `copy_migration_worker.py` | 모든 identifier를 안전하게 quote하고 schema를 명시한다. 연결 직후 제한된 `search_path`도 설정한다. | `$user` 등 선행 스키마에 동명 객체가 있어도 `public`의 의도한 객체만 조회·변경 |
| H-05 | DB 연결이 `sslmode=require`에 머물러 서버 인증서와 hostname을 검증하지 않는다. | `verify-full`을 기본으로 하고 CA/root certificate와 hostname 설정·검증 UI를 제공한다. 예외 허용은 명시적 위험 승인과 감사 로그를 요구한다. | 잘못된 CA·hostname·중간자 인증서는 연결 실패, 올바른 인증서만 성공 |
| H-06 | 프로필 복호화에 공개된 legacy Fernet 키 fallback이 남아 있고 일부 초기화 경로는 그 키로 새 값을 기록할 수 있다. `src/models/profile.py:103-117,136-165` | legacy 값은 한 번의 인증된 마이그레이션으로 OS 보호 저장소 기반 키로 재암호화하고, 공개 키 fallback과 신규 기록을 제거한다. | legacy fixture 변환 후 공개 상수만으로 프로필 복호화 불가, 신규 설치에 legacy ciphertext 0 |
| H-07 | `check_license()` 예외가 unrestricted 상태로 fail-open 된다. `.activation` I/O 오류 등으로 신규 작업 제한을 우회할 수 있다. `src/main.py:103-115`, `src/ui/main_window.py:374-377` | 예외를 `UNKNOWN_RESTRICTED`로 처리해 조회·복구·기존 작업 재개만 허용하고 새 작업은 차단한다. | 권한 오류·손상 파일·directory 대체·disk error 주입에서 신규 작업이 항상 차단되고 복구 안내 제공 |
| H-08 | history가 mutable `profile_id`만 저장한다. 중단 후 같은 프로필의 source/target 또는 방향을 바꾸면 남은 작업이 현재 endpoint에서 실행되어 하나의 history가 여러 대상에 분산된다. `main_window.py:323-390`, `profile.py:195-212`, `migration_wizard_dialog.py:926-1040,1564-1571` | history에 비밀 제외 endpoint identity, migration mode, schema, plan fingerprint를 불변 저장한다. 재개 시 현재 프로필과 비교하고 identity 변경을 차단한다. | endpoint/mode 변경 후 재개가 거부되고, 비밀번호 교체처럼 identity를 보존하는 변경만 허용 |
| H-09 | history를 먼저 commit하고 checkpoint를 작업마다 별도 commit한다. 중간 실패 시 일부 checkpoint만 남고, 재개는 존재하는 subset만 처리한 뒤 완료될 수 있다. `migration_wizard_dialog.py:1546-1560`, `file_archive_migration_dialog.py:1301-1315`, `models/history.py:106-133,190-195` | history와 전체 checkpoint를 단일 transaction으로 bulk 생성한다. 불변 planned set/count/hash를 저장하고 재개 전에 완전성을 검증한다. | N번째 checkpoint 실패·프로세스 중단·DB lock 주입에서 전부 생성되거나 전부 rollback되며 subset 완료 불가 |

### 3.3 Medium

| ID | 문제와 근거 | 보완 방향 |
|---|---|---|
| M-01 | 분할 데이터 행 수 추정 쿼리 실패를 잡은 뒤 rollback하지 않아 같은 transaction의 이후 탐색도 실패할 수 있다. `partition_discovery.py:274-291` | 추정 조회를 savepoint로 격리하거나 오류 시 rollback 후 다음 작업을 시작한다. |
| M-02 | archive manifest를 lock 안에서 다시 읽지만 오래된 메모리 값으로 최신 디스크 값을 덮을 수 있다. `archive_manifest.py:224-255` | 항목별 version/CAS 또는 최신 디스크 기준 충돌 병합을 사용하고 동시 writer 테스트를 추가한다. |
| M-03 | legacy INSERT worker의 OFFSET pagination은 source 변경과 복합 PK에서 누락·중복 가능성이 있고 0행 반복 종료도 불안정하다. `migration_worker.py:114-203,259-307` | 제거하거나 안정적인 복합 keyset pagination과 snapshot으로 교체한다. |
| M-04 | archive checksum은 선택적이고 manifest 자체에 인증이 없어 신뢰되지 않은 공유·이동 매체에서 파일과 manifest를 함께 변조할 수 있다. `archive_manifest.py:36-56,315-390`, `file_archive_workers.py:811-870` | checksum을 필수화하고 서명 또는 인증된 manifest를 도입하며 신뢰 경계를 문서화한다. |
| M-05 | 현재 프로필 암호화 키와 암호화 DB가 같은 사용자 데이터 디렉터리에 있어 디렉터리 복제만으로 함께 탈취된다. `profile.py:142-158`, `saved_connection.py:20-37`, `app_paths.py:65-124` | Windows DPAPI/Credential Manager 등 사용자·장치 보호 저장소로 key wrapping한다. |
| M-06 | 라이선스 발급 private key를 `NoEncryption()`으로 저장한다. `tools/issue_license.py:63-82` | 암호화 저장, 접근 권한 검증, 키 식별자·rotation·폐기 절차를 적용한다. |
| M-07 | 설치 프로그램의 `/LICENSEKEY`와 입력 필드, 남는 `license.seed`가 평문 노출 경로를 만든다. `installer/DBMigrationTool.iss:86-141`, `licensing/activation.py:63-106` | UI 마스킹, command-line secret 제거 또는 안전한 응답 파일/IPC, seed 최소 수명·삭제를 적용한다. |
| M-08 | lock file이 버전 관리에서 제외되고 의존성이 하한 위주라 빌드 재현성과 공급망 검증이 약하다. | lock file을 추적하고 CI가 lock과 hash를 강제하며 업데이트 절차를 분리한다. |
| M-09 | VC++ prerequisite는 현재 서명이 유효하지만 빌드/설치 단계에서 signer·hash를 검증하는 gate가 없다. | 승인된 Microsoft signer와 고정 hash/버전 검증 후에만 포함·실행한다. |
| M-10 | `installer/build_installer.bat`가 현재 UTF-8/LF 형식에서 `cmd.exe` 파싱에 실패한다. | Windows batch 호환 인코딩/CRLF로 정규화하고 clean VM CI에서 직접 실행한다. |
| M-11 | EXE와 installer에 Authenticode 서명이 없어 배포 출처와 변경 여부를 Windows가 검증하지 못한다. | 보안 저장된 코드서명 인증서와 timestamp를 적용하고 서명 검증을 릴리스 gate로 만든다. |
| M-12 | 미완료 history가 있는 프로필도 삭제할 수 있어 history/checkpoint가 UI에서 재개 불가능한 orphan이 된다. `main_window.py:349-365`, `profile.py:214-220`, `local_db.py:39-68` | 미완료 작업이 있으면 삭제를 차단하거나 명시적 폐기·복구·재연결 절차를 제공한다. |
| M-13 | 실행 중 tray 종료가 worker `stop/cancel/wait` 없이 `app.quit()`을 호출한다. `tray_icon.py:180-199`, `main.py:121-128` | application-level worker registry로 graceful stop, bounded wait, DB·manifest·logger flush 후 종료한다. |
| M-14 | index/CLUSTER DDL 오류를 savepoint/rollback 없이 잡아 PostgreSQL aborted transaction을 계속 사용하고, DDL rollback 뒤 metadata만 남길 수 있다. `table_creator.py:395-414,429-506,742-770` | 무시 가능한 statement는 savepoint로 격리하고 필수 DDL은 즉시 실패시킨다. psycopg2/3 공통 예외 처리를 정리한다. |

## 4. 등급에서 제외하거나 낮춘 항목

다음 항목은 보완 backlog에는 남기되 현재 C/H/M 종료 수에는 포함하지 않는다.

- archive import 파일은 예외 frame이 끝나면 CPython 참조 해제로 닫혀 지속 누수가 입증되지 않았다. context manager 확대는 Low 방어 개선이다.
- 추정 행 수 기반 진행률은 정확도 요구사항에는 못 미치지만 데이터 완료 판정에 사용되지 않고 추정값임을 표시한다. Low 제품 개선으로 분류한다.
- 로거 종료 시 마지막 비동기 DB 로그 batch가 남을 수 있으나 동기 파일 로그는 유지된다. tray 종료 개선에 포함하는 Low 항목이다.
- error signal 직후 QThread 파괴 race와 다른 thread에서의 rollback 후보는 실제 DB 재현 전까지 확정하지 않는다. 실DB 검증 backlog에 둔다.
- 비밀번호 마스킹은 일부 앞글자를 남기지만 현재 그 값을 직접 기록하는 호출 경로가 확인되지 않았다. 호출 차단 테스트와 전체 마스킹을 Low로 수행한다.

## 5. 구현 순서

### 단계 0 — 릴리스 중지와 안전장치

1. C-01/C-02가 해결될 때까지 Python COPY 모드를 운영 배포에서 비활성화한다.
2. 모든 모드의 성공 판정 전에 대상 행 수와 checkpoint 완전성을 별도 검증한다.
3. unsigned installer와 재현 불가능한 의존성으로 새 릴리스를 배포하지 않는다.

완료 조건: 위험 모드가 기본·자동 선택에서 제거되고, 기존 부분 작업의 복구 안내가 마련되어야 한다.

### 단계 1 — 스트리밍과 재개 정합성

대상: C-01, C-02, H-01, H-02, H-08, H-09, M-03

1. COPY stream protocol과 checkpoint 소유권을 재설계한다.
2. history 생성 시 불변 계획과 endpoint fingerprint를 원자적으로 기록한다.
3. 완료 상태를 worker의 명시적 결과와 checkpoint 집합으로만 결정한다.
4. 취소·오류·프로세스 중단 뒤 target을 source of truth로 안전하게 재개한다.

필수 테스트: 작은 큐 stress, CSV property test, 각 commit 경계 crash injection, profile identity 변경, checkpoint N번째 실패, cancel/connect timeout, 복합 키 재개.

### 단계 2 — DB 일관성과 SQL 경계

대상: H-03, H-04, M-01, M-14

1. 일관된 source snapshot 또는 쓰기 중단 계약을 적용한다.
2. 모든 relation을 schema-qualified identifier로 생성한다.
3. 예상 가능한 SQL 실패를 savepoint로 격리하고 원본 오류를 보존한다.
4. DDL과 metadata가 함께 성공하거나 함께 실패하게 만든다.

필수 테스트: 실제 PostgreSQL 9.3 source와 지원 target, concurrent writer, search_path shadow, 권한 부족, duplicate index, CLUSTER 오류, transaction-aborted semantics.

### 단계 3 — 보안과 라이선스 fail-closed

대상: H-05, H-06, H-07, M-04~M-07

1. TLS server identity 검증을 기본값으로 만든다.
2. profile key를 OS 보호 저장소로 이동하고 legacy 키를 폐기한다.
3. 라이선스 I/O/검증 예외를 제한 모드로 처리한다.
4. archive manifest 인증, issuer key 운영, installer secret 전달을 강화한다.

필수 테스트: 잘못된 인증서, legacy migration, key/db 동시 복제, activation 손상·권한 오류, archive+manifest 동시 변조, command-line/설치 로그 secret scan.

### 단계 4 — 종료·복구·동시 writer

대상: M-02, M-12, M-13

1. manifest 동시 저장에 versioned compare-and-swap을 적용한다.
2. 미완료 작업이 있는 profile의 편집·삭제 정책과 복구 UI를 구현한다.
3. 앱 종료 시 모든 worker와 producer를 순서대로 중단하고 자원을 flush한다.

필수 테스트: 두 프로세스 manifest 경쟁, profile 삭제/복원, 각 worker 단계의 tray 종료, timeout 뒤 강제 종료, 임시 파일·lock·thread 누수 검사.

### 단계 5 — 재현 가능한 서명 릴리스

대상: M-08~M-11

1. 의존성 lock과 hash 검증을 CI에 고정한다.
2. prerequisite provenance를 검증한다.
3. batch 빌드를 clean Windows VM에서 실행한다.
4. EXE와 installer를 Authenticode 서명하고 timestamp와 서명 검증 결과를 산출물에 첨부한다.

필수 gate: clean checkout에서 dist와 installer 재빌드 성공, lock drift 0, prerequisite signer/hash 일치, `Get-AuthenticodeSignature` Valid.

## 6. 테스트 및 감사 종료 조건

구현 후 아래 조건을 모두 만족해야 한다.

1. 각 C/H/M 이슈에 실패하는 회귀 테스트가 먼저 추가되고 수정 후 통과한다.
2. `pytest`, `ruff`, `mypy`가 모두 통과한다.
3. 네 핵심 worker 모듈의 변경 분기 커버리지가 90% 이상이고, 전체 line coverage가 80% 이상이다.
4. PostgreSQL 9.3 source와 지원 target을 사용하는 실제 DB matrix에서 COPY, 재개, 취소, 동시 쓰기, DDL 실패를 검증한다.
5. 10회 이상의 느린 소비자/작은 큐 stress와 임의 CSV chunk property test에서 행 hash·개수·마지막 키가 모두 일치한다.
6. history의 planned set, checkpoint set, 대상 반영 결과가 모든 종료 상태에서 일치한다.
7. clean Windows VM에서 signed dist/installer를 재현하고 설치·업그레이드·제거 smoke test를 통과한다.
8. 수정 담당자와 독립된 두 검토자가 전체 목록을 교차검증한다.
9. 새 이슈 발견 시 목록에 편입하고 다시 포화 검토한다. 연속된 마지막 독립 검토에서 추가 신규 C/H/M이 0건이어야 한다.
10. 확정 C/H/M 각각의 상태가 `fixed + verified`여야 한다. 단순 위험 수용이나 문서화만으로 0건 처리하지 않는다.

## 7. 권장 작업 단위

서로 다른 원인을 한 변경에 섞지 않고 아래 순서로 작은 변경 묶음을 만든다.

1. COPY queue/EOF와 CSV anchor
2. 원자적 history 계획과 결과 상태
3. endpoint fingerprint와 profile 수명주기
4. cancellation과 application shutdown
5. snapshot, schema qualification, DDL transaction
6. TLS, secret storage, license fail-closed
7. archive authenticity와 concurrent manifest
8. dependency lock, prerequisite 검증, installer encoding, signing
9. 실제 DB·Windows 릴리스 matrix와 최종 독립 재감사

각 작업 단위는 문제 재현 테스트, 최소 수정, 회귀 테스트, 운영 복구 절차를 함께 제출해야 한다.

## 8. 진행 현황 (2026-09-26)

### 종합 상태표 (main `b340b42`, 1.2.8 이후 리뷰 major 통합 — §8.7)

상태 구분
- **해결**: 코드 수정과 회귀 테스트가 main에 들어갔다. 괄호 안은 알려진 한계다.
- **부분**: §3 완료 판정 중 일부를 아직 충족하지 못했다.
- **보류**: 외부 조건 때문에 착수할 수 없다. 사유를 적는다.

집계: 해결 22 · 부분 2(H-01, H-03) · 보류 1(M-11). 이전 리뷰가 남긴 major(H-06 키 갈림, H-08 다중 유형 legacy, H-09 아카이브 보충분)는 §8.7에서 모두 해소했고, 남은 리뷰 지적은 minor뿐이다. §6 종료 조건(재감사, clean VM 서명 릴리스, PG 9.3 실DB matrix)은 아직 충족하지 못했다.

| ID | 상태 | 검증 근거 | 잔여·검증 대기 |
|---|---|---|---|
| C-01 | 해결 | `tests/core/test_copy_stream_integrity.py`(큐 1/2/8 느린 소비자, 수정 전 코드에서 마지막 8행 유실 재현), 커밋 전 `assert_fully_consumed()`. 실DB E2E 3회(§8 첫 표, §8.3, §8.4)에서 원본·대상 집계 5종 일치 | — |
| C-02 | 해결 | 같은 파일의 모든 청크 분할 지점·무작위 분할 200회·이스케이프 따옴표 테스트. 실DB 중단→재개 E2E(`point_history_260509`, `260512`) 집계 일치 | — |
| H-01 | 부분 | 워커는 건너뛴 파티션이 있으면 예외로 끝나고 이력이 `failed`로 남는다. 재개가 실패 파티션을 다시 잡는다(`tests/core/test_copy_worker_completeness.py`) | 마법사 UI 경로(`on_error` → `failed`) 자동 테스트가 없다. 마법사는 `partial` 대신 `failed`로 표시한다(아카이브 다이얼로그만 `partial`) |
| H-02 | 해결(연결 수립 단계는 `connect_timeout` 10초 상한) | `tests/core/test_copy_cancel_and_snapshot.py`, `tests/core/test_worker_shutdown.py`(COPY·commit·Server COPY·연결 중·일시정지·아카이브 export 단계별 중지). `tools/e2e_cancel_copy.py` 실DB 중지→재개(§8.4). 아카이브 워커 비동기 반복 cancel과 close 전 cancel 정지(§8.5) | 리뷰 minor: 대상 준비 commit 앞 `_raise_if_stopped` 없음. 커밋 기록 없는 재개에서 `last_path_id`를 초기화하지 않음(완료 COUNT로 fail-closed) |
| H-03 | 부분 | 파티션마다 원본 `REPEATABLE READ, READ ONLY` 트랜잭션 하나(`test_copy_cancel_and_snapshot.py`). scratch PostgreSQL 동시 쓰기 통합 5건(`tests/core/test_copy_snapshot_integration.py`, 환경변수 필요) | PG 9.3 원본 동시 쓰기를 검증하지 못했다(운영 원본에 쓰기 금지). 재개는 새 snapshot이라 이미 복사한 행의 update를 탐지하지 못한다. 일시정지 중 xmin 유지(UI 안내 없음) |
| H-04 | 해결 | `tests/database/test_schema_qualification.py`, `test_sql_safety.py`, 실DB shadow 스키마 테스트 `tests/integration/test_sql_boundaries_realdb.py`(temp DB) | 운영 bms30 기존 트리거 함수는 부모 재생성 때만 교체. `nextval('seq'::regclass)` DEFAULT에 schema 없음 |
| H-05 | 해결 | `tests/database/test_connection_params.py`, `test_system_ca.py`, TLS 통합 13 passed(§8.3) | Windows 실제 TLS PostgreSQL 연결. 기존 `require` 프로필이 `verify-full`로 바뀜(릴리스 노트) |
| H-06 | 해결 | 리뷰 회귀 16건·`tests/utils/test_secret_store.py`·뮤테이션 8종 포착(`d50d209`). 실데이터 적용은 D2 릴리스 5단계(§8.6). 2차 리뷰 major(같은 프로세스 복수 매니저 키 갈림)는 프로세스 공유 키 핸들·쓰기 직전 키 파일 대조로 해결(`b207738`, §8.7) | 다운그레이드 불가. 리뷰 minor: 키 기록 성공 뒤 되읽기 실패 시 그 세션 쓰기 거부(재시작으로 복구), 첫 생성의 일시 오류가 프로세스 동안 캐시됨. Windows 경로 핸들 식별자 검증 대기 |
| H-07 | 해결 | `tests/licensing/test_check.py::TestFailClosed` | — |
| H-08 | 해결(legacy 한계) | 이력 identity·재개 게이트 테스트, 실DB 중단→재개에서 identity 게이트 확인(§8.3, §8.4, §8.7). 리뷰 major(다중 유형 뒤 유형 누락)는 뒤 유형 결정 강제·기본값 없는 선택 창으로 해결(`b44596b`, `3fdae64`, §8.7) | 리뷰 minor: `started_at`이 checkpoint와 모순돼도 도입일 기준 제외를 믿음(시계 역방향). 선택 창 기본값 정책(기본값 없음) 사용자 확인 대기 |
| H-09 | 해결(legacy 한계) | 이력·checkpoint 단일 트랜잭션, N번째 실패 rollback, legacy 누락 보충 14건(`fb27614`). 아카이브 보충분은 채택 때 기록한 `legacy_supplemented` 이름만 원본 부재 시 0건 완료(`3fdae64`, §8.7) | v1.2.8에서 이미 채택한 아카이브 이력은 기록이 NULL이라 보충분 부재도 실패(폐기로만 복구). copy 워커의 원본 부재 판정은 `information_schema` 기반·전 파티션 적용(별도 검토) |
| M-01 | 해결 | `tests/core/test_partition_discovery_recovery.py`, 실DB M-01 통합(temp) | — |
| M-02 | 해결 | `tests/core/test_archive_manifest_cas.py`(스레드 4·프로세스 3 경쟁, 수정 전 lost update 재현) | — |
| M-03 | 해결(제거) | OFFSET 기반 `migration_worker.py` 삭제, 생성 경로 0건 | — |
| M-04 | 해결(한계) | `tests/core/test_archive_manifest_auth.py`, `test_file_archive_security.py`, `tests/ui/dialogs/test_archive_security_prompt.py` | passphrase를 비우면 다운그레이드는 확인+WARNING만. 같은 passphrase 옛 아카이브 롤백·기밀성은 범위 밖 |
| M-05 | 해결(비Windows는 0600 평문) | DPAPI 래핑(`WrappedKeyFile`), 디렉터리 복제 회귀 테스트. 실데이터 적용은 §8.6 | — |
| M-06 | 해결 | `tests/release/test_issue_license.py` 32건 | 운영: 기존 평문 키 사본 폐기, 서버 signer의 암호화 PEM 읽기 |
| M-07 | 해결 | `.iss` `[Code]` 컴파일·음성 대조(§8.1), `tests/licensing/test_activation.py` 씨앗 수명 8건 | clean VM 관리자/사용자/무음 설치·업그레이드 smoke 미수행 |
| M-08 | 해결 | `build.bat` `uv sync --locked`, `uv lock --check`(Mac·Windows) | CI 없음 — lock 강제는 build.bat에서만 |
| M-09 | 해결 | `installer/verify_prerequisites.ps1` 양성·음성 대조(§8.1). 실제 `build_installer.bat` 실행 결과는 §8.6 | — |
| M-10 | 해결 | `tests/release/test_windows_script_encoding.py`(LF 주입 시 실패 확인). 실제 batch 실행은 §8.6 | clean Windows VM CI |
| M-11 | 보류 — 코드서명 인증서 없음 | `installer/codesign.ps1` 훅만: 인증서 없으면 `UNSIGNED BUILD` 경고, `CODESIGN_REQUIRED=1`이면 실패 | 인증서 조달, `unins000.exe` 서명, 릴리스 gate에 `CODESIGN_REQUIRED=1` |
| M-12 | 해결 | 미완료 이력 프로필 삭제 차단·명시적 폐기 테스트 | — |
| M-13 | 해결 | `tests/core/test_worker_shutdown.py`(단계별 종료·timeout 강제 종료·임시 파일/잠금/스레드 누수), `tests/test_main_shutdown.py`, `tests/test_tray_icon.py`, `tests/utils/test_logger_mixins.py`(§8.5) | 실제 Windows 트레이 종료 수동 확인 |
| M-14 | 해결 | `tests/core/test_table_creator_transactions.py`(`FakePgConnection` 중단 트랜잭션 규칙), 실DB M-14 통합(temp) | 부모 잠금 구간이 길어져 동시 생성 시 경합 가능(즉시 실패 후 rollback) |

자동 게이트(Mac, `b340b42`): 단위 1161 passed / 3 skipped / 21 deselected, ruff format(150 files)·check, mypy(61 files) 통과.

### 1차 수정 — v1.2.7

브랜치 `fix/copy-integrity-license-failclosed`.

| ID | 상태 | 수정 내용 |
|---|---|---|
| C-01 | 해결 | `CopyStreamBuffer`: 정상 종료(close)와 취소를 분리. close는 큐 빈자리를 기다려 종료 표시를 넣고 소비자는 끝까지 비운다. 취소·오류에서 `read()`는 예외를 던져 대상 COPY가 실패(커밋 안 됨). 커밋 전 `assert_fully_consumed()`(생산 문자 수 == 소비 문자 수). 행 수·마지막 키는 소비자 기준. |
| C-02 | 해결 | `_CsvRecordTracker`: 따옴표 상태를 청크 사이에 이어 추적해 CSV 레코드 경계를 판정, 마지막 레코드만 `csv` 모듈로 분해. 따옴표 없는 청크는 기존 split 빠른 경로. 닫히지 않은 따옴표로 EOF면 오류. |
| (신규) | 추가 | `_verify_partition_row_count()`: Python COPY·Server COPY 모두 완료 기록 전에 원본·대상 `COUNT(*)` 일치를 확인. 불일치면 `failed`. |
| H-01 | 워커 측 해결 | `skip_on_error`로 건너뛴 파티션이 있으면 실행 끝에 예외 → UI가 이력을 `failed`로 유지, '이어서 시작'이 실패 파티션을 다시 잡는다. |
| H-07 | 해결 | `check_license()`는 예외를 던지지 않고 `CHECK_FAILED`(제한 모드: 재개만 허용)를 돌려준다. `main.py`도 예외 시 `None` 대신 `CHECK_FAILED`. |

검증
- 단위 테스트 593 passed (기존 570 + 신규 23): `tests/core/test_copy_stream_integrity.py`(큐 크기 1/2/8 느린 소비자, 모든 청크 분할 지점, 무작위 분할 200회, 이스케이프 따옴표, 취소/오류), `tests/core/test_copy_worker_completeness.py`, `tests/licensing/test_check.py::TestFailClosed`.
  - 수정 전 코드에서 C-01 테스트는 40행 중 마지막 8행(큐 크기와 동일)이 사라지는 것으로 재현됨.
- 실DB E2E (bms93 PG 9.3 → bms30 `temp` PG 16, 파티션당 약 471만 행): Python COPY 전체, Server COPY 전체, Python COPY 5배치 후 중지→재개 — 세 경우 모두 원본·대상 `count`, `sum(path_id)`, `sum(issued_date)`, `sum(length(changed_value))`, `connection_status` 참 개수 일치. 테스트 파티션은 검증 후 삭제.
- ruff format/check 통과. mypy는 기존 오류 1건(`src/ui/dialogs/log_viewer_dialog.py:387`, 이번 변경과 무관) 유지.

남은 항목: H-02~H-06, H-08, H-09, M-01~M-14, 서명 릴리스(단계 5). 재감사 전까지 운영 승인 보류 원칙은 유지한다.

### 8.1 릴리스·배포 경로 (M-06 ~ M-11) — 브랜치 `audit/w1-release`

| ID | 상태 | 수정 내용 | 남은 것 |
|---|---|---|---|
| M-06 | 해결 | `tools/issue_license.py`: 개인키를 PKCS#8 PEM + `BestAvailableEncryption`으로만 저장(`O_EXCL`, POSIX 0600 / Windows `icacls`). 패스프레이즈는 getpass 또는 `DBMT_ISSUER_KEY_PASSPHRASE`만(CLI 인자 없음, `allow_abbrev=False`), 새 키는 12바이트 이상. 그룹·기타 권한이 열린 키는 거부. 키 ID = 공개키 SHA-256 fingerprint를 생성·발급·조회 때 출력. 평문 키(raw 32B·비암호 PEM)는 패스프레이즈를 묻기 전에 거부하고 `--convert-legacy`로 변환만 허용(원본은 사람이 폐기). docstring·`LICENSE_GUIDE.md`에 rotation·폐기 절차와 "운영 발급은 라이선스 서버 signer, 이 도구는 개발·비상용" 명시. 리뷰 지적(서버 signer는 raw 32바이트만 읽어 교체 절차가 그대로는 실행되지 않음)에 따라 `--export-signer-raw <새 경로>` 추가. 암호화 원본에서만, O_EXCL·0600, 덮어쓰기 거부, 평문 경고·폐기 안내, 키 ID·`EXPECTED_PUBLIC_KEY_B32` 출력. 내보낸 raw로는 도구가 발급하지 않음. 교체 절차에 두 형식과 서버 배치·`match` 확인 단계를 명시. | 기존 평문 키 사본이 있다면 변환 후 폐기(운영 작업). 서버 signer가 암호화 PEM+패스프레이즈 secret을 읽도록 바꾸기(라이선스 서버 저장소 후속) |
| M-07 | 해결(Windows 설치 smoke 대기) | `.iss`: `/LICENSEKEY=`를 감지하면 값은 기록하지 않고 설치 중단, 무음 설치는 `/LICENSEFILE=<경로>`(읽기 실패·빈 파일이면 중단). 입력 필드 마스킹(`Add(..., True)`), 키 값 `Log()` 없음. 씨앗을 `{app}\seed\license.seed`로 옮기고 `[Dirs] users-modify`로 일반 사용자 앱이 지울 수 있게 함. 1.2.7 이하 `{app}\license.seed`는 업그레이드가 이동·삭제. 앱: `activation.discard_seed()`가 `activate`/`touch` 직후(=등록 성공) 사용자 폴더에 `license.key`가 저장된 경우에만 씨앗 삭제, 실패는 비치명·다음 실행 재시도. | clean VM에서 관리자/사용자 설치·무음 설치·업그레이드 smoke(릴리스 단계) |
| M-08 | 해결 | `uv.lock` 추적(`.gitignore`에서 제거, 루트 `.gitattributes`에 `uv.lock text eol=lf`). `build.bat`은 `uv sync --locked --all-extras`(lock drift면 실패, `uv pip install` 제거). `tools/bump_version.py`가 lock 안의 프로젝트 버전도 함께 올려 다음 빌드의 `--locked`가 깨지지 않음. 업데이트 절차는 `BUILD_GUIDE.md` "의존성 lock". | CI 부재 — lock 강제는 build.bat에서만 |
| M-09 | 해결 | `installer/verify_prerequisites.ps1` + `installer/prerequisites.sha256`: 고정 SHA-256 일치, `Get-AuthenticodeSignature` Valid, 서명자 CN·O = Microsoft Corporation, 타임스탬프 존재를 모두 만족해야 `build_installer.bat`이 ISCC를 실행. 고정값은 my-wsl-01 `dist\prerequisites\vc_redist.x64.exe`를 읽기 전용 확인(14.44.35211.0, `CC0FF0EB…096B713B`, 서명자 thumbprint `8F985BE8…1A1DB975`, Microsoft Time-Stamp). 갱신 절차는 `BUILD_GUIDE.md`. | — |
| M-10 | 해결(회귀 검사 추가) | 원인은 LF 줄바꿈 UTF-8 배치를 `cmd.exe`가 잘못 끊어 읽은 것. 루트 `.gitattributes`(`*.bat/*.cmd/*.ps1/*.iss eol=crlf`)가 이미 정규화했고 1.2.7을 이 상태로 빌드함. `tests/release/test_windows_script_encoding.py`가 `git check-attr eol`=crlf, 작업 트리 bare LF 0, 파일별 인코딩(build·installer bat UTF-8 무BOM + `chcp 65001`, `.iss` UTF-8 BOM, `.ps1` ASCII, `run_dev.bat` CP949)을 고정. LF 1개 파일 주입 시 실패함을 확인. | clean Windows VM CI에서 직접 실행 |
| M-11 | 부분(훅만) | 인증서가 없어 실제 서명 불가. `installer/codesign.ps1`: `CODESIGN_CERT_THUMBPRINT` 또는 `CODESIGN_PFX`+`CODESIGN_PFX_PASSWORD`(PFX는 실행 중에만 CurrentUser\My에 가져와 비밀번호가 signtool 명령줄에 안 나옴)가 있으면 `signtool sign /fd SHA256 /tr <ts> /td SHA256` → `signtool verify /pa` → `Get-AuthenticodeSignature` Valid+타임스탬프 확인. 없으면 `WARNING: UNSIGNED BUILD` 배너, `CODESIGN_REQUIRED=1`이면 실패. `build.bat`(exe)·`build_installer.bat`(서명 상태 확인 + 설치본 서명)에 연결. | 코드서명 인증서 조달, `unins000.exe` 서명(Inno `SignTool=`), 릴리스 gate에서 `CODESIGN_REQUIRED=1` |

검증 (2026-09-25)
- 단위 테스트 666 passed(기준 607 + 신규 59): `tests/release/test_issue_license.py`(21, 리뷰 수리 후 32 — 전체 677), `tests/release/test_release_scripts.py`(21), `tests/release/test_windows_script_encoding.py`(9), `tests/licensing/test_activation.py` 씨앗 수명 8. 신규 테스트는 구현 전 기능 부재로 실패함을 먼저 확인.
- ruff format/check, mypy(src) 통과. `uv lock --check`가 Mac uv 0.11.16과 Windows 호스트 버전 uv 0.10.4(uvx) 모두 통과.
- my-wsl-01 **출력 없는** 확인(임시 폴더 `C:\Temp`에서 실행 후 삭제, 저장소·dist 무변경): `verify_prerequisites.ps1` 실제 파일 exit 0 / 틀린 해시·다른 서명자(notepad, CN=Microsoft Windows)·파일 없음 exit 1. `codesign.ps1` 인증서 없음 → UNSIGNED 배너 exit 0, `CODESIGN_REQUIRED=1` → exit 1, `-CheckOnly` Microsoft 서명 파일 → OK. 버전 추출 `for /f` → `1.2.7`. `ISCC /O-`(산출물 비활성)로 새 `.iss` `[Code]` 컴파일 성공, 일부러 주석을 깨뜨린 사본은 `Syntax error`로 실패(음성 대조).
- 실제 Windows 빌드(`build.bat`/`build_installer.bat` 전체 실행)와 설치 smoke는 릴리스 단계에서 수행한다.

### 8.2 M-02 / M-04 — 파일 아카이브 (브랜치 `audit/w1-archive`)

| ID | 상태 | 수정 내용 |
|---|---|---|
| M-02 | 해결(단위 검증) | `ArchiveManifestStore.save`: 잠금 안에서 디스크 최신본을 다시 읽어 3-way 병합. 파티션 항목마다 `entry_version` CAS — 이 writer가 안 바꾼 항목은 디스크 값 유지, 바꾼 항목은 base version·내용이 디스크와 같을 때만 +1 기록, 아니면 `ManifestConflictError`. 디스크 manifest를 못 읽으면 덮지 않고 실패(예전엔 예외를 삼키고 덮음). 기본 manifest가 깨져 한 번 늦은 백업으로 병합할 때는 이 writer가 이미 본 항목(없음·낮은 version)을 되살려 마지막 commit 항목이 사라지지 않는다(리뷰 지적 수정). export는 `commit_partition`으로 파일 교체와 항목 기록을 같은 잠금 안에서 해 경쟁 writer와 섞이지 않는다. Windows 락은 60초 비차단 재시도, `os.replace` 읽기 경합 재시도. |
| M-04 | 해결(단위 검증) | 신규 항목 `checksum_sha256` 필수(없으면 저장 실패), import는 checksum 없는 항목을 명시적 확인 없이 거부. passphrase 기반 PBKDF2-SHA256(600k)+HMAC-SHA256으로 canonical manifest 인증(export 때 지정했으면 import 때 필수, 기계별 키 없음). 세탁(변조 디스크 재서명)·passphrase 없는 쓰기는 차단. 다운그레이드(auth 제거)는 **passphrase를 입력하면 거부**(import 확인 창이 인증 없는 경로에서도 export 때 passphrase를 지정했는지 먼저 묻는다)하지만, 사용자가 비워 두면 legacy와 구분할 수 없어 **명시적 확인 + WARNING(차단 아님)**. 같은 PC에서의 기계적 탐지(로컬 이력 DB에 경로별 salt·revision 기록)는 후속 과제. import는 대상 DB 연결 전에 신뢰 판정 + 선택 파일 전체 SHA-256 사전 검증, DDL은 검증된 메모리 manifest로만 생성(TOCTOU). legacy(1.2.7 이하 48건)는 UI의 명시적 확인 + 작업 로그 WARNING으로만 import. 신뢰 경계: `docs/base/archive-trust-boundary.md`. |

검증
- 단위 테스트 683 passed(기존 607 + 신규 76, 리뷰 수리 11건 포함): `tests/core/test_archive_manifest_cas.py`(스레드 4·프로세스 3 경쟁 포함),
  `tests/core/test_archive_manifest_auth.py`, `tests/core/test_file_archive_security.py`,
  `tests/ui/dialogs/test_archive_security_prompt.py`. 수정 전 코드에서 스레드·프로세스 경쟁 테스트는
  다른 writer의 최신 값이 오래된 메모리 값으로 되돌아가는 lost update로 실패함을 확인.
- 기존 테스트 조정(약화 아님): checksum 없는 항목을 store로 저장하던 픽스처 3곳을 "올바른 checksum으로 저장"
  또는 "디스크 JSON을 직접 고쳐 legacy 흉내"로 변경, skip_on_error 테스트의 `load` 목에 신뢰 판정 목 추가.
- 실DB E2E: 해당 작업에 배정된 파티션이 없어 수행하지 않음(아카이브 경로는 DB 쪽이 기존 COPY와 동일).

남은 한계: 같은 passphrase로 만든 예전 아카이브 한 벌로의 롤백, 기밀성(암호화 없음)은 범위 밖(문서화).

### 8.3 wave1 통합 (main 병합, 2026-09-25)

`audit/w1-conn` → `w1-secrets` → `w1-hist` → `w1-release` → `w1-archive` 순으로 `--no-ff` 병합했다.
§8.1·§8.2 외 브랜치의 상태는 아래와 같다.

| ID | 상태 | 요약 | 남은 것(리뷰 지적) |
|---|---|---|---|
| H-05 | 해결 | 공용 빌더 `src/database/connection_params.py`로 PostgreSQL 연결 지점을 모두 모았다. SSL 기본값은 `verify-full`, CA 칸이 비면 `src/database/system_ca.py`가 OS 신뢰 저장소 PEM을 찾아 넘긴다. `require`는 위험 승인과 감사 로그가 있어야 한다. | 기존 SSL 프로필이 `require`에서 `verify-full`로 바뀐다(릴리스 노트). Windows 실제 TLS PostgreSQL 연결은 릴리스 게이트 |
| H-02 / H-04 | 부분(연결 단계) | 모든 연결에 `connect_timeout`(기본 10초)과 `search_path=public`을 적용했다. | COPY·commit 취소, relation schema 한정 |
| H-06 / M-05 | 해결 | 읽기 경로의 legacy fallback을 없애고 1회 마이그레이션(키 교체·재암호화·백업 정리·완료 표식)으로 대체했다. 키는 DPAPI로 감싼다(비Windows는 0600 평문). 복호화하지 못한 행은 잠긴 프로필로 보인다. | ~~2차 리뷰 major: 같은 프로세스 복수 매니저 키 갈림~~ → §8.7에서 해결. 다운그레이드 불가(릴리스 노트) |
| H-08 / H-09 / M-12 | 해결(legacy 한계) | 이력과 checkpoint를 한 트랜잭션으로 만들고 endpoint 지문·불변 계획으로 재개를 검증한다. 미완료 이력이 있는 프로필은 명시적 폐기 후에만 삭제된다. | ~~2차 리뷰 major: 다중 유형 legacy 뒤 유형 누락, 아카이브 보충 파티션 미완료~~ → §8.7에서 해결 |

병합 중 처리
- 텍스트 충돌: `file_archive_migration_dialog.py` import 인접 줄(둘 다 유지), 이 문서 끝 절(§8.1·§8.2로 분리).
- 의미 충돌: 잠긴 프로필 편집 시 `_confirm_identity_change`가 기본값 설정과 비교하던 문제를 미완료 이력의
  endpoint 지문과 비교하도록 고쳤다.

검증: 단위 978 passed / 2 skipped, ruff format·check, mypy(61 files) 통과, TLS 통합 13 passed.
실DB E2E(bms93 → temp, `--drop-after`): Python COPY `point_history_260507`(4,704,295행), Server COPY
`point_history_260508`(4,704,267행), 중단→재개 `point_history_260509`(4,702,462행, identity 게이트 확인)
모두 원본·대상 집계 5종 일치.

남은 항목: H-01(UI 표시), H-02·H-04 잔여, H-03, M-01, M-03, M-11(인증서), M-13, M-14와 위 리뷰 지적.
재감사 전까지 운영 승인 보류 원칙은 유지한다.

### 8.4 wave2 통합 (main 병합, 2026-09-25)

`audit/w2-copy` → `audit/w2-sql` 순으로 `--no-ff` 병합했다. 두 브랜치 모두 1차 리뷰 major를 TDD로 수리했고
2차 리뷰가 pass였다.

| ID | 상태 | 요약 | 남은 것(리뷰 지적) |
|---|---|---|---|
| H-02 | 해결(연결 수립 단계 제외) | `stop()` → `_on_stop_requested()`: `CopyStreamBuffer` 취소와 원본·대상 `cancel()`을 백그라운드에서 작업 종료까지 반복한다. 중지 뒤에는 새 commit을 시작하지 않는다. 중지는 오류가 아니라 `CopyCancelled`로 처리하고 checkpoint를 `failed`로 바꾸지 않는다. 승인하거나 자동으로 수행한 TRUNCATE는 COPY 전에 따로 커밋한다. 재개 keep은 checkpoint에 `rows_processed>0`이 있을 때만 하고, 기록이 없으면 확인(ask)한다. | 연결 수립 단계는 `connect_timeout`(10초)에만 묶인다. 아카이브 워커 훅이 없다(M-13과 함께 처리). `finally`의 close와 cancel 스레드가 경합한다(`_work_finished.set()`과 join을 close보다 먼저 해야 한다). 대상 준비 commit 앞에 `_raise_if_stopped`가 없다. 커밋 기록 없는 재개에서 checkpoint `last_path_id`를 초기화하지 않는다(COUNT로 fail-closed) |
| H-03 | 해결(한계 3) | 파티션마다 원본 `REPEATABLE READ, READ ONLY` 트랜잭션 하나에서 배치, Server COPY, 완료 검증 COUNT를 실행한다. | 재개는 새 snapshot이라 이미 복사한 행의 update를 탐지하지 못한다. 일시정지 중에도 원본 xmin을 붙잡는다(UI 안내 없음). PG 9.3에서 동시 쓰기를 검증하지 못했다 |
| H-04 | 해결 | COPY 워커는 `_relation()`, 그 밖의 코드는 `qualified_name`/`sql.Identifier("public", …)`로 모든 relation을 `"public"`에 한정했다. `information_schema` 조회에는 모두 `table_schema='public'`을 붙였다(아카이브 부모 컬럼 조회 포함). 트리거 본문은 `format('%I.%I','public',…)`을 쓴다. 병합 뒤 `sql.Identifier(partition_name)`는 0건이다. | 운영 bms30의 기존 트리거 함수는 부모를 다시 만들 때만 교체된다. `nextval('seq'::regclass)` DEFAULT는 schema가 없다. schema 상수가 `PUBLIC_SCHEMA`와 `RELATION_SCHEMA`로 중복돼 있다 |
| M-01 | 해결 | `_estimate_row_count`를 SAVEPOINT로 격리했다. 실패하면 0을 반환하고 탐색을 이어 간다. | — |
| M-03 | 해결(제거) | OFFSET 기반 legacy `MigrationWorker`(`migration_worker.py`)를 삭제했다. 생성하는 곳이 없었다. | — |
| M-14 | 해결 | 파티션 생성 DDL과 metadata를 한 트랜잭션으로 묶어 `commit_or_raise`로 커밋하고, 실패하면 rollback한다. 무시해도 되는 인덱스·CLUSTER 오류만 SQLSTATE 허용 목록으로 SAVEPOINT 격리한다. `apply_params`의 SET 실패도 SAVEPOINT로 처리한다. | 부모 잠금 구간이 길어져 여러 인스턴스가 동시에 생성하면 경합할 수 있다(즉시 실패 후 rollback) |

병합 중 처리
- 텍스트 충돌은 `src/core/CLAUDE.md` 한 곳이었다. 취소 규약·원본 snapshot 절(w2-copy)과 SQL 경계 규칙 절(w2-sql)을 모두
  살렸다. M-03 삭제로 틀린 설명이 된 'psycopg3(탐색·legacy 워커)'는 'psycopg3(탐색)'로 고쳤다.
- 코드 충돌은 없었다. w2-sql 리뷰가 남긴 migration_worker.py `information_schema` 필터 누락은 M-03 삭제로 함께 없어졌다.

동작 변경(릴리스 노트)
- 사용자 중지와 비 skip 모드의 TRUNCATE 거부는 '오류'가 아니라 '중단'으로 끝난다. '이어서 시작'으로 재개할 수 있다.
- TRUNCATE를 승인(또는 server 모드가 자동 TRUNCATE)한 뒤 이관이 실패하거나 중지돼도 옛 대상 데이터는 되살아나지 않는다.
- 커밋된 배치 기록이 없는 파티션을 재개할 때 대상에 행이 있으면 TRUNCATE 확인 창이 뜬다.

검증: 단위 1070 passed / 3 skipped / 21 deselected, ruff format(146 files)·check, mypy(60 files) 통과.
실DB E2E(bms93 PG 9.3 → temp PG 16, `--drop-after`): Python COPY `point_history_260510`(4,702,204행, 32.7s),
Server COPY `point_history_260511`(4,705,733행, 20.1s), 중단→재개 `point_history_260512`(500,000행에서 중지,
재개 후 4,708,021행, identity 게이트 확인). 세 경우 모두 원본·대상 집계 5종이 일치했다. 실행 뒤
temp `partition_table_info`에 남은 세 행도 지웠다.

남은 항목: H-01(UI 표시), M-11(인증서), M-13, 위 리뷰 지적, CI의 PG 통합 테스트 job(9.x 포함).
재감사 전까지 운영 승인 보류 원칙은 유지한다.

### 8.5 wave3 통합 (main 병합, 2026-09-25)

`audit/w3-shutdown`을 `--no-ff` 병합(`6f193ae`)하고 리뷰 minor를 후속 커밋(`b6dd474`)으로 고쳤다.

| ID | 상태 | 요약 |
|---|---|---|
| M-13 | 해결 | `src/core/worker_registry.py`(약한 참조 `WorkerRegistry` + `ShutdownCoordinator`): 전체 `stop("app_shutdown")`·조회 `cancel_query()` → 30초 대기(이벤트 루프 유지) → flush(로거 → 로컬 DB) → `app.exit(0)`. 초과하면 WARNING 후 강제 종료(`finalize_exit`가 `os._exit`). 종료 중 시작한 워커는 등록 즉시 중지. 트레이 종료는 `quit_requested`만 발행한다. 트레이 없는 창 닫기도 조정자를 거친다(예전엔 프로세스가 남음). DB 로그 writer가 `close()` 뒤 큐에 남은 로그를 끝까지 쓴다. |
| H-02 잔여 | 해결 | 아카이브 워커의 동기 cancel·다른 스레드 rollback을 비동기 반복 cancel로 바꿨다. COPY 워커와 아카이브 워커 모두 연결 close 전에 `_stop_cancel_retries()`로 cancel 반복을 멈춘다(psycopg2 cancel·close 경합, 리뷰 minor. 수정 전 테스트가 close 순간 cancel 스레드 생존 `[True, True]`로 실패). |

검증: 단위 1111 passed / 3 skipped / 21 deselected(wave2 대비 +41), ruff format·check, mypy(61 files) 통과.
실DB E2E는 하지 않았다. 종료 경로는 DB 쪽 동작이 wave2 중지 경로와 같고, 미커밋 배치는 연결 종료로 롤백된다.

### 8.6 D2 릴리스 — 1.2.8 (my-wsl-01, 2026-09-25)

main `b678338`에서 Windows 빌드. 릴리스 커밋 `c1b17cf`, 태그 `v1.2.8`.

| 단계 | 결과 |
|---|---|
| 실데이터 백업 | 어떤 실행보다 먼저 `%APPDATA%`(= `C:\Users\hijde\AppData\Roaming`)의 앱 파일(`db_migration.db`, 옛 `db_migration.backup-*.db`, `.encryption_key`, `profile_fernet.key`, `.activation`, `license.key`, `logs\`)과 개발 실행 데이터(`Python\`의 DB·키·로그, `pytest-qt-qapp\`)를 `DBMigrationTool-backup-20260925\`로 복사했다. 29개 파일 SHA-256 29/29 일치. 설치 앱과 실프로필 통합 테스트는 모두 `%APPDATA%` 루트를 쓴다 |
| pull·환경 | `git pull --ff-only` 성공. 추적 `.bat/.cmd/.ps1/.iss` 7개 모두 CRLF(bare LF 0), 재체크아웃 불필요. `uv.lock` 추적 확인 후 `uv sync --frozen --all-extras`(uv 0.10.4)와 `uv lock --check` 통과 |
| 단위 테스트 | 처음 1106 passed / 2 failed. 두 건 모두 테스트가 POSIX를 전제한 문제였다(경로 구분자 문자열 비교, 텍스트 모드 `\n`→`\r\n` 변환으로 checksum 불일치). `b678338`로 고친 뒤 **1108 passed / 6 skipped / 21 deselected**. ruff format·check, mypy(61 files) 통과 |
| 실프로필 통합 | `DBMIG_RUN_REAL_PROFILE_TESTS=1 -m integration -k saved_profiles` **1 passed**(bms93·bms30 연결). 적용 전에는 평문 키 파일(과거 공개 키 아님)이 암호문 7건을 모두 풀었고 legacy 키로 풀리는 암호문은 0건이었다. 적용 후 키 파일은 `DBMT-KEY2:dpapi`(migrated, 이전 키 저널 없음), DB 키 상태 `dpapi`, 새 키로 7/7 복호화, legacy 0건, 교체 전 평문 키로 0/7. 프로필 3개 모두 읽힘(잠김 0, bms93→bms30 1). `*.bak-pre-keywrap` 잔여 0 |
| 빌드 | `build.bat`이 1.2.7 → 1.2.8로 올리고 exe 생성. `build_installer.bat`의 M-09 gate 통과(`vc_redist.x64.exe` 14.44.35211.0, 고정 해시·Microsoft 서명 일치). 두 산출물 모두 `WARNING: UNSIGNED BUILD`(M-11 보류) |
| 버전 동기화 | Mac에서 `bump_version.py --set 1.2.8`. 4파일 blob 해시가 Windows와 같음을 확인하고 커밋·태그·push. Windows는 버전 파일을 `git checkout`으로 되돌린 뒤 pull, 작업 트리 깨끗 |

산출물

| 파일 | SHA-256 | 크기 | ProductVersion | 서명 |
|---|---|---:|---|---|
| `dist\DBMigrationTool.exe` | `E2CE917C47DB906E600951CCD5A2624EC7F6D24CCF8D2A4FBC67DBA31C86B4A1` | 65,614,080 | 없음(PyInstaller spec에 버전 리소스 없음) | NotSigned |
| `dist\installer\DBMigrationTool-Setup-1.2.8.exe` | `2B6FF2404DFBFE142D22343B3337BA3BC2A9B3D9BCDF68D69B0A4A78D4868CDC` | 90,496,100 | 1.2.8 | NotSigned |

릴리스 커밋 `c1b17cf` 메시지의 exe 해시 축약 `…9C86B4A1`은 오기다. 정확한 값은 위 표다.

주의·남은 것
- 백업 폴더에는 교체 전 평문 키와 그 키로 풀리는 DB 사본이 함께 있다. 이 폴더가 H-06/M-05 노출 경로이므로
  1.2.8이 안정되면 지운다.
- `%APPDATA%\Python\`(개발 실행)과 `pytest-qt-qapp\`에는 아직 평문 키가 있다. 그 경로로 앱을 실행하면 같은 1회 마이그레이션이 적용된다.
- 미서명이라 배포용 릴리스가 아니다(M-11). 설치·업그레이드·제거 smoke와 Windows 실제 TLS 연결은 하지 않았다.


### 8.7 리뷰 major 통합 (main 병합, 2026-09-26)

`fix/secrets-divergence` → `fix/legacy-adoption` 순으로 `--no-ff` 병합했다(`3f570b6`, `b340b42`). 두 브랜치 모두
최종 리뷰가 pass였고 남은 지적은 minor뿐이다. 이 병합으로 §8.3에서 넘어온 리뷰 major 3건이 모두 해소됐다.

| ID | 커밋 | 요약 |
|---|---|---|
| H-06 / M-05 (키 갈림) | `b207738` | `shared_profile_key()`·`ProfileKeyHandle`: (DB realpath, 키 파일 realpath)를 키로 쓰는 모듈 레지스트리에서 키 준비·마이그레이션을 프로세스당 1회만 한다. fallback·진행 중·실패 결과도 같은 프로세스에서 재시도하지 않는다. 키 재설정도 핸들을 거쳐 모든 매니저에 반영된다. `create_profile`·`update_profile`·`save_connection`은 쓰기 직전 키 파일을 다시 읽어 대조(`for_write`)하고, 달라졌거나 지워졌거나 읽을 수 없으면 `ProfileKeyUnavailableError`로 아무것도 쓰지 않는다. |
| H-08 (다중 유형 legacy) | `b44596b`, `3fdae64` | 구버전 checkpoint 생성 순서(유형 코드 ED→PH→PS→RT→TH)로 checkpoint 없는 유형을 앞(원래 선택 안 함)과 뒤(끊겨 빠졌을 수 있음)로 나눈다. 뒤 유형은 '원래 작업의 항목 확인' 창에서 유형마다 있었음/없었음을 골라야 하고(기본값 없음, 모두 정하기 전 확인 버튼 비활성), 정하지 않은 채택은 `LegacyTypeDecisionError`로 거부된다. 이력 시작일에 도구에 없던 유형(`LEGACY_TYPE_INTRODUCED`: ED·RT·TH 2025-11-19, PS 2026-03-30)은 묻지 않는다. import 경로는 대상 덮어쓰기 경고를 띄운다. |
| H-09 (아카이브 보충분) | `b44596b`, `3fdae64` | 채택 때 보충한 이름만 `migration_history.legacy_supplemented`(JSON, 계획 기록과 같은 트랜잭션)에 기록한다. 아카이브 워커는 이 집합(`absent_ok_partitions`)에 든 이름만 원본에 없으면 0건 완료로 처리한다. export는 `pg_catalog`로 존재를 확인하고(조회 전후 rollback — 건너뛰기 모드의 중단 트랜잭션 대응), import는 manifest 항목과 파일이 모두 없을 때만 부재로 본다. 원래 checkpoint·새 계획 파티션의 부재는 전처럼 실패한다. |

병합 중 처리
- 텍스트·의미 충돌 없음(변경 파일이 겹치지 않음). 두 브랜치가 미뤄 둔 문서 갱신(이 절, 종합 상태표, `CLAUDE.md` §6)을 병합 뒤 반영했다.
- 스키마 변경: `local_db.py`에 `legacy_supplemented TEXT` 컬럼(`_ADDED_COLUMNS`로 기존 DB에 추가, NULL은 빈 집합).

검증
- 단위 **1161 passed / 3 skipped / 21 deselected**(1.2.8 기준 1111 + secrets 14 + legacy 36), ruff format(150 files)·check,
  mypy(61 files) 통과. 각 브랜치에서 새 테스트가 수정 전 코드로 실패하는 것(RED)과 뮤테이션 포착을 확인했다.
- 실DB E2E(bms93 PG 9.3 → temp PG 16, **LAN 호스트 `192.168.0.48`** — 2026-09-26부터 iptime 5445/5446 포워딩 제거,
  `--drop-after`): Python COPY `point_history_260521`(4,714,327행, 33.4s), Server COPY `point_history_260522`
  (4,714,858행, 20.8s), 중단→재개 `point_history_260523`(500,000행에서 중지, 재개 후 4,711,294행, identity 게이트 확인)
  모두 원본·대상 집계 5종 MATCH. 실행 뒤 temp `partition_table_info`에 남은 세 행을 지웠다. 로그의 비밀번호 문자열 0건.
- legacy-adoption 브랜치는 별도로 아카이브 export(bms93 `point_history_260518` 4,715,608행)·import(temp, 부재 파티션만) 실DB 확인을 했다.

남은 리뷰 지적(minor, 운영 차단 아님)
- H-06: 새 키 기록 성공 뒤 되읽기 실패 시 핸들은 fallback k0, 디스크는 K1이라 그 세션 쓰기가 거부되고 안내 문구가 원인과 맞지 않는다(재시작으로 복구).
  첫 생성의 일시 오류(DPAPI 읽기 실패 등)가 프로세스 동안 캐시돼 뒤에 연 다이얼로그도 복구하지 못한다.
  핸들 식별자 `normcase(realpath)`의 Windows 경로 동작은 my-wsl-01에서 확인해야 한다.
- H-08: `started_at`이 도입일보다 이른데 그 유형의 checkpoint가 있으면(시계 역방향 모순) 뒤 유형을 조용히 제외한다.
  represented ∩ unavailable이 비어 있지 않으면 `started_at`을 믿지 않도록 고치는 것을 권한다. 선택 창 기본값 정책(기본값 없음)은 사용자 확인 대기.
- H-09: v1.2.8에서 이미 채택한 아카이브 legacy 이력은 `legacy_supplemented`가 NULL이라 보충분 부재도 실패한다(폐기로만 복구).
  Windows 실데이터에 해당 이력이 있는지 읽기 전용으로 세 볼 것. export 쪽 회귀 테스트 1건은 수정 전 코드에서도 통과한다(dialog→worker 통합 테스트 권장).
- 별도 이슈: copy 워커(`PostgresOptimizer.estimate_table_size`)는 원본 존재 확인에 `information_schema.tables`를 써서 권한 문제를 '없음'으로 오판할 수 있고, 모든 파티션에서 부재를 0건 완료로 처리한다.

재감사 전까지 운영 승인 보류 원칙은 유지한다. 릴리스(버전 올림·빌드·push)는 별도 단계에서 한다.
