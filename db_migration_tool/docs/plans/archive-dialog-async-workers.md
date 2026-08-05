# 파일 아카이브 다이얼로그 — 동기 DB 작업의 워커 스레드 전환 계획

- **작성일**: 2026-08-05
- **대상**: `src/ui/dialogs/file_archive_migration_dialog.py`
- **상태**: 계획 (미착수)
- **선행 조건**: 없음. 단, 진행 중인 SQLAlchemy 2.0 마이그레이션이 끝난 뒤 시작할 것을 권장

> 줄 번호는 작업 트리가 바뀌면 어긋난다. 이 문서는 **심볼 이름**을 기준으로 쓴다.

---

## 1. 요약

파일 아카이브 다이얼로그의 DB/파일 작업 3개가 GUI 스레드에서 동기 실행되어 창이 얼어붙는다.
이를 QThread 워커로 옮긴다.

| 대상 메서드 | 하는 일 | 현재 최악 지연 |
|---|---|---|
| `check_connections` | 소스·대상 엔드포인트 확인 | 약 10초 (5초 × 2, 직렬) |
| `discover_partitions` | 파티션 탐색 | **상한 없음** (파티션당 `COUNT(*)` 전수 스캔) |
| `check_target_completed` | 대상 완료 여부 확인 | **상한 없음** (아카이브 전체 SHA-256, 또는 파티션당 2회 왕복) |

**그러나 이 작업의 본질은 "빠르게 만들기"가 아니다.**
현재는 이벤트 루프가 막혀 있어서 *우연히* 막혀 있던 데이터 손상 경로가, 비동기로 바꾸는 순간 열린다.
따라서 **안전 게이트를 먼저 세우고 나서** 비동기화한다. 순서를 바꾸면 안 된다.

---

## 2. 핵심 위험 — 반드시 먼저 이해할 것

### 2.1 데이터 손상 경로 (이 계획의 존재 이유)

```
대상 확인이 실패하거나 아직 안 끝남
  → _target_has_data == {}
  → _fill_partition_items 가 completed_like=False 로 판정
  → 전 파티션이 Qt.Checked
  → _update_nav_state 의 has_target 이 True → '다음' 활성
  → go_next() 가 _frozen_selection 에 전부 고정
  → start_migration → FileToPostgresArchiveWorker
  → _prepare_target_table 이 truncate_mode="auto"
  → 이미 완료된 대상 파티션까지 TRUNCATE 후 재적재
```

마지막 단계는 커밋 `14b1f07`에서 확인 팝업이 제거되어 **사용자에게 묻지 않는다.**

**지금 이걸 막고 있는 것**: `_busy()` 컨텍스트매니저가 `processEvents()` 직전에 `next_btn`을 잠근다.
즉 방어가 "이벤트 루프가 막혀 있다"는 사실에 의존한다. **비동기화하면 사라진다.**

회귀 테스트가 이미 존재한다 — `tests/ui/dialogs/test_dialog_safety.py::TestArchiveBusyGuard`.
`_busy()`를 걷어낼 때 이 테스트를 **삭제하지 말고 이관**해야 한다.

### 2.2 QThread 참조를 놓치면 프로세스가 죽는다

PySide6 6.10.2 / 이 환경에서 실측: 실행 중인 QThread의 마지막 파이썬 참조를 버리고 GC가 돌면
**프로세스 즉사(exit 127)**. 참조를 유지하고 `wait()`하면 정상 종료.

현실적 발생 경로는 "지역변수로 만들기"보다 **"이전 워커가 도는 중에 멤버를 재할당하기"**다.

### 2.3 현재 저장소에 워커 정리 코드가 하나도 없다

`src/ui/` 전체에 `wait()` / `quit()` / `requestInterruption()` 호출 **0건**.
마법사의 `closeEvent`는 *마이그레이션 실행 중*만 막고 탐색·확인·검증 워커는 그대로 두고 닫는다.

다이얼로그가 `parent`를 갖기 때문에(`main_window.start_migration`) C++ 객체는 살아남는다.
→ 크래시 대신 **좀비 다이얼로그**가 되어, 창을 닫은 뒤 워커 오류가 메인 창 위에 맥락 없이 뜬다.

---

## 3. 설계 결정

### D1. 마법사 패턴은 **참조**하되 **복제하지 않는다**

마법사에 이미 같은 워커 3종이 있다(`PartitionDiscoveryWorker`, `TargetCompletedCheckWorker`,
`RowCountVerificationWorker`). 하지만 다음 결함을 함께 갖고 있다:

