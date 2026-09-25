# 파일 아카이브 신뢰 경계와 동시 저장 규칙

- 대상 코드: `src/core/archive_manifest.py`, `src/core/file_archive_workers.py`,
  `src/ui/dialogs/archive_security_prompt.py`
- 관련 감사 항목: M-02(manifest 동시 저장), M-04(checksum 필수화·manifest 인증)
  — `docs/plans/code-audit-remediation-2026-08-26.md`
- 적용 버전: manifest format `version 3` (1.2.7 이하는 `version 2`)

## 1. 무엇을 보호하고 무엇을 보호하지 않는가

| 위협 | 방어 | 결과 |
|---|---|---|
| 전송·저장 중 파일 손상(비트 오류, 잘린 복사) | 파티션 파일마다 SHA-256(`checksum_sha256`) | import가 적재 전에 거부 |
| 공유 폴더·USB에서 **데이터 파일만** 바꿈 | SHA-256 불일치 | 거부 |
| **파일과 manifest checksum을 함께** 바꿈 | manifest 전체에 passphrase 기반 HMAC-SHA256 | 거부(passphrase를 모르는 공격자는 MAC을 못 만든다) |
| manifest의 DDL 메타데이터(`parent_tables.columns`)·경로·행 수 조작 | 같은 HMAC가 manifest 전체를 덮는다 | 거부 |
| `auth` 블록을 지워 legacy처럼 보이게 함(다운그레이드) — 사용자가 passphrase를 입력한 경우 | passphrase가 있는데 manifest가 인증 없음 → `require_trusted`가 `allow_legacy_unverified`와 무관하게 거부. import 확인 창도 인증 없는 경로에서 "export 때 passphrase를 지정했나"를 먼저 묻고, 입력하면 "다운그레이드 의심"으로 시작하지 않는다 | **거부** |
| 같은 다운그레이드 — 사용자가 passphrase를 **비워 두고** legacy 확인을 누른 경우 | 파일만으로는 진짜 legacy와 구분할 수 없다 | **차단 아님.** 명시적 확인이 필요하고 경고가 작업 로그에 남는다(§1 한계 7) |
| 검증 뒤 디스크 manifest 바꿔치기(TOCTOU) | import는 검증한 메모리 사본만 쓴다(`ManifestTableCreator(manifest=...)`), 데이터 파일은 검증한 핸들 그대로 COPY | 거부 |
| 두 writer(스레드·프로세스)의 동시 저장 | 파일 락 + 항목별 `entry_version` CAS | 한쪽은 `ManifestConflictError`로 실패, 조용한 덮어쓰기 없음 |

보호하지 **않는** 것(명시적 한계):

1. **passphrase를 아는 사람**은 유효한 아카이브를 새로 만들 수 있다. HMAC은 "같은
   passphrase를 가진 사람이 만들었다"만 보장한다(대칭 키 — 서명자 식별 아님).
2. **롤백**: 같은 passphrase로 만든 *예전* 아카이브(파일+manifest 한 벌)로 통째로 되돌리는
   것은 막지 못한다. 외부 상태(최신 revision 기록 등)가 필요하다. `revision` 값은 manifest에
   남기므로 운영 절차에서 비교할 수 있다.
3. **기밀성 없음**: 파일과 manifest는 암호화하지 않는다. 데이터 유출 방지는 매체·폴더 권한의
   몫이다.
4. **인증 없는 아카이브(legacy, passphrase 미지정 export)**를 사용자가 확인하고 가져오면,
   그 순간의 폴더 내용을 그대로 믿는다.
5. passphrase 강도는 사용자 몫이다. PBKDF2-SHA256 600,000회로 오프라인 추측 비용을 올릴 뿐이다.
6. 대상 PostgreSQL 연결 보안(H-05 TLS)은 이 문서 범위가 아니다.
7. **다운그레이드는 사용자의 기억에 기대어서만 막힌다.** 공격자가 `auth`를 지우고 `version`을
   2로 바꾸고 파일과 checksum을 함께 바꾸면, 그 아카이브는 1.2.7 이하 legacy와 바이트 수준에서
   구분되지 않는다. 사용자가 "passphrase로 export했다"고 답하면(passphrase 입력) 거부되지만,
   비워 두고 legacy 확인을 누르면 가져온다. legacy 확인은 "차단"이 아니라 "명시적 확인 + 경고"다.
   같은 PC에서의 다운그레이드·롤백을 기계적으로 막으려면 로컬 이력 DB에 export 경로별
   salt·revision을 남기는 외부 상태가 필요하다(미구현, 후속 과제).
   export 쪽도 같다: 인증 없는 기존 폴더에 passphrase로 이어 쓰려면 "채택" 확인이 필요할 뿐,
   auth가 지워진 폴더인지 판별하지 못한다. 채택 확인 창은 새 폴더 export를 권장한다.

## 2. manifest 인증 형식

