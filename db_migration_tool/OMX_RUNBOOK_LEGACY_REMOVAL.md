# OMX Runbook — Legacy MigrationDialog 제거 (즉시 삭제)

## Goal
- UI 진입점을 Wizard로 단일화하여 기능/정책 중복을 제거한다.
- Server-side COPY / resume 정책 / truncate 경고 등 “민감한 UX/안전 정책”을 한 군데에서만 유지한다.

## Scope
- 제거 대상: `src/ui/dialogs/migration_dialog.py` (Legacy UI)
- 유지/기본: `src/ui/dialogs/migration_wizard_dialog.py`

## Why (결정 근거)
- Wizard가 현재 권장 플로우(연결 확인 → 범위/파티션 → 실행) 및 최신 옵션(server-side COPY 포함)을 커버함.
- Legacy dialog를 유지하면 아래 정책을 **두 군데**에 계속 동기화해야 함:
  - 중단(stop) = completed로 오인하지 않기(QThread.finished semantics)
  - resume 시 server-side COPY 금지/자동 전환
  - server-side COPY의 TRUNCATE 위험 경고/확인

## Change summary
- `git rm db_migration_tool/src/ui/dialogs/migration_dialog.py`
- 참조는 Wizard로 단일화(빌드/테스트 통과 조건으로 검증)

## Quality gates
- `make test` ✅

## Rollback
- Git에서 이전 커밋으로 되돌리면 복구 가능.

## Follow-ups (선택)
- README/사용 가이드에서 legacy UI 언급이 있으면 제거.
- PyInstaller/배포 빌드 smoke.