| 마법사의 결함 | 복제 시 결과 |
|---|---|
| `finished.connect(lambda: ...)` | 람다는 수신자 QObject가 없어 자동 해제가 안 됨 → 다이얼로그 파괴 시 `RuntimeError` |
| `_on_discovery_error`가 `check_completed_btn`을 복구하지 않음 | **탐색 1회 실패 → '완료여부 확인' 영구 비활성** |
| 커넥션 `close()`가 `finally` 밖 | 루프 중 예외 시 커넥션 누수 |
| 취소 플래그 없음 | 파티션 수천 개면 끝날 때까지 못 멈춤 |
| `_is_running()`이 보조 워커를 모름 | 탐색 중 창이 닫힘 |
| 오래된 결과 무효화 없음 | 조건 바꿔도 이전 결과가 화면을 덮음 |

**결정**: 워커를 `src/core/scan_workers.py`(신규)로 분리해 **양쪽 다이얼로그가 공유**한다.
마법사가 워커를 다이얼로그 안에 정의한 것이 애초에 아카이브 쪽 중복 구현을 낳은 원인이다.

### D2. 모든 결과 시그널의 첫 인자는 **generation**

```python
result   = Signal(int, object)   # (generation, payload)
failed   = Signal(int, str)
progress = Signal(int, int, int) # (generation, done, total)
```

모든 슬롯의 첫 줄은 `if gen != self._scan_gen: return`.

**gen을 올리는 지점**: 탐색/확인 시작, 날짜 변경, 항목 체크박스 변경, 재개/새작업 전환
**gen을 올리지 않는 지점**: `partition_filter.textChanged` — 이름 필터는 표시만 거르고
선택 상태를 안 건드린다. 여기서 무효화하면 필터 칠 때마다 재탐색을 강요당한다.

**이 결정의 최대 실익은 테스트다.** 슬롯이 `(gen, payload)`를 받는 순수 함수에 가까워지므로
위험 로직의 90%를 **QThread 없이 직접 호출로 검증**할 수 있다.

### D3. `_selection_verified` 게이트 — 새 안전 불변식

> **대상 확인이 현재 목록에 대해 끝나지 않았으면
> `next_btn`은 꺼져 있고 `go_next()`는 `_frozen_selection`을 굳히지 않는다.**

```python
# _update_nav_state
has_target = bool(self.resume_mode and self._frozen_selection) or bool(self.get_selected_partition_names())
self.next_btn.setEnabled(has_target and (self.resume_mode or self._selection_verified))
```

`go_next()` 내부에도 **같은 가드를 중복으로** 둔다. 버튼 상태만 믿으면 안 된다 —
큐에 남은 클릭이 배달될 수 있다(그게 애초에 `_busy`가 존재한 이유다).

**확인 실패 시 `_selection_verified`를 True로 올리지 않는다.**
현재 코드는 `FileNotFoundError`를 잡아 전부 False로 놓고 "대상 확인 완료"라고 표시한다.
방향은 옳지만(False = 재실행 = 안전한 쪽), **"전부 미완료" = "전부 TRUNCATE 후 재적재"**이므로
화면이 "완료"라고 말하면 안 된다.

### D4. 닫기 정책을 2층으로 나눈다

```python
_is_running()      # 마이그레이션 실행 중 → 닫기 불가 (현행 유지)
_has_active_scan() # 탐색/확인 중 → 닫기 허용하되 워커를 먼저 정리
```

탐색은 파괴적이지 않으므로 사용자에게 물어볼 것이 없다.
`_is_running()`에 탐색 워커를 넣으면 "수 분간 닫을 수 없는 창"이 된다 — **하지 말 것.**

### D5. `finished`를 재정의하지 않는다

`BaseMigrationWorker`는 `finished = Signal()`로 `QThread.finished`를 가린다(작업 종료 ≠ 스레드 종료).
신규 워커는 **`result`/`failed`/`progress`만 정의**하고, 정리 로직(버튼 복구, `deleteLater`,
inflight 해제)은 **`QThread.finished`에 붙인다.**

그러면 result가 왔든 failed가 왔든 정리가 정확히 한 번 돈다 →
"탐색 실패 후 버튼이 영구 비활성"(마법사의 실제 버그)이 구조적으로 불가능해진다.

---

## 4. 권장 아키텍처

### 4.1 신규 모듈 `src/core/scan_workers.py`