```json
"auth": {
  "scheme": "hmac-sha256",
  "kdf": "pbkdf2-sha256",
  "iterations": 600000,
  "salt": "<16바이트 hex, 아카이브마다 무작위>",
  "key_check": "<HMAC(key, 'key-check') 앞 16바이트 hex>",
  "mac": "<64자 hex>"
}
```

- 키: `PBKDF2-HMAC-SHA256(NFC(passphrase), salt, iterations, 32바이트)`.
  passphrase는 유니코드 NFC로 정규화한다(한글 조합형/완성형 입력 차이 흡수).
- MAC 입력: 도메인 구분자 `psql93-migration-archive/manifest/hmac-sha256/v1\0` +
  manifest 전체에서 `auth.mac`만 뺀 **정규화 JSON**(`sort_keys`, `separators=(",", ":")`,
  `ensure_ascii=False`). 인증 파라미터(salt·iterations·key_check)와 알 수 없는 필드도 MAC에
  포함되므로 어떤 필드를 바꾸거나 더해도 검증에 실패한다. 공백·키 순서만 다른 재직렬화는 통과한다.
- `key_check`는 "passphrase 오타"와 "변조"를 구분해 안내하기 위한 값이다(MAC과 같은 추측 저항).
- 비교는 모두 `hmac.compare_digest`.
- **기계별 키를 쓰지 않는다.** 키는 passphrase와 manifest 안의 salt로만 정해지므로 다른 PC·경로로
  옮긴 아카이브도 같은 passphrase로 검증된다.
- passphrase는 manifest·프로필·로그 어디에도 저장하지 않는다. 실행(재개 포함)마다 다시 묻는다.

## 3. 정책

### Export (PostgreSQL → 파일)

| 폴더 상태 | passphrase 입력 | 동작 |
|---|---|---|
| 새 폴더 | 있음 | 서명된 manifest 생성 |
| 새 폴더 | 없음 | 인증 없이 저장 + 작업 로그 WARNING (import 때 확인 필요) |
| 서명된 아카이브 | 같은 passphrase | 디스크 MAC 검증 뒤 병합·재서명 |
| 서명된 아카이브 | 없음 / 다른 passphrase | **거부** (다운그레이드·세탁 방지) |
| 인증 없는 기존 아카이브(파티션 있음) | 있음 | 명시적 "채택" 확인 필요. 확인하면 WARNING 후 기존 항목까지 서명 |

- 새로 기록하는 파티션 항목은 `checksum_sha256`(64자 hex)이 **필수**다. 없으면 저장 자체가
  실패한다(`ValueError`). checksum은 임시 파일에서 계산하고, 최종 경로 교체와 manifest 기록은
  같은 잠금 안에서 한다(`ArchiveManifestStore.commit_partition`).
- 저장할 때 디스크 manifest가 서명돼 있으면 **먼저 MAC을 검증**한다. 변조된 디스크 내용을
  병합해 새 MAC으로 "세탁"하지 않는다.

### Import (파일 → PostgreSQL)

1. manifest 로드. passphrase가 있으면 서명된 manifest의 MAC을 검증한다. MAC 실패는 백업
   (`manifest.json.bak`)으로 넘어가지 않는다(JSON이 깨진 경우에만 백업 사용).
2. 신뢰 판정(`require_trusted`) — **대상 DB에 연결하기 전**:
   - 서명 + 검증됨 → 진행
   - 서명됐는데 passphrase 없음 → 거부(확인 플래그로도 우회 불가)
   - 인증 없음 + passphrase 입력 → **거부**(다운그레이드 의심, 확인 플래그로도 우회 불가)
   - 인증 없음 + passphrase 없음(legacy·passphrase 미지정 export·사용자가 비워 둔 auth 제거)
     → `allow_legacy_unverified` 명시 확인이 있을 때만 진행, 경고를 작업 로그에 남긴다
3. **사전 검증**: 선택한 모든 파티션 파일의 SHA-256을 manifest와 대조한다. 하나라도 틀리면
   대상 DB를 건드리기 전에 전체 거부(`skip_on_error`면 경고 후 해당 파티션만 개별 실패).
4. 파티션마다 파일을 한 번 열어 같은 핸들로 다시 검증한 뒤 그대로 COPY(TOCTOU 방어),
   커밋 전 대상 `COUNT(*)` == manifest `row_count` 확인.
5. checksum이 없는 항목은 명시적 확인이 있을 때만 크기·행 수 검증으로 진행하고 파티션마다
   WARNING을 남긴다.

사전 검증 때문에 import는 파일을 두 번 읽는다(사전 1회 + 적재 직전 1회). 파티션당 수백 MB 수준에서
DB 적재 시간보다 작다.

### 기존 아카이브(1.2.7 이하, 48건) 하위 호환

