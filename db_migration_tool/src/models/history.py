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
- legacy(계획 컬럼이 비어 있는 구버전 이력): 자동 재개하지 않는다. 사용자가 현재 연결이
  원래 작업의 연결과 같다고 명시적으로 확인하면 `adopt_legacy_history()`가 현재 identity와
  남아 있는 checkpoint 집합을 계획으로 한 번 기록하고, 그 뒤로는 엄격 모드로 검사한다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
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
    "MigrationHistoryItem",
    "MigrationPlan",
    "ResumeCheck",
    "ResumeVerdict",
    "endpoint_fingerprint",
    "endpoint_identity",
    "endpoint_label",
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
    ) -> ResumeCheck:
        """재개 전에 계획 무결성·identity·checkpoint 완전성을 검증한다.

        순서: 저장된 계획의 무결성(count/hash/plan 지문) → 현재 프로필과 identity
        비교 → 계획 밖 checkpoint 차단 → 누락 checkpoint 보충. 보충 외에는 아무것도
        쓰지 않는다. 차단 판정에서는 checkpoint를 건드리지 않는다.
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
            return ResumeCheck(
                ResumeVerdict.LEGACY,
                history_id,
                pending=sorted(present - completed),
                problems=[
                    "이 작업은 연결 지문과 계획을 기록하기 전 버전에서 만들어졌습니다.",
                    "원래 작업과 같은 원본·대상인지 자동으로 확인할 수 없습니다.",
                    # 구버전은 checkpoint를 하나씩 commit했으므로(H-09) 일부가 빠져 있을 수 있다.
                    f"기록된 범위 {row.start_date} ~ {row.end_date}, 남아 있는 체크포인트 "
                    f"{len(present):,}개(완료 {len(completed):,}개) — 범위의 파티션이 모두 "
                    "있는지도 확인하세요.",
                ],
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
    ) -> ResumeCheck:
        """사용자가 확인한 legacy 이력을 현재 identity와 남은 checkpoint 집합에 묶는다.

        한 번만 기록된다(이미 계획이 있으면 ValueError). 이후 재개는 엄격 모드로 검사한다.
        남은 checkpoint가 없으면 계획을 복원할 근거가 없으므로 거부한다 — 폐기 대상이다.
        """
        row = self.repo.get_by_id(history_id)
        if row is None:
            raise ValueError("작업 이력을 찾을 수 없습니다")
        if row.plan_version is not None:
            raise ValueError("이미 계획이 기록된 이력입니다")
        names = sorted({name for name, _ in self.repo.get_checkpoint_states(history_id)})
        if not names:
            raise ValueError("체크포인트가 없어 계획을 복원할 수 없습니다. 작업을 폐기하세요.")
        plan = MigrationPlan.from_profile(profile, names, schema)
        bound = self.repo.bind_legacy_plan(
            history_id, names, legacy_adopted_at=datetime.now(), **plan.to_columns()
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