```python
class ScanWorker(QThread):
    result   = Signal(int, object)
    failed   = Signal(int, str)
    progress = Signal(int, int, int)
    # finished 는 재정의하지 않는다 (D5)

    def __init__(self, generation: int): ...   # dict/list/date/bool 원시값만 받는다
    def cancel_query(self): ...                # conn.cancel() — 다른 스레드에서 호출 가능
    def _should_stop(self) -> bool: ...        # isInterruptionRequested() 래퍼

class ConnectionCheckWorker(ScanWorker)
class PartitionScanWorker(ScanWorker)
class TargetCompletedScanWorker(ScanWorker)
```

**생성자 인자 규약 (정적 테스트로 강제)**
- 넘기는 것: `dict` / `list` / `date` / `str` / `bool`
- **넘기지 않는 것**: 위젯, `ConnectionProfile`, `HistoryManager`, `CheckpointManager`, 커넥션/커서

근거: 옮기는 3개 메서드는 **SQLite를 전혀 건드리지 않는다.** 로컬 DB를 만지는 곳
(`_check_incomplete_migration`, `_on_resume_clicked`, `start_migration`, `on_worker_finished`, `on_error`)은
전부 GUI 스레드에 남는다. 이 성질을 불변식으로 못 박아 두면 나중에 깨지지 않는다.

### 4.2 다이얼로그 측 — 상태 전이를 5개 메서드로 집중

```python
self._scan_gen: int = 0
self._scan_workers: dict[str, ScanWorker] = {}   # "conn" | "discover" | "target"
self._inflight: set[str] = set()
self._selection_verified: bool = False

_bump_generation()    # gen++ , _selection_verified=False, 목록 회색 처리, nav 갱신
_start_scan(kind, w)  # 실행중 가드 → isRunning 가드 → 멤버 보관 → 바인드 메서드 연결 → start
_finish_scan(kind)    # QThread.finished 에 연결. inflight 해제 + deleteLater + _set_scan_state
_set_scan_state()     # 탐색 관련 버튼 활성화를 여기서만 결정
_shutdown_scans(ms)   # requestInterruption + cancel_query + wait
```

`_set_scan_state()`는 `src/ui/CLAUDE.md`가 이미 규정한 원칙
("버튼 활성화는 각 핸들러가 아니라 한 곳에서")을 탐색 상태에도 적용한 것이다.

**시그널 연결에 람다 금지.** 전부 바인드 메서드(`self._on_...`)로. 테스트에서 직접 호출 가능해진다.

### 4.3 코어 측 최소 변경 (전부 키워드 전용, 기본값 `None` → 기존 호출부 무영향)

| 파일 | 변경 |
|---|---|
| `partition_discovery.py` | `_create_connection`에 `connect_timeout=10` |
| `partition_discovery.py` | `discover_partitions(..., *, should_stop=None, on_progress=None)` |
| `archive_manifest.py` | `compute_file_metadata(path, *, should_stop=None, on_progress=None)` |
| `archive_manifest.py` | `get_completed_status(names, *, should_stop=None, on_progress=None)` |
| 다이얼로그 → 워커 이동분 | `conn_params["connect_timeout"] = 10`, `conn.autocommit = True` |

**체크섬을 크기 비교로 대체하지 말 것.** 빨라지지만 `✓`(완료) 오탐을 만들고,
오탐은 사용자가 그 파티션을 건너뛰게 해서 **손상된 데이터가 조용히 남는다.**
체크섬은 유지하고 ① 취소 가능하게 ② `progress`로 "3/47 파티션, 2.1GB 확인 중"을 보여준다.

### 4.4 앱 수명 훅

- `main_window.start_migration` — `dialog.exec()` 뒤 `dialog.deleteLater()`
  (현재 다이얼로그가 MainWindow 자식으로 계속 누적된다)
- `main.py` — `app.aboutToQuit.connect(...)`로 전역 워커 정리
- `tray_icon.set_migration_running()`이 **호출되지 않는 死 코드**다. 실제로 연결하고
  판정에 탐색 워커도 포함시킨다. 안 그러면 종료 확인 없이 `app.quit()` → §2.2 크래시

### 4.5 취소 설계

취소가 안 먹는 구간 3곳:

| 구간 | 왜 안 먹나 | 대응 |
|---|---|---|
| 커넥션 수립 | `connect_timeout` 부재 → 방화벽 드롭 시 수십 초~2분 | `connect_timeout=10` |
| 단일 장기 쿼리 | `COUNT(*)` 전수 스캔 중엔 루프 플래그를 못 읽음 | `conn.cancel()` |
| 파일 SHA-256 | 1MB 청크 루프에 취소 훅 없음 | `should_stop` 콜백 |