- 1.2.7 이하 export는 checksum은 있지만 `auth`가 없다(version 2).
- 가져올 때 UI가 먼저 "export 때 passphrase를 지정했나"를 묻는다(legacy는 비워 둔다).
  이어서 "인증 없는 아카이브 가져오기" 확인을 받는다. 확인하면 진행하고, 거부하면
  시작하지 않는다. 조용히 통과하는 경로는 없다.
- 계속 쓸 아카이브라면 **새 폴더로 passphrase를 지정해 다시 export** 하는 것을 권장한다.
  기존 폴더를 "채택"해 서명할 수도 있지만, 그 시점의 폴더 내용을 그대로 믿는 것이다.
- 1.2.7 이하 앱은 version 3 manifest를 읽지 않는다(서명을 검사하지 못하는 구버전이 서명된
  아카이브를 가져가는 우회로 차단). 새 앱이 기존 폴더에 이어 export하면 그 폴더는 version 3이 된다.
  **구버전과 새 버전을 같은 아카이브 폴더에 섞어 쓰지 않는다.**

## 4. 동시 저장(M-02)

- 잠금: `<archive>/.manifest.lock` (POSIX `flock`, Windows `msvcrt.locking` 60초 대기).
  스레드·프로세스 모두 직렬화된다.
- 잠금 안에서 디스크 최신본을 다시 읽고 **3-way 병합**한다
  (base = 이 writer가 마지막으로 읽은/쓴 스냅샷, mine = 메모리, theirs = 디스크).
  - 이 writer가 바꾸지 않은 항목 → 디스크 값 유지(예전 버그: 오래된 메모리 값이 최신 디스크를 덮음)
  - 바꾼 파티션 항목 → 디스크 `entry_version`과 내용이 base와 같을 때만 `entry_version + 1`로 기록
  - 양쪽이 서로 다르게 바꿈 → `ManifestConflictError` (디스크는 그대로)
  - 같은 값으로 바꿈 → 충돌 아님
  - `parent_tables`는 키 단위 내용 3-way, `source`/`target`은 바꾼 쪽 값
- 기록은 임시 파일 → `fsync` → `os.replace`(Windows는 읽기 경합 시 짧게 재시도)로 원자적이다.
- 디스크 manifest가 깨져 있으면 백업으로 병합하고, 둘 다 못 읽으면 **덮어쓰지 않고 실패**한다
  (예전에는 예외를 삼키고 메모리 값으로 덮어 다른 writer 항목을 잃었다).
- 백업은 항상 **한 번 전** 저장본이다. 그래서 백업으로 병합할 때는 디스크가 이 writer가 이미 본
  것보다 뒤처질 수 있다. `entry_version`은 단조 증가하고 항목을 지우는 경로가 없으므로,
  base에 있던 항목이 디스크에 없거나 디스크 version이 base보다 낮으면 디스크를 **오래된 사본**으로
  보고 이 writer가 아는 값을 되살린다(WARNING). 그 항목을 이 writer가 다시 바꾸는 경우도
  충돌이 아니라 base 기준으로 +1 한다. `parent_tables`는 version이 없어 "없어진 키"만 되살린다.
  `revision`도 `max(디스크, base) + 1`로 뒤로 가지 않는다.
  한계: 백업 이후 **다른 writer**가 기록한 항목은 이 writer의 메모리에 없으므로 되살릴 수 없다
  (데이터 파일은 남고, 재개하면 다시 export된다).
- 저장이 끝나면 호출자의 메모리 manifest는 기록된 최신본으로 바뀐다.
- export 도중 같은 파티션을 다른 작업이 먼저 기록하면 그 파티션은 `failed`(재개 가능)로 끝나고,
  디스크의 파일과 manifest는 먼저 기록한 쪽 것으로 서로 일치한다.

## 5. 테스트

| 파일 | 내용 |
|---|---|
| `tests/core/test_archive_manifest_cas.py` | stale writer lost update 재현, 같은 항목 충돌, 동일 값 비충돌, version 증가, 깨진 manifest 비덮어쓰기, 한 번 늦은 백업 폴백에서 항목·version·revision·parent_table 비후퇴, `commit_partition` CAS, 스레드 4개·프로세스 3개 경쟁 |
| `tests/core/test_archive_manifest_auth.py` | 서명·검증, 잘못된 passphrase, 필드별 변조, 파일+checksum 동시 변조, 재직렬화, 다른 경로 이동, NFC/NFD, 백업 비폴백, auth 제거(다운그레이드: passphrase 입력 시 거부, 미입력 시 확인 필요), legacy 확인 플래그, checksum 필수, 다운그레이드·세탁·채택 |
| `tests/core/test_file_archive_security.py` | import 신뢰 판정이 대상 연결보다 먼저, legacy 확인·경고, 검증된 manifest로 DDL, checksum 없는 항목, 사전 검증, export 서명·경고·경쟁 |
| `tests/ui/dialogs/test_archive_security_prompt.py` | 실행 전 확인 대화 판단 로직(인증 없는 import 경로의 passphrase 질문·다운그레이드 거부 포함) |
