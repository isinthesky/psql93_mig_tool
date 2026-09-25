# 전체 코드 감사 및 보완 계획

- 작성일: 2026-08-26
- 감사 기준: `main` / `3813ac3` / 애플리케이션 1.2.5
- 범위: `src`, `tests`, `tools`, PyInstaller 설정, Inno Setup 설치 프로그램, 빌드 스크립트
- 현재 상태: 계획 수립 완료, 제품 코드 수정 전

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

## 8. 진행 현황 (2026-09-25)

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
| M-06 | 해결 | `tools/issue_license.py`: 개인키를 PKCS#8 PEM + `BestAvailableEncryption`으로만 저장(`O_EXCL`, POSIX 0600 / Windows `icacls`). 패스프레이즈는 getpass 또는 `DBMT_ISSUER_KEY_PASSPHRASE`만(CLI 인자 없음, `allow_abbrev=False`), 새 키는 12바이트 이상. 그룹·기타 권한이 열린 키는 거부. 키 ID = 공개키 SHA-256 fingerprint를 생성·발급·조회 때 출력. 평문 키(raw 32B·비암호 PEM)는 패스프레이즈를 묻기 전에 거부하고 `--convert-legacy`로 변환만 허용(원본은 사람이 폐기). docstring·`LICENSE_GUIDE.md`에 rotation·폐기 절차와 "운영 발급은 라이선스 서버 signer, 이 도구는 개발·비상용" 명시. | 기존 평문 키 사본이 있다면 변환 후 폐기(운영 작업) |
| M-07 | 해결(Windows 설치 smoke 대기) | `.iss`: `/LICENSEKEY=`를 감지하면 값은 기록하지 않고 설치 중단, 무음 설치는 `/LICENSEFILE=<경로>`(읽기 실패·빈 파일이면 중단). 입력 필드 마스킹(`Add(..., True)`), 키 값 `Log()` 없음. 씨앗을 `{app}\seed\license.seed`로 옮기고 `[Dirs] users-modify`로 일반 사용자 앱이 지울 수 있게 함. 1.2.7 이하 `{app}\license.seed`는 업그레이드가 이동·삭제. 앱: `activation.discard_seed()`가 `activate`/`touch` 직후(=등록 성공) 사용자 폴더에 `license.key`가 저장된 경우에만 씨앗 삭제, 실패는 비치명·다음 실행 재시도. | clean VM에서 관리자/사용자 설치·무음 설치·업그레이드 smoke(릴리스 단계) |
| M-08 | 해결 | `uv.lock` 추적(`.gitignore`에서 제거, 루트 `.gitattributes`에 `uv.lock text eol=lf`). `build.bat`은 `uv sync --locked --all-extras`(lock drift면 실패, `uv pip install` 제거). `tools/bump_version.py`가 lock 안의 프로젝트 버전도 함께 올려 다음 빌드의 `--locked`가 깨지지 않음. 업데이트 절차는 `BUILD_GUIDE.md` "의존성 lock". | CI 부재 — lock 강제는 build.bat에서만 |
| M-09 | 해결 | `installer/verify_prerequisites.ps1` + `installer/prerequisites.sha256`: 고정 SHA-256 일치, `Get-AuthenticodeSignature` Valid, 서명자 CN·O = Microsoft Corporation, 타임스탬프 존재를 모두 만족해야 `build_installer.bat`이 ISCC를 실행. 고정값은 my-wsl-01 `dist\prerequisites\vc_redist.x64.exe`를 읽기 전용 확인(14.44.35211.0, `CC0FF0EB…096B713B`, 서명자 thumbprint `8F985BE8…1A1DB975`, Microsoft Time-Stamp). 갱신 절차는 `BUILD_GUIDE.md`. | — |
| M-10 | 해결(회귀 검사 추가) | 원인은 LF 줄바꿈 UTF-8 배치를 `cmd.exe`가 잘못 끊어 읽은 것. 루트 `.gitattributes`(`*.bat/*.cmd/*.ps1/*.iss eol=crlf`)가 이미 정규화했고 1.2.7을 이 상태로 빌드함. `tests/release/test_windows_script_encoding.py`가 `git check-attr eol`=crlf, 작업 트리 bare LF 0, 파일별 인코딩(build·installer bat UTF-8 무BOM + `chcp 65001`, `.iss` UTF-8 BOM, `.ps1` ASCII, `run_dev.bat` CP949)을 고정. LF 1개 파일 주입 시 실패함을 확인. | clean Windows VM CI에서 직접 실행 |
| M-11 | 부분(훅만) | 인증서가 없어 실제 서명 불가. `installer/codesign.ps1`: `CODESIGN_CERT_THUMBPRINT` 또는 `CODESIGN_PFX`+`CODESIGN_PFX_PASSWORD`(PFX는 실행 중에만 CurrentUser\My에 가져와 비밀번호가 signtool 명령줄에 안 나옴)가 있으면 `signtool sign /fd SHA256 /tr <ts> /td SHA256` → `signtool verify /pa` → `Get-AuthenticodeSignature` Valid+타임스탬프 확인. 없으면 `WARNING: UNSIGNED BUILD` 배너, `CODESIGN_REQUIRED=1`이면 실패. `build.bat`(exe)·`build_installer.bat`(서명 상태 확인 + 설치본 서명)에 연결. | 코드서명 인증서 조달, `unins000.exe` 서명(Inno `SignTool=`), 릴리스 gate에서 `CODESIGN_REQUIRED=1` |

검증 (2026-09-25)
- 단위 테스트 666 passed(기준 607 + 신규 59): `tests/release/test_issue_license.py`(21), `tests/release/test_release_scripts.py`(21), `tests/release/test_windows_script_encoding.py`(9), `tests/licensing/test_activation.py` 씨앗 수명 8. 신규 테스트는 구현 전 기능 부재로 실패함을 먼저 확인.
- ruff format/check, mypy(src) 통과. `uv lock --check`가 Mac uv 0.11.16과 Windows 호스트 버전 uv 0.10.4(uvx) 모두 통과.
- my-wsl-01 **출력 없는** 확인(임시 폴더 `C:\Temp`에서 실행 후 삭제, 저장소·dist 무변경): `verify_prerequisites.ps1` 실제 파일 exit 0 / 틀린 해시·다른 서명자(notepad, CN=Microsoft Windows)·파일 없음 exit 1. `codesign.ps1` 인증서 없음 → UNSIGNED 배너 exit 0, `CODESIGN_REQUIRED=1` → exit 1, `-CheckOnly` Microsoft 서명 파일 → OK. 버전 추출 `for /f` → `1.2.7`. `ISCC /O-`(산출물 비활성)로 새 `.iss` `[Code]` 컴파일 성공, 일부러 주석을 깨뜨린 사본은 `Syntax error`로 실패(음성 대조).
- 실제 Windows 빌드(`build.bat`/`build_installer.bat` 전체 실행)와 설치 smoke는 릴리스 단계에서 수행한다.