**`terminate()` 절대 금지** — 이 코드베이스에 한정한 구체적 피해:
1. psycopg 커넥션 미종료 → PostgreSQL 9.3의 `max_connections` 잠식
2. `.manifest.lock` 핸들 잔류 → 이후 export의 `save()`가 약 10초 블록 후 실패
3. `.{name}.{uuid}.tmp` 파일이 아카이브 디렉터리에 누적
4. GIL 보유 중 종료 시 인터프리터 영구 정지

`wait()`는 짧게(300ms) 반복하고 사이에 "취소 중… (최대 N초)"를 갱신한다.

---

## 5. 단계별 실행 계획

**한꺼번에 바꾸지 않는다.** `_busy()` 제거는 세 곳의 안전 장치를 동시에 걷어낸다.
하나씩 옮기면 `_busy()`를 남은 동기 경로에 계속 쓸 수 있다.

### 0단계 — 공통 인프라 (동작 변화 없음, 별도 커밋)

- `_scan_gen`, `_scan_workers`, `_inflight`, `_selection_verified`
- `_bump_generation`, `_start_scan`, `_finish_scan`, `_set_scan_state`, `_shutdown_scans`
- `_has_active_scan`, `closeEvent`/`reject` 확장
- `_update_nav_state` + `go_next`에 **D3 게이트**
- `main_window`의 `deleteLater`, `app.aboutToQuit` 훅

**이 단계에서 D3 불변식 테스트를 먼저 쓰고, 1~3단계 내내 초록으로 유지한다.**

### 1단계 — `check_connections`

- **먼저 하는 이유**: 결과가 `bool×2 + str×2`뿐이고 후속 상태(`_frozen_selection`, 목록)에 영향이 없다.
  실패해도 최악이 "램프가 안 바뀐다".
- **주의**: `__init__`의 `QTimer.singleShot(0, self.check_connections)`로 창이 뜨자마자 시작된다
  → 수명 문제를 가장 먼저 만난다. 인프라 실전 검증에 적합.
- **gen이 필요한 이유**: `source_status_message`/`target_status_message`가
  `create_history(source_status=..., target_status=...)`로 **이력에 기록**된다.
  stale 결과가 덮으면 이력에 잘못된 연결 상태가 남는다.
- **진입점 이름을 바꾸지 말 것** → §6 참조
- 롤백: 커밋 1개 revert

### 2단계 — `check_target_completed`

- **3단계보다 먼저 하는 이유**:
  ① 입력(`names`)이 확정된 순수 조회, 출력이 `_target_has_data` 하나 → 경계가 가장 깔끔
  ② 오늘 가장 오래 걸리는 구간 → 체감 이득 최대
  ③ `_selection_verified` 게이트가 여기서 완성 → §2.1 방어 확립
- 이 단계 전용: `get_completed_status` / `compute_file_metadata`에 `should_stop`·`on_progress`
- 롤백: `_target_has_data`를 채우는 경로 하나만

### 3단계 — `discover_partitions`

- **마지막인 이유**: 목록 전체를 다시 그리고 **2단계를 체인으로 호출**한다.
  순서를 뒤집으면 "비동기 탐색 → 동기 확인"이 되어 탐색 직후 UI가 다시 얼어붙는 중간 상태가 생긴다.
- 체인 방식: **슬롯 체이닝** — `_on_discovery_result(gen, rows)` 안에서 gen 검증 후
  `_start_target_check(gen)`. **generation을 그대로 물려준다.**
  (단일 워커 2단계 방식은 이 코드베이스에서 이점이 없다 — 소스와 대상이 다른 엔드포인트라
   재사용할 커넥션 자체가 없다)
- 이 단계 전용: `PartitionDiscovery`의 `connect_timeout`, `should_stop`·`on_progress`,
  `_get_row_count`의 `COUNT(*)` 비용 재검토(별도 이슈로 분리 가능)
- 탐색 **요청 즉시** `discovered_partitions = []`, `partition_list.clear()`, `_target_has_data = {}`
  (현재는 결과가 나온 뒤에야 갈아끼우므로, 비동기에서는 탐색 중 이전 결과가 체크된 채 남는다)

---

## 6. 테스트 계획

### 6.1 기존 테스트를 깨뜨리지 않는 조건 ⚠️

