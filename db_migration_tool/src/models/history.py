"""
마이그레이션 이력 모델 및 관리자

이력은 만들어질 때의 계획에 묶인다(H-08/H-09, 설계: docs/plans/history-integrity-h08-h09-m12.md).

- 생성: `HistoryManager.create_planned_history()`가 이력과 계획된 모든 checkpoint를
  한 트랜잭션으로 만들고, 비밀을 뺀 endpoint 지문·migration mode·schema·planned
  set/count/hash·plan 지문을 불변으로 저장한다.
- 재개: `HistoryManager.prepare_resume()`가 저장된 계획의 무결성 → 현재 프로필과의
  identity 일치 → checkpoint 완전성을 차례로 확인한다. 누락 checkpoint는 계획대로
  보충하고, 계획 밖 checkpoint·계획 변조·identity 변경은 차단한다.
  비밀번호·SSL·호환 모드 변경은 identity가 아니므로 허용한다.
- legacy(계획 컬럼이 비어 있는 구버전 이력): 자동 재개하지 않는다. 기록된 날짜 범위와
  파티션 명명 규칙으로 기대 집합을 다시 계산해 남은 checkpoint와 비교한다(`LegacyCoverage`).
  사용자가 현재 연결이 원래 작업의 연결과 같다고 명시적으로 확인하면
  `adopt_legacy_history()`가 현재 identity와 (남은 checkpoint ∪ 범위 대비 누락 보충분)을
  계획으로 한 번 기록하고, 그 뒤로는 엄격 모드로 검사한다. 체크포인트가 있는 유형 안의
  누락을 보충하지 않으면 채택하지 않는다(`LegacyPlanGapError`) — 남은 일부만으로 계획을
  고정하면 구버전 H-09 누락이 '정상'이 되어 subset만 처리하고 completed로 닫힌다.
  구버전은 checkpoint를 유형 코드 순서(ED→PH→PS→RT→TH)로 만들었으므로, checkpoint가
  없는 **뒤 유형**은 끊겨서 통째로 빠졌을 수 있다. 그 유형들은 원래 작업에 포함됐는지
  호출자가 명시적으로 정해야 채택된다(`LegacyTypeDecisionError`).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from src.database.local_db import Checkpoint, MigrationHistory
from src.database.repository import (
    INCOMPLETE_STATUSES,
    CheckpointRepository,
    HistoryRepository,
)
from src.models.profile import ENDPOINT_KIND_FILE, normalize_endpoint_config

if TYPE_CHECKING:
    from src.models.profile import ConnectionProfile

__all__ = [
    "INCOMPLETE_STATUSES",
    "PLAN_VERSION",
    "DEFAULT_SCHEMA",
    "CheckpointItem",
    "CheckpointManager",
    "HistoryManager",
    "LegacyCoverage",
    "LegacyPlanGapError",
    "LegacyTypeDecisionError",
    "MigrationHistoryItem",
    "MigrationPlan",
    "ResumeCheck",
    "ResumeVerdict",
    "endpoint_fingerprint",
    "endpoint_identity",
    "endpoint_label",
    "legacy_range_candidates",
    "planned_set_hash",
]

# 계획 레코드 형식 버전. NULL이면 이 기능 이전(legacy) 이력이다.
PLAN_VERSION = 1

# 탐색·복사 대상 스키마. 파티션 탐색이 public으로 한정되어 있다.
DEFAULT_SCHEMA = "public"


def _sha256(domain: str, payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{domain}\n{body}".encode()).hexdigest()


def endpoint_identity(config: dict[str, Any] | None) -> dict[str, Any]:
    """endpoint를 식별하는 비밀 아닌 값만 정규화해 돌려준다.

    PostgreSQL: kind, host(소문자·공백 제거), port, database, username.
    File: kind, archive_path(정규화).
    password/ssl/compat_mode는 **identity가 아니다** — 바뀌어도 같은 대상이다.
    """
    cfg = normalize_endpoint_config(config)
    kind = str(cfg["kind"])
    if kind == ENDPOINT_KIND_FILE:
        raw = str(cfg.get("archive_path") or "").strip()
        return {
            "kind": kind,
            "archive_path": os.path.normcase(os.path.normpath(raw)) if raw else "",
        }
    port_raw = cfg.get("port")
    port: int | str
    try:
        port = int(port_raw) if port_raw not in (None, "") else 5432
    except (TypeError, ValueError):
        port = str(port_raw).strip()
    return {
        "kind": kind,
        "host": str(cfg.get("host") or "").strip().lower(),
        "port": port,
        "database": str(cfg.get("database") or "").strip(),
        "username": str(cfg.get("username") or "").strip(),
    }


def endpoint_fingerprint(config: dict[str, Any] | None) -> str:
    """`endpoint_identity()`의 SHA-256. 이력에는 이 값만 저장한다."""
    return _sha256("dbmig-endpoint/v1", endpoint_identity(config))


def endpoint_label(config: dict[str, Any] | None) -> str:
    """거부 안내에 보여 줄 표시 문자열. 비밀번호·사용자명은 넣지 않는다."""
    ident = endpoint_identity(config)
    if ident["kind"] == ENDPOINT_KIND_FILE:
        return f"file:{ident['archive_path']}"
    return f"{ident['host']}:{ident['port']}/{ident['database']}"


def planned_set_hash(partitions: list[str] | tuple[str, ...]) -> str:
    """planned set의 해시. 순서와 무관하다(정렬 후 계산)."""
    return _sha256("dbmig-plan-set/v1", sorted(set(partitions)))


@dataclass(frozen=True)
class MigrationPlan:
    """이력 생성 시점에 고정하는 계획. 생성 뒤에는 바뀌지 않는다."""

    migration_mode: str
    schema: str
    source_fingerprint: str
    target_fingerprint: str
    partitions: tuple[str, ...]  # 정렬·중복 없음
    endpoint_label: str = ""

    @classmethod
    def from_profile(
        cls,
        profile: ConnectionProfile,
        partitions: list[str] | tuple[str, ...],
        schema: str = DEFAULT_SCHEMA,
    ) -> MigrationPlan:
        names = [str(p) for p in partitions]
        if not names:
            raise ValueError("계획에 파티션이 없습니다")
        if any(not n.strip() for n in names):
            raise ValueError("빈 파티션 이름이 계획에 있습니다")
        if len(set(names)) != len(names):
            raise ValueError("계획에 같은 파티션이 두 번 이상 있습니다")
        return cls(
            migration_mode=profile.migration_mode,
            schema=schema,
            source_fingerprint=endpoint_fingerprint(profile.source_config),
            target_fingerprint=endpoint_fingerprint(profile.target_config),
            partitions=tuple(sorted(names)),
            endpoint_label=(
                f"{endpoint_label(profile.source_config)} → {endpoint_label(profile.target_config)}"
            ),
        )

    @property
    def planned_count(self) -> int:
        return len(self.partitions)

    @property
    def planned_hash(self) -> str:
        return planned_set_hash(self.partitions)

    @property
    def fingerprint(self) -> str:
        """identity + schema + planned set 전체를 묶은 지문."""
        return _sha256(
            "dbmig-plan/v1",
            {
                "version": PLAN_VERSION,
                "mode": self.migration_mode,
                "schema": self.schema,
                "source": self.source_fingerprint,
                "target": self.target_fingerprint,
                "count": self.planned_count,
                "set": self.planned_hash,
            },
        )

    def to_columns(self) -> dict[str, Any]:
        return {
            "plan_version": PLAN_VERSION,
            "migration_mode": self.migration_mode,
            "schema_name": self.schema,
            "source_fingerprint": self.source_fingerprint,
            "target_fingerprint": self.target_fingerprint,
            "endpoint_label": self.endpoint_label,
            "planned_partitions": json.dumps(list(self.partitions), ensure_ascii=False),
            "planned_count": self.planned_count,
            "planned_hash": self.planned_hash,
            "plan_fingerprint": self.fingerprint,
        }


def _parse_day(value: str | None) -> date | None:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def legacy_range_candidates(start: date, end: date) -> dict[str, list[str]]:
    """기록된 날짜 범위에서 명명 규칙상 있을 수 있는 파티션 이름(테이블 이름별, 정렬).

    daily: `<table>_YYMMDD`(범위의 모든 날), monthly: `<table>_YYMM`(범위와 겹치는 모든 달).
    탐색(`PartitionDiscovery`)의 겹침 규칙과 같다. 원본에 실제로 있는지는 모른다 —
    채택한 legacy 이력에서 원본(DB·아카이브 manifest)에 없는 파티션은 워커가 0건 완료로
    닫는다(copy·아카이브 워커 공통).
    """
    # 지연 import: src.core 패키지가 워커를 거쳐 이 모듈을 다시 import한다(순환).
    from src.core.table_types import TABLE_TYPE_CONFIG

    result: dict[str, list[str]] = {}
    for config in TABLE_TYPE_CONFIG.values():
        names: list[str] = []
        if config.partition_suffix == "daily":
            day = start
            while day <= end:
                names.append(f"{config.table_name}_{day:%y%m%d}")
                day += timedelta(days=1)
        else:
            year, month = start.year, start.month
            while (year, month) <= (end.year, end.month):
                names.append(f"{config.table_name}_{year % 100:02d}{month:02d}")
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        result[config.table_name] = sorted(names)
    return result


def legacy_creation_order() -> list[str]:
    """구버전이 checkpoint를 만든 유형 순서(테이블 이름).

    구버전 두 다이얼로그는 탐색 결과를 `(table_type_code, from_timestamp)`로 정렬한 목록을
    그대로 checkpoint로 만들었다(`PartitionDiscovery`, `ArchiveManifestStore`의 정렬).
    그래서 중간에 끊기면 남은 checkpoint는 이 순서의 **앞부분**이다.
    """
    from src.core.table_types import TABLE_TYPE_CONFIG

    return [
        config.table_name
        for _code, config in sorted(TABLE_TYPE_CONFIG.items(), key=lambda kv: kv[0].value)
    ]


def _type_codes() -> dict[str, str]:
    from src.core.table_types import TABLE_TYPE_CONFIG

    return {config.table_name: code.value for code, config in TABLE_TYPE_CONFIG.items()}


def _partition_table(name: str) -> str | None:
    """`<table>_<숫자 접미사>` 형식이면 테이블 이름을, 아니면 None."""
    from src.core.table_types import TABLE_TYPE_CONFIG

    base, _, suffix = name.rpartition("_")
    if not suffix.isdigit():
        return None
    for config in TABLE_TYPE_CONFIG.values():
        width = 6 if config.partition_suffix == "daily" else 4
        if config.table_name == base and len(suffix) == width:
            return base
    return None


def _shown(names: list[str], limit: int = 10) -> str:
    rest = len(names) - limit
    return ", ".join(names[:limit]) + (f" 외 {rest:,}개" if rest > 0 else "")


@dataclass
class LegacyCoverage:
    """legacy 이력의 남은 checkpoint가 기록된 범위를 얼마나 덮는지.

    구버전은 선택한 파티션을 유형 코드 순서(`legacy_creation_order`, ED→PH→PS→RT→TH)로
    checkpoint로 만들었다. 중간에 끊기면 남은 checkpoint는 그 순서의 앞부분이다.

    - `represented_types`: checkpoint가 하나라도 있는 유형 — 원래 선택된 것이 확실하다.
    - `excluded_types`: checkpoint가 없고 순서상 마지막 represented 유형보다 **앞**인 유형 —
      원래 선택했다면 checkpoint가 먼저 생겼어야 하므로 선택하지 않은 것이 확실하다. 경고하지
      않는다(선언에 넣으면 포함한다).
    - `trailing_types`: checkpoint가 없고 순서상 **뒤**인 유형의 범위 내 후보. 선택하지 않았을
      수도, 끊겨서 통째로 빠졌을 수도 있다 — 로컬 데이터로는 구분할 수 없다.
    - `declared_types`: 호출자(사용자)가 원래 작업에 포함됐다고 확인한 유형. None이면 아직
      정하지 않았다(`undecided_types`가 뒤 유형 전체). 선언된 checkpoint 없는 유형은
      `included_types`가 되어 기대 집합에 들어간다.
    - `gaps`: 기대 집합(represented ∪ included의 범위 내 후보) 중 checkpoint가 없는 것 —
      구버전 H-09 누락(또는 당시 사용자가 뺀 파티션). 채택하려면 반드시 보충해야 한다.
    - `absent_types`: 계획에 넣지 않는 뒤 유형의 후보(미결정이거나 사용자가 제외).
    - `outside`: 범위 밖이거나 명명 규칙에 맞지 않는 checkpoint. 계획에 그대로 둔다.
    """

    start_date: str
    end_date: str
    range_ok: bool
    present: list[str]
    completed_count: int
    range_days: int = 0
    represented_types: list[str] = field(default_factory=list)
    expected_count: int = 0
    gaps: list[str] = field(default_factory=list)
    absent_types: dict[str, list[str]] = field(default_factory=dict)
    outside: list[str] = field(default_factory=list)
    trailing_types: dict[str, list[str]] = field(default_factory=dict)
    excluded_types: list[str] = field(default_factory=list)
    declared_types: list[str] | None = None
    included_types: list[str] = field(default_factory=list)

    @classmethod
    def compute(
        cls,
        start_date: str | None,
        end_date: str | None,
        states: list[tuple[str, str | None]],
        original_types: Iterable[str] | None = None,
    ) -> LegacyCoverage:
        order = legacy_creation_order()
        declared: list[str] | None = None
        if original_types is not None:
            declared = sorted({str(t) for t in original_types})
            unknown = [t for t in declared if t not in order]
            if unknown:
                raise ValueError(f"알 수 없는 테이블 유형입니다: {', '.join(unknown)}")

        present = sorted({name for name, _ in states})
        completed = len({name for name, status in states if status == "completed"})
        start, end = _parse_day(start_date), _parse_day(end_date)
        base = cls(
            start_date=str(start_date or ""),
            end_date=str(end_date or ""),
            range_ok=False,
            present=present,
            completed_count=completed,
            declared_types=declared,
        )
        if start is None or end is None or start > end:
            base.outside = present
            return base

        candidates = legacy_range_candidates(start, end)
        present_set = set(present)
        represented = sorted(
            {t for n in present if (t := _partition_table(n)) is not None and t in candidates}
        )
        # 순서상 마지막으로 checkpoint가 생긴 유형. 그보다 앞의 빈 유형은 선택되지 않았다.
        last = max((order.index(t) for t in represented), default=-1)
        absent = [t for t in order if t not in represented]
        included = [t for t in absent if declared is not None and t in declared]
        expected = {n for t in [*represented, *included] for n in candidates[t]}
        all_candidates = {n for names in candidates.values() for n in names}
        trailing = [t for t in absent if order.index(t) > last]

        base.range_ok = True
        base.range_days = (end - start).days + 1
        base.represented_types = represented
        base.included_types = included
        base.expected_count = len(expected)
        base.gaps = sorted(expected - present_set)
        base.trailing_types = {t: candidates[t] for t in trailing}
        base.excluded_types = [t for t in absent if order.index(t) < last and t not in included]
        base.absent_types = {t: candidates[t] for t in trailing if t not in included}
        base.outside = sorted(present_set - all_candidates)
        return base

    @property
    def undecided_types(self) -> dict[str, list[str]]:
        """원래 작업에 포함됐는지 아직 정하지 않은 뒤 유형(유형 → 범위 내 후보)."""
        if self.declared_types is not None:
            return {}
        return dict(self.trailing_types)

    @property
    def candidates(self) -> set[str]:
        """보충해도 되는 이름: 범위 내 기대 파티션 전체(모든 유형)."""
        start, end = _parse_day(self.start_date), _parse_day(self.end_date)
        if not self.range_ok or start is None or end is None:
            return set()
        return {n for names in legacy_range_candidates(start, end).values() for n in names}

    def summary_lines(self, migration_mode: str | None = None) -> list[str]:
        if not self.range_ok:
            return [
                f"기록된 범위({self.start_date or '없음'} ~ {self.end_date or '없음'})를 해석할 수 "
                "없어 누락 여부를 확인할 수 없습니다. 이 작업은 이어서 진행할 수 없습니다."
            ]
        types = ", ".join(self.represented_types) or "(없음)"
        lines = [
            f"기록된 범위 {self.start_date} ~ {self.end_date} ({self.range_days:,}일), "
            f"체크포인트가 있는 유형: {types}",
        ]
        if self.included_types:
            lines.append(
                "원래 작업에 포함했다고 확인한 유형(체크포인트 없음): "
                + ", ".join(self.included_types)
            )
        lines.append(
            f"범위에서 기대되는 파티션 {self.expected_count:,}개, 남아 있는 체크포인트 "
            f"{len(self.present):,}개(완료 {self.completed_count:,}개), 누락 {len(self.gaps):,}개"
        )
        if self.gaps:
            lines.append(
                f"누락된 파티션: {_shown(self.gaps)} — 이어서 진행하면 계획에 보충합니다. "
                + _supplement_note(migration_mode)
            )
        undecided = [f"{t} {len(n):,}개" for t, n in self.undecided_types.items() if n]
        if undecided:
            order = "→".join(_type_codes()[t] for t in legacy_creation_order())
            lines.append(
                f"체크포인트가 없는 뒤 유형: {', '.join(undecided)} — 이전 버전은 유형 순서"
                f"({order})로 체크포인트를 만들어, 중간에 끊기면 뒤 유형이 통째로 빠집니다. "
                "원래 작업에 포함됐는지 정해야 이어서 진행할 수 있습니다."
            )
        if self.outside:
            lines.append(
                f"범위 밖·규칙 밖 체크포인트 {len(self.outside):,}개는 그대로 계획에 둡니다: "
                f"{_shown(self.outside, 5)}"
            )
        return lines


def _supplement_note(migration_mode: str | None) -> str:
    """보충한 파티션을 워커가 어떻게 처리하는지 — 경로별로 다르다."""
    if migration_mode == "postgres_to_file":
        return (
            "원본 DB에 없는 파티션은 0건 완료로 처리하고 파일을 만들지 않습니다. 아카이브에 "
            "같은 파티션이 이미 있으면 새로 내보낸 내용으로 교체합니다."
        )
    if migration_mode == "file_to_postgres":
        return (
            "아카이브에 항목과 파일이 모두 없는 파티션은 0건 완료로 처리합니다(항목이나 파일 "
            "중 하나만 있으면 실패). 대상에 이미 데이터가 있으면 묻지 않고 비운 뒤 아카이브 "
            "내용으로 다시 적재합니다."
        )
    return (
        "원본 DB에 없는 파티션은 0건 완료로 처리하고, 대상에 이미 데이터가 있으면 비울지 묻습니다."
    )


class LegacyPlanGapError(ValueError):
    """범위 대비 누락을 보충하지 않은 채 legacy 이력을 채택하려 했다."""

    def __init__(self, gaps: list[str]):
        self.gaps = sorted(gaps)
        super().__init__(
            f"기록된 범위에서 체크포인트가 빠진 파티션 {len(self.gaps):,}개를 보충하지 않으면 "
            f"이 작업을 이어서 진행할 수 없습니다: {_shown(self.gaps)}"
        )


class LegacyTypeDecisionError(ValueError):
    """checkpoint가 없는 뒤 유형의 포함 여부를 정하지 않은 채 legacy 이력을 채택하려 했다."""

    def __init__(self, types: Iterable[str]):
        self.types = sorted(types)
        super().__init__(
            f"체크포인트가 없는 뒤 유형 {', '.join(self.types)}이(가) 원래 작업에 포함됐는지 "
            "정하지 않으면 이 작업을 이어서 진행할 수 없습니다(끊겨서 통째로 빠졌을 수 있음)."
        )


class ResumeVerdict(StrEnum):
    OK = "ok"
    LEGACY = "legacy"  # 계획·지문 없음 → 명시적 확인 필요
    IDENTITY_CHANGED = "identity_changed"  # endpoint/mode/schema가 바뀜 → 차단
    PLAN_INVALID = "plan_invalid"  # 계획 변조·계획 밖 checkpoint → 차단
    NOT_FOUND = "not_found"


@dataclass
class ResumeCheck:
    """재개 전 검증 결과."""

    verdict: ResumeVerdict
    history_id: int
    # 재개할 파티션(계획 순서). allowed가 아니면 LEGACY 안내용으로만 채워진다.
    pending: list[str] = field(default_factory=list)
    # 계획대로 보충한 checkpoint
    supplemented: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # LEGACY일 때 범위 대비 커버리지(누락·유형·범위 밖). 확인 창과 채택이 함께 쓴다.
    legacy: LegacyCoverage | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is ResumeVerdict.OK

    @property
    def message(self) -> str:
        return "\n".join(self.problems)


class MigrationHistoryItem:
    """마이그레이션 이력 데이터 클래스"""

    def __init__(
        self,
        id: int | None = None,
        profile_id: int = 0,
        start_date: str = "",
        end_date: str = "",
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        status: str = "pending",
        total_rows: int = 0,
        processed_rows: int = 0,
        plan_version: int | None = None,
        migration_mode: str | None = None,
        schema_name: str | None = None,
        source_fingerprint: str | None = None,
        target_fingerprint: str | None = None,
        endpoint_label: str | None = None,
        planned_count: int | None = None,
        planned_hash: str | None = None,
        plan_fingerprint: str | None = None,
        legacy_adopted_at: datetime | None = None,
    ):
        self.id = id
        self.profile_id = profile_id
        self.start_date = start_date
        self.end_date = end_date
        self.started_at = started_at
        self.completed_at = completed_at
        self.status = status
        self.total_rows = total_rows
        self.processed_rows = processed_rows
        self.plan_version = plan_version
        self.migration_mode = migration_mode
        self.schema_name = schema_name
        self.source_fingerprint = source_fingerprint
        self.target_fingerprint = target_fingerprint
        self.endpoint_label = endpoint_label
        self.planned_count = planned_count
        self.planned_hash = planned_hash
        self.plan_fingerprint = plan_fingerprint
        self.legacy_adopted_at = legacy_adopted_at

    @property
    def is_legacy(self) -> bool:
        """계획·지문이 없는 구버전 이력인가."""
        return self.plan_version is None

    @classmethod
    def from_db_model(cls, db_history: MigrationHistory) -> MigrationHistoryItem:
        """DB 모델에서 생성"""
        return cls(
            id=db_history.id,
            profile_id=db_history.profile_id,
            start_date=db_history.start_date or "",
            end_date=db_history.end_date or "",
            started_at=db_history.started_at,
            completed_at=db_history.completed_at,
            status=db_history.status or "pending",
            total_rows=db_history.total_rows or 0,
            processed_rows=db_history.processed_rows or 0,
            plan_version=db_history.plan_version,
            migration_mode=db_history.migration_mode,
            schema_name=db_history.schema_name,
            source_fingerprint=db_history.source_fingerprint,
            target_fingerprint=db_history.target_fingerprint,
            endpoint_label=db_history.endpoint_label,
            planned_count=db_history.planned_count,
            planned_hash=db_history.planned_hash,
            plan_fingerprint=db_history.plan_fingerprint,
            legacy_adopted_at=db_history.legacy_adopted_at,
        )


class CheckpointItem:
    """체크포인트 데이터 클래스"""

    def __init__(
        self,
        id: int | None = None,
        history_id: int = 0,
        partition_name: str = "",
        status: str = "pending",
        rows_processed: int = 0,
        error_message: str = "",
        last_path_id: int | None = None,
        last_issued_date: int | None = None,
        last_issued_date_text: str | None = None,
        copy_method: str = "INSERT",
        bytes_transferred: int = 0,
    ):
        self.id = id
        self.history_id = history_id
        self.partition_name = partition_name
        self.status = status
        self.rows_processed = rows_processed
        self.error_message = error_message
        self.last_path_id = last_path_id
        self.last_issued_date = last_issued_date
        self.last_issued_date_text = last_issued_date_text
        self.copy_method = copy_method
        self.bytes_transferred = bytes_transferred

    @classmethod
    def from_db_model(cls, db_checkpoint: Checkpoint) -> CheckpointItem:
        """DB 모델에서 생성"""
        return cls(
            id=db_checkpoint.id,
            history_id=db_checkpoint.history_id,
            partition_name=db_checkpoint.partition_name,
            status=db_checkpoint.status or "pending",
            rows_processed=db_checkpoint.rows_processed or 0,
            error_message=db_checkpoint.error_message or "",
            last_path_id=db_checkpoint.last_path_id,
            last_issued_date=db_checkpoint.last_issued_date,
            last_issued_date_text=getattr(db_checkpoint, "last_issued_date_text", None),
            copy_method=db_checkpoint.copy_method or "INSERT",
            bytes_transferred=db_checkpoint.bytes_transferred or 0,
        )


class HistoryManager:
    """이력 관리자 클래스 (HistoryRepository 활용)"""

    def __init__(self):
        self.repo = HistoryRepository()

    def create_history(
        self,
        profile_id: int,
        start_date: str,
        end_date: str,
        source_status: str | None = None,
        target_status: str | None = None,
        total_rows: int = 0,
    ) -> MigrationHistoryItem:
        """계획 없는 이력 생성(하위 호환용 저수준 API).

        이렇게 만든 이력은 계획·endpoint 지문이 없어 재개 시 legacy로 취급된다.
        실제 실행은 `create_planned_history()`를 쓴다.

        Args:
            total_rows: 이 작업이 옮기기로 한 전체 행 수. 실행 시작 시점의
                범위를 기록해 둬야 나중에 "어디까지 갔나"를 답할 수 있다.
                PostgreSQL 소스라면 플래너 통계 기반 추정치다.
        """
        db_history = self.repo.create(
            profile_id=profile_id,
            start_date=start_date,
            end_date=end_date,
            started_at=datetime.now(),
            status="running",
            total_rows=total_rows,
            source_connection_status=source_status,
            target_connection_status=target_status,
            connection_check_time=datetime.now() if source_status or target_status else None,
        )
        return MigrationHistoryItem.from_db_model(db_history)

    def create_planned_history(
        self,
        profile: ConnectionProfile,
        partitions: list[str] | tuple[str, ...],
        start_date: str,
        end_date: str,
        *,
        schema: str = DEFAULT_SCHEMA,
        source_status: str | None = None,
        target_status: str | None = None,
        total_rows: int = 0,
    ) -> MigrationHistoryItem:
        """이력 + 계획된 모든 checkpoint를 한 트랜잭션으로 만든다(H-08, H-09).

        불변 계획(endpoint 지문, mode, schema, planned set/count/hash, plan 지문)을 함께
        기록한다. 어느 checkpoint에서든 실패하면 이력까지 전부 rollback된다.

        Raises:
            ValueError: 저장되지 않은 프로필, 빈 계획, 중복 파티션.
        """
        if profile.id is None:
            raise ValueError("저장되지 않은 프로필로는 이력을 만들 수 없습니다")
        plan = MigrationPlan.from_profile(profile, partitions, schema)
        now = datetime.now()
        db_history = self.repo.create_with_checkpoints(
            plan.partitions,
            profile_id=profile.id,
            start_date=start_date,
            end_date=end_date,
            started_at=now,
            status="running",
            total_rows=total_rows,
            source_connection_status=source_status,
            target_connection_status=target_status,
            connection_check_time=now if source_status or target_status else None,
            **plan.to_columns(),
        )
        return MigrationHistoryItem.from_db_model(db_history)

    def prepare_resume(
        self,
        history_id: int,
        profile: ConnectionProfile,
        *,
        schema: str = DEFAULT_SCHEMA,
        original_types: Iterable[str] | None = None,
    ) -> ResumeCheck:
        """재개 전에 계획 무결성·identity·checkpoint 완전성을 검증한다.

        순서: 저장된 계획의 무결성(count/hash/plan 지문) → 현재 프로필과 identity
        비교 → 계획 밖 checkpoint 차단 → 누락 checkpoint 보충. 보충 외에는 아무것도
        쓰지 않는다. 차단 판정에서는 checkpoint를 건드리지 않는다.

        `original_types`는 legacy 이력에만 쓴다: 사용자가 원래 작업에 포함됐다고 확인한
        테이블 유형. 주면 checkpoint 없는 뒤 유형의 누락까지 `legacy.gaps`에 넣어 보여 준다.
        """
        row = self.repo.get_by_id(history_id)
        if row is None:
            return ResumeCheck(
                ResumeVerdict.NOT_FOUND, history_id, problems=["작업 이력을 찾을 수 없습니다."]
            )

        states = self.repo.get_checkpoint_states(history_id)
        present = {name for name, _ in states}
        completed = {name for name, status in states if status == "completed"}

        if row.plan_version is None:
            coverage = LegacyCoverage.compute(row.start_date, row.end_date, states, original_types)
            return ResumeCheck(
                ResumeVerdict.LEGACY,
                history_id,
                pending=sorted(present - completed),
                problems=[
                    "이 작업은 연결 지문과 계획을 기록하기 전 버전에서 만들어졌습니다.",
                    "원래 작업과 같은 원본·대상인지 자동으로 확인할 수 없습니다.",
                    # 구버전은 checkpoint를 하나씩 commit했으므로(H-09) 일부가 빠져 있을 수 있다.
                    *coverage.summary_lines(profile.migration_mode),
                ],
                legacy=coverage,
            )

        planned, integrity_problem = self._verified_plan(row)
        if integrity_problem:
            return ResumeCheck(ResumeVerdict.PLAN_INVALID, history_id, problems=[integrity_problem])

        identity_problems = self._identity_problems(row, profile, schema)
        if identity_problems:
            return ResumeCheck(
                ResumeVerdict.IDENTITY_CHANGED, history_id, problems=identity_problems
            )

        outside = sorted(present - set(planned))
        if outside:
            shown = ", ".join(outside[:5]) + (" 외" if len(outside) > 5 else "")
            return ResumeCheck(
                ResumeVerdict.PLAN_INVALID,
                history_id,
                problems=[f"계획에 없는 체크포인트 {len(outside)}개가 있습니다: {shown}"],
            )

        supplemented: list[str] = []
        if present != set(planned):
            supplemented = self.repo.add_missing_checkpoints(history_id, planned)

        return ResumeCheck(
            ResumeVerdict.OK,
            history_id,
            pending=[name for name in planned if name not in completed],
            supplemented=supplemented,
        )

    @staticmethod
    def _verified_plan(row: MigrationHistory) -> tuple[list[str], str | None]:
        """저장된 planned set을 읽고 count/hash/plan 지문으로 무결성을 확인한다."""
        broken = "저장된 작업 계획이 손상되었습니다(파티션 목록·개수·해시 불일치)."
        try:
            planned = json.loads(row.planned_partitions or "")
        except (TypeError, ValueError):
            return [], broken
        if not isinstance(planned, list) or not all(isinstance(p, str) for p in planned):
            return [], broken
        if planned != sorted(set(planned)) or len(planned) != row.planned_count:
            return [], broken
        if planned_set_hash(planned) != row.planned_hash:
            return [], broken
        recomputed = MigrationPlan(
            migration_mode=row.migration_mode or "",
            schema=row.schema_name or "",
            source_fingerprint=row.source_fingerprint or "",
            target_fingerprint=row.target_fingerprint or "",
            partitions=tuple(planned),
        ).fingerprint
        if recomputed != row.plan_fingerprint:
            return [], "저장된 작업 계획의 지문이 맞지 않습니다(연결·모드 기록이 바뀜)."
        return planned, None

    @staticmethod
    def _identity_problems(
        row: MigrationHistory, profile: ConnectionProfile, schema: str
    ) -> list[str]:
        problems: list[str] = []
        if row.migration_mode != profile.migration_mode:
            problems.append(
                f"이관 방향이 바뀌었습니다: 기록 {row.migration_mode} → 현재 {profile.migration_mode}"
            )
        if row.schema_name != schema:
            problems.append(f"스키마가 바뀌었습니다: 기록 {row.schema_name} → 현재 {schema}")
        if row.source_fingerprint != endpoint_fingerprint(profile.source_config):
            problems.append("원본 연결(host·port·database·user)이 바뀌었습니다.")
        if row.target_fingerprint != endpoint_fingerprint(profile.target_config):
            problems.append("대상 연결(host·port·database·user 또는 경로)이 바뀌었습니다.")
        if problems:
            current = (
                f"{endpoint_label(profile.source_config)} → {endpoint_label(profile.target_config)}"
            )
            problems.append(f"기록된 연결: {row.endpoint_label or '(기록 없음)'}")
            problems.append(f"현재 연결: {current}")
        return problems

    def adopt_legacy_history(
        self,
        history_id: int,
        profile: ConnectionProfile,
        *,
        schema: str = DEFAULT_SCHEMA,
        supplement: Iterable[str] = (),
        original_types: Iterable[str] | None = None,
    ) -> ResumeCheck:
        """사용자가 확인한 legacy 이력을 현재 identity와 계획에 묶는다.

        계획 = 남아 있는 checkpoint ∪ `supplement`. `supplement`는 기록된 범위의 기대
        파티션(`LegacyCoverage.candidates`)이어야 하고, 누락(`LegacyCoverage.gaps`: checkpoint가
        있는 유형과 `original_types`로 포함한 유형의 누락)은 전부 들어 있어야 한다 — 아니면
        `LegacyPlanGapError`. 남은 일부만으로 계획을 고정하면 구버전 H-09 누락이 정상이 되어
        subset만 처리하고 completed로 닫히기 때문이다.

        checkpoint가 없는 뒤 유형(`LegacyCoverage.trailing_types`)이 있으면 `original_types`로
        원래 작업에 포함됐는지 정해야 한다(None이면 `LegacyTypeDecisionError`) — 유형 경계에서
        끊긴 이력은 누락이 gaps로 드러나지 않는다. 범위를 해석할 수 없거나 checkpoint가 없으면
        거부한다.

        보충 checkpoint 생성과 계획 기록은 한 트랜잭션이다. 한 번만 기록된다(이미 계획이
        있으면 ValueError). 이후 재개는 엄격 모드로 검사한다.
        """
        row = self.repo.get_by_id(history_id)
        if row is None:
            raise ValueError("작업 이력을 찾을 수 없습니다")
        if row.plan_version is not None:
            raise ValueError("이미 계획이 기록된 이력입니다")
        states = self.repo.get_checkpoint_states(history_id)
        coverage = LegacyCoverage.compute(row.start_date, row.end_date, states, original_types)
        if not coverage.present:
            raise ValueError("체크포인트가 없어 계획을 복원할 수 없습니다. 작업을 폐기하세요.")
        if not coverage.range_ok:
            raise ValueError(" ".join(coverage.summary_lines()) + " 작업을 폐기하세요.")

        extra = {str(n) for n in supplement}
        invalid = sorted(extra - coverage.candidates)
        if invalid:
            raise ValueError(
                f"기록된 범위({coverage.start_date} ~ {coverage.end_date})의 파티션만 보충할 수 "
                f"있습니다: {_shown(invalid)}"
            )
        unfilled = sorted(set(coverage.gaps) - extra)
        if unfilled:
            raise LegacyPlanGapError(unfilled)
        if coverage.undecided_types:
            raise LegacyTypeDecisionError(coverage.undecided_types)

        names = sorted(set(coverage.present) | extra)
        plan = MigrationPlan.from_profile(profile, names, schema)
        bound = self.repo.bind_legacy_plan(
            history_id,
            coverage.present,
            add_partitions=sorted(extra - set(coverage.present)),
            legacy_adopted_at=datetime.now(),
            **plan.to_columns(),
        )
        if not bound:
            raise ValueError("확인하는 동안 이력이 바뀌었습니다. 다시 시도하세요")
        return self.prepare_resume(history_id, profile, schema=schema)

    def count_incomplete_histories(self, profile_id: int) -> int:
        """프로필에 남은 미완료(running/failed/partial) 이력 수."""
        return self.repo.count_incomplete_by_profile(profile_id)

    def abandon_incomplete_histories(self, profile_id: int) -> int:
        """프로필의 미완료 이력을 모두 명시적으로 폐기(cancelled)한다."""
        return self.repo.abandon(profile_id=profile_id)

    def abandon_history(self, history_id: int) -> bool:
        """미완료 이력 하나를 명시적으로 폐기(cancelled)한다."""
        return self.repo.abandon(history_id=history_id) == 1

    def get_history(self, history_id: int) -> MigrationHistoryItem | None:
        """이력 조회"""
        db_history = self.repo.get_by_id(history_id)
        if db_history:
            return MigrationHistoryItem.from_db_model(db_history)
        return None

    def get_all_history(self) -> list[MigrationHistoryItem]:
        """모든 이력 조회 (최신순)"""
        db_histories = self.repo.get_all_desc()
        return [MigrationHistoryItem.from_db_model(h) for h in db_histories]

    def update_history_status(
        self,
        history_id: int,
        status: str,
        processed_rows: int | None = None,
        total_rows: int | None = None,
    ) -> bool:
        """이력 상태 업데이트

        Args:
            total_rows: 전체 행 수를 나중에 바로잡을 때만 넘긴다. 실행 시점의
                값은 추정치이므로, 실제로 다 옮기고 나면 정정할 수 있다.
        """
        updates: dict[str, Any] = {"status": status}
        if processed_rows is not None:
            updates["processed_rows"] = processed_rows
        if total_rows is not None:
            updates["total_rows"] = total_rows

        if status in ["completed", "failed", "cancelled"]:
            updates["completed_at"] = datetime.now()

        return self.repo.update_by_id(history_id, **updates)

    def get_incomplete_history(self, profile_id: int) -> MigrationHistoryItem | None:
        """미완료 이력 조회"""
        db_history = self.repo.get_incomplete_by_profile(profile_id)
        if db_history:
            return MigrationHistoryItem.from_db_model(db_history)
        return None

    def get_completed_histories(self, profile_id: int) -> list[MigrationHistoryItem]:
        """프로필의 완료된 이력 목록 조회"""
        db_histories = self.repo.get_completed_by_profile(profile_id)
        return [MigrationHistoryItem.from_db_model(h) for h in db_histories]


class CheckpointManager:
    """체크포인트 관리자 클래스 (CheckpointRepository 활용)"""

    def __init__(self):
        self.repo = CheckpointRepository()

    def create_checkpoint(self, history_id: int, partition_name: str) -> CheckpointItem:
        """체크포인트 생성"""
        db_checkpoint = self.repo.create(
            history_id=history_id, partition_name=partition_name, status="pending"
        )
        return CheckpointItem.from_db_model(db_checkpoint)

    def get_checkpoints(self, history_id: int) -> list[CheckpointItem]:
        """이력별 체크포인트 조회"""
        db_checkpoints = self.repo.get_by_history(history_id)
        return [CheckpointItem.from_db_model(c) for c in db_checkpoints]

    def update_checkpoint_status(
        self,
        checkpoint_id: int,
        status: str,
        rows_processed: int | None = None,
        error_message: str | None = None,
        last_path_id: int | None = None,
        last_issued_date: int | None = None,
        last_issued_date_text: str | None = None,
        copy_method: str | None = None,
        bytes_transferred: int | None = None,
    ) -> bool:
        """체크포인트 상태 업데이트"""
        updates: dict[str, Any] = {"status": status}
        if rows_processed is not None:
            updates["rows_processed"] = rows_processed
        if error_message is not None:
            updates["error_message"] = error_message
        if last_path_id is not None:
            updates["last_path_id"] = last_path_id
        if last_issued_date is not None:
            updates["last_issued_date"] = last_issued_date
        if last_issued_date_text is not None:
            updates["last_issued_date_text"] = last_issued_date_text
        if copy_method is not None:
            updates["copy_method"] = copy_method
        if bytes_transferred is not None:
            updates["bytes_transferred"] = bytes_transferred

        return self.repo.update_by_id(checkpoint_id, **updates)

    def get_processed_rows(self, history_id: int) -> int:
        """이력의 누적 처리 행 수(실행 횟수와 무관).

        워커의 성능 카운터는 실행 1회분만 센다. 재개 후 그 값을 이력에
        쓰면 진행량이 뒤로 간다(70만 처리 후 중단 → 30만 더 처리하고 완료 →
        이력에는 30만). 체크포인트는 실행을 넘어 남으므로 여기서 합산한다.
        """
        return self.repo.sum_rows_processed(history_id)

    def has_pending_checkpoints(self, history_id: int) -> bool:
        """아직 끝나지 않은 파티션이 남아 있는가."""
        return self.repo.count_pending(history_id) > 0

    def final_total_rows(self, history_id: int, processed_rows: int) -> int | None:
        """완료 시점에 이력의 전체 행 수를 정정할 값.

        실행 시작 때 기록한 분모는 플래너 통계 기반 추정치다. 오차가 0.02%
        수준이어도 분모가 분자보다 크면 다 옮기고도 99%로 남는다.

        단 **전부 성공했을 때만** 정정한다. '건너뛰기' 모드로 일부 파티션이
        실패하면, 옮긴 양을 전체로 간주하는 순간 100%가 그 실패를 덮는다.

        Returns:
            정정할 값, 또는 그대로 두라는 뜻의 None.
        """
        try:
            if self.has_pending_checkpoints(history_id):
                return None
        except Exception:
            # 집계에 실패하면 건드리지 않는다. 추정치가 남는 편이
            # 틀린 100%보다 낫다.
            return None
        return processed_rows

    def get_pending_checkpoints(self, history_id: int) -> list[CheckpointItem]:
        """미완료 체크포인트 조회"""
        db_checkpoints = self.repo.get_pending_by_history(history_id)
        return [CheckpointItem.from_db_model(c) for c in db_checkpoints]

    def get_completed_partition_names(self, history_ids: list[int]) -> set[str]:
        """여러 이력에서 완료된 파티션 이름 집합 반환"""
        if not history_ids:
            return set()
        checkpoints = self.repo.get_completed_names_by_history_ids(history_ids)
        return {c.partition_name for c in checkpoints}