UI 테스트 12개가 `check_connections`를 `lambda self: None`으로 패치해 생성자를 통과시킨다
(`test_dialog_safety.py`, `test_run_state_machine.py`의 픽스처).
`__init__`의 `QTimer.singleShot(0, self.check_connections)`가 **패치된 bound method를 캡처**하기
때문에 이후 이벤트 루프가 돌아도 실 DB에 붙지 않는다.

> **진입점 이름을 바꾸거나 `__init__`에서 직접 워커를 `start()`하면 UI 테스트 67개가
> 전부 실 DB 접속을 시도한다.** `check_connections`라는 이름과 호출 위치를 유지할 것.

### 6.2 실 DB 없이 검증 가능한 범위

| 항목 | 판정 | 방법 |
|---|---|---|
| UI 상태 전이 (램프·버튼·문구) | **가능** | 슬롯 직접 호출 |
| 중복 실행 방지 | **가능** | 워커 클래스 patch → `isRunning=True` |
| 오래된 결과 무효화 | **가능** | gen을 올린 뒤 옛 토큰으로 슬롯 호출 |
| 닫기 중 워커 정리 | **가능** | MagicMock 워커 + `requestInterruption`/`wait` assert |
| 파일 아카이브 경로 전체 | **모킹 불필요** | `tmp_path`에 실제 manifest 생성 |
| 워커 내부 SQL의 정확성 | **불가** | → 아래 보완책 |

**핵심**: `(gen, payload)` 슬롯 시그니처 덕에 **데이터를 파괴할 수 있는 로직(§2.1)은
실 DB 없이 100% 검증된다.** 실 DB가 필요한 건 `conn.cancel()`의 실제 효과와
`connect_timeout` 실동작뿐이고, 그건 **데이터를 파괴하지 않는 종류의 실패**다.
이 비대칭이 위 설계를 고른 이유다.

**SQL 정확성 보완책**: 이번 리팩터링에서 **SQL 문자열을 바꾸지 않는다.**
본문을 워커로 그대로 이동만 하고, 그 사실을 테스트로 고정한다 —
실행된 SQL에 `information_schema.tables`와 `LIMIT 1`이 있고 **`COUNT(*)`는 없어야 한다**
(성능 회귀 방어).

### 6.3 신규 테스트 파일

`tests/ui/dialogs/test_archive_async_workers.py`

| 클래스 | 내용 | 스레드 |
|---|---|---|
| `TestArchiveWorkerLogic` | `run()` 직접 호출 — 결과/예외→시그널 매핑, SQL 형태 고정 | ✗ |
| `TestArchiveWorkerThreading` | 배선 스모크 2~3건 (`qtbot.waitSignal`) | ✓ |
| `TestArchiveAsyncUiState` | 버튼/램프/문구 전이, 실패 후 막다른 상태 없음 | ✗ |
| `TestArchiveDuplicateGuard` | 연타, 동시 실행 금지 | ✗ |
| `TestArchiveStaleResults` | gen 무효화 (양방향) | ✗ |
| `TestArchiveWorkerCleanup` | 닫기/Esc 시 정리, 지각 결과 무해 | 일부 ✓ |

**`TestArchiveBusyGuard` 이관**: `_busy()` 제거 시 삭제가 아니라 이관.
같은 불변식("탐색 중 next/back/close 잠김", "끝나면 규칙대로 복원")을 비동기 경로에서 검증.

**정적 테스트** (이 저장소의 기존 스타일 — `inspect.getsource` 사용):
- 워커 연결 구간에 `lambda`가 없음
- 워커 생성자 인자에 위젯/Manager 타입이 없음
- 다이얼로그에 `processEvents`가 없음

### 6.4 스레드 테스트 flaky 방지

- `time.sleep()` 금지. `qtbot.waitSignal(sig, timeout=3000)` / `waitUntil`만
- **`start()`와 `waitSignal` 사이에 `processEvents()`를 끼우지 말 것** — 배달이 끝나버려 타임아웃한다
  (워커→다이얼로그는 Queued 연결이라 메인 루프가 돌기 전엔 슬롯이 안 불린다)
- **테스트 종료 전 반드시 조인**: `qtbot.waitUntil(lambda: not worker.isRunning(), 5000)`.
  안 하면 무관한 다음 테스트가 깨진다 (가장 흔한 flaky 원인)
- 워커 소요 시간은 `threading.Event`로 제어

---

## 7. 이관 체크리스트 (놓치면 버그가 되는 항목)

- [ ] 탐색 시작 시 `_target_has_data = {}` — 안 비우면 이전 탐색의 `✓`가 새 목록에 유령으로 남음
- [ ] `_render_partition_list`가 체크 상태를 초기화한다 → 비동기에서는 "사용자가 선택하는 도중
      결과 도착"으로 수동 체크가 날아간다. 재렌더 시점을 gen으로 통제할 것
- [ ] `discover_status` 문구 덮어쓰기 순서 — 현재 `"완료: N개"`가 `"대상 확인 완료"`를 덮는다.
      두 워커로 쪼개면 최종 문구를 2번째 슬롯이 정하도록 옮길 것
- [ ] nav 복구는 **성공 슬롯이 아니라 `QThread.finished`**에 (같은 유형의 버그를 2026-08-05에 수정함)
- [ ] `source_status_message` / `target_status_message`는 **이력에 기록**된다. 반영 누락 시 "확인 전"으로 남음
- [ ] `partition_filter.text()`는 **hoist 금지** — 결과 적용 시점에 메인 스레드에서 다시 읽어야 함
- [ ] `QMessageBox`는 메인 스레드 사전 검증에만. 결과 슬롯에서 띄우면 좀비 다이얼로그가 메인 창 위에 팝업
- [ ] psycopg3 키 이름은 `dbname` (마법사 워커는 psycopg2의 `database` — 복사 시 충돌)
- [ ] `conn.autocommit = True` 추가 (현재 루프 전체가 단일 트랜잭션 → 대상 DB에 idle-in-transaction 유지)
- [ ] 워커 멤버 재할당 전 `isRunning()` 검사 (§2.2)
- [ ] `_frozen_selection` 확정 후 도착하는 지각 결과 차단 (gen 또는 `run_state != "idle"` 가드)
- [ ] `profile.*_config`에 평문 password 포함 — 로그/예외 메시지에 dict를 통째로 찍지 말 것

---

## 8. 실 DB 수동 체크리스트 (전환 후 1회)

- [ ] 파티션 수천 개 범위로 "파티션 찾기" — 탐색 중 창 이동/리사이즈가 되는가
- [ ] 탐색 중 필터 입력창 타이핑이 즉시 반응하는가
- [ ] 탐색 중 "닫기" → 앱이 죽지 않는가, 로그에 `QThread: Destroyed while thread is still running`이 없는가
- [ ] 탐색 중 날짜를 바꾸고 다시 찾기 → 목록이 **새 날짜** 결과인가
- [ ] 대상 확인 실패 시 '다음'이 잠기는가 (§2.1 방어 확인)
- [ ] 연결 안 되는 프로필로 열기 → 램프가 `busy`에서 멈추지 않고 `error`로 가는가
- [ ] `file→postgres`, `postgres→file` **양방향** 확인 (분기가 다름)
- [ ] 취소 후 `pg_stat_activity`에 세션이 남지 않는가

---

## 9. 범위 밖 / 후속 과제

| 항목 | 비고 |
|---|---|
| `PartitionDiscovery._get_row_count`의 `COUNT(*)` 전수 스캔 | 탐색 지연의 지배적 원인. 별도 이슈로 분리 |
| 마법사(`migration_wizard_dialog.py`)의 동일 결함 수정 | `scan_workers.py` 공유 후 마법사도 이관하면 자연히 해결 |
| 아카이브 워커의 `pause()` 무효 | `_check_pause()` 호출이 없는데 일시정지 버튼이 존재. 별도 수정 |
| 아카이브 다이얼로그의 파티션 표시 상한 부재 | 마법사는 5,000. 수만 개 렌더 시 별도 문제 |
| 로그 보존/정리 정책 부재 | 무관하지만 미해결 |

---

## 10. 예상 규모

| 단계 | 신규/수정 | 테스트 | 롤백 단위 |
|---|---|---|---|
| 0 | 다이얼로그 +약 120줄, `main_window`/`main.py` 각 1~2줄 | D3 불변식 5~6건 | 커밋 1개 |
| 1 | `scan_workers.py` 신규 + 다이얼로그 1개 메서드 | 8~10건 | 커밋 1개 |
| 2 | 워커 1종 + 코어 2개 시그니처 | 10~12건 | 커밋 1개 |
| 3 | 워커 1종 + 코어 1개 시그니처 + 체인 | 10~12건 | 커밋 1개 |

각 단계는 **독립적으로 revert 가능**해야 한다. 그게 이 순서를 고른 이유다.
