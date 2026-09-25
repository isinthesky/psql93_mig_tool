"""라이선스 키 발급 — 개발·비상용(개발사 전용).

**운영 발급은 라이선스 서버의 signer가 한다**(`lgetech-license-server/app/licensing.py`).
이 도구는 개발·테스트 키 발급과, 서버를 쓸 수 없을 때의 비상 발급에만 쓴다. 서버와 이 도구가
같은 개인키를 쓴다면 그 키의 사본이 하나 더 있는 것이므로, 사용 후 이 PC의 사본을 지운다.

## 개인키 보호 (감사 M-06)

- 개인키는 **PKCS#8 PEM + 패스프레이즈 암호화**(`BestAvailableEncryption`)로만 저장한다.
- 패스프레이즈는 **대화형 입력(getpass) 또는 환경변수 `DBMT_ISSUER_KEY_PASSPHRASE`** 로만 받는다.
  명령줄 인자로는 받지 않는다 — 셸 히스토리·프로세스 목록·로그에 남는다.
- 새 키 파일은 `O_EXCL`로 만들고 권한을 0600으로 둔다(Windows는 `icacls`로 현재 사용자만).
  POSIX에서 그룹·기타 권한이 열린 키 파일은 읽기를 거부한다.
- 키 식별자는 공개키 SHA-256 fingerprint(`SHA256:<hex>`)다. 생성·발급·변환 때 출력하고,
  발급 대장·키 관리 기록에 함께 남긴다.
- 기존 평문 키(raw 32바이트, 또는 암호화되지 않은 PEM)는 발급에 쓸 수 없다.
  경고와 함께 `--convert-legacy` 로 암호화 파일로 **변환만** 허용한다. 원본 평문 파일은
  도구가 지우지 않는다 — 변환본으로 발급이 되는지 확인한 뒤 사람이 모든 사본을 폐기한다.

개인키는 이 저장소에 두지 않는다. 경로는 인자로만 받고 기본값을 두지 않는다
(실수로 커밋되는 경로를 아예 만들지 않기 위해서다).

## 사용법

    # 패스프레이즈는 프롬프트로 묻는다(비대화형이면 환경변수를 쓴다).
    python tools/issue_license.py --genkey --out-private C:\\secure\\license_private.key
    python tools/issue_license.py --key-file C:\\secure\\license_private.key \\
        --cust "OO전자" --months 12
    python tools/issue_license.py --key-file C:\\secure\\license_private.key \\
        --cust "OO전자" --exp 2027-12-31
    python tools/issue_license.py --show-public --key-file C:\\secure\\license_private.key

    # 기존 평문 키 → 암호화 키 (변환만 가능)
    python tools/issue_license.py --convert-legacy \\
        --key-file C:\\secure\\old_plain.key --out-private C:\\secure\\license_private.key

발급 내역은 `--ledger`로 준 CSV에 append 한다. 이 파일도 저장소에 넣지 않는다.

## 키 교체(rotation) 절차

앱은 공개키 하나(`src/licensing/keys.py`의 `LICENSE_PUBLIC_KEY_B32`)만 신뢰한다. 따라서 교체는
"새 키로 전부 재발급"과 같다.

1. `--genkey`로 새 암호화 키를 만들고 fingerprint를 키 관리 기록에 남긴다.
2. 라이선스 서버 signer 키와 앱 `keys.py` 공개키를 **함께** 바꾼다(앱/서버 계약, CLAUDE.md §5).
   공용 테스트 벡터와 공개키 일치 여부를 확인한다.
3. 새 공개키로 앱을 빌드·배포한다. 옛 키로 서명된 라이선스는 새 앱에서 INVALID가 되므로,
   발급 대장의 유효 라이선스를 새 키로 재발급해 새 앱과 함께 전달한다.
4. 재발급이 끝나면 옛 개인키의 모든 사본(백업 포함)을 폐기하고, 폐기일과 fingerprint를 기록한다.

## 폐기(유출 의심) 절차

앱은 오프라인(TOFU)이라 **개별 라이선스나 키를 원격으로 무효화할 수 없다.** 유출이 의심되면:

1. 즉시 해당 키 사용을 중단하고(서버 signer 포함) 유출 범위·시점을 기록한다.
2. 위 교체 절차 1~4를 긴급으로 수행한다. 옛 공개키를 가진 기존 빌드는 유출 키로 만든
   위조 라이선스도 계속 받아들이므로, 새 빌드로의 업그레이드가 유일한 차단 수단이다.
3. 발급 대장에서 교체 전 일련번호를 모두 '폐기 키로 서명됨'으로 표시한다.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

# 저장소 루트를 import 경로에 넣는다 (tools/ 에서 직접 실행하므로).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from src.licensing.payload import _b32encode, build_key  # noqa: E402

# 패스프레이즈를 받는 유일한 비대화형 경로. 명령줄 인자는 받지 않는다.
PASSPHRASE_ENV = "DBMT_ISSUER_KEY_PASSPHRASE"
# 새 키(생성·변환)에 요구하는 최소 길이. 기존 키를 여는 데는 적용하지 않는다.
MIN_PASSPHRASE_LEN = 12
KEY_FILE_MODE = 0o600

_ENCRYPTED_PEM = b"-----BEGIN ENCRYPTED PRIVATE KEY-----"
_PLAIN_PEM = b"-----BEGIN PRIVATE KEY-----"
_RAW_KEY_LEN = 32

PAYLOAD_VERSION = 1


def add_months(start: date, months: int) -> date:
    """start 로부터 months 개월 뒤의 '하루 전'. 12개월이면 1년에서 하루 모자란 날이다.

    만료일은 '포함'이므로(그 날까지 유효) 이렇게 잡아야 정확히 계약 기간이 된다.
    """
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    day = start.day

    # 2월 30일 같은 날짜를 피한다.
    while True:
        try:
            end = date(year, month, day)
            break
        except ValueError:
            day -= 1

    return date.fromordinal(end.toordinal() - 1)


def _warn(message: str) -> None:
    print(f"[경고] {message}", file=sys.stderr)


# ── 패스프레이즈 ────────────────────────────────────────────


def read_passphrase(*, confirm: bool) -> bytes:
    """패스프레이즈를 환경변수 또는 대화형 입력으로 받는다. 명령줄 인자로는 받지 않는다.

    Args:
        confirm: 새 키를 만들 때처럼 두 번 입력받아 일치를 확인할지.
    """
    from_env = os.environ.get(PASSPHRASE_ENV)
    if from_env is not None:
        if not from_env:
            raise SystemExit(f"환경변수 {PASSPHRASE_ENV} 가 비어 있습니다.")
        return from_env.encode("utf-8")

    if not sys.stdin.isatty():
        raise SystemExit(
            "패스프레이즈를 입력받을 터미널이 없습니다. "
            f"대화형으로 실행하거나 환경변수 {PASSPHRASE_ENV} 를 설정하세요."
        )
    first = getpass.getpass("개인키 패스프레이즈: ")
    if confirm and getpass.getpass("패스프레이즈 확인: ") != first:
        raise SystemExit("두 패스프레이즈가 일치하지 않습니다.")
    if not first:
        raise SystemExit("패스프레이즈가 비어 있습니다.")
    return first.encode("utf-8")


def _require_strong(passphrase: bytes) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LEN:
        raise SystemExit(
            f"새 개인키의 패스프레이즈는 {MIN_PASSPHRASE_LEN}바이트 이상이어야 합니다."
        )


# ── 파일 권한 ───────────────────────────────────────────────


def restrict_permissions(path: Path, platform: str = os.name) -> None:
    """키 파일을 소유자만 읽고 쓰게 한다. 실패하면 경고한다(파일은 이미 암호화돼 있다)."""
    if platform == "nt":
        user = os.environ.get("USERNAME") or getpass.getuser()
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            _warn(f"icacls 로 권한을 제한하지 못했습니다: {path} — 직접 ACL을 확인하세요.")
        return
    try:
        path.chmod(KEY_FILE_MODE)
    except OSError as exc:
        _warn(f"권한을 0600으로 바꾸지 못했습니다: {path} ({exc})")


def _check_permissions(path: Path) -> None:
    """POSIX에서 그룹·기타 권한이 열린 키 파일은 거부한다. Windows ACL은 운영 절차로 확인한다."""
    if os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(
            f"개인키 파일 권한이 너무 넓습니다({oct(mode)}): {path}\n"
            f"소유자만 읽을 수 있게 0600으로 바꾸세요: chmod 600 {path}"
        )


# ── 키 저장·로드 ────────────────────────────────────────────


def fingerprint_of(private: Ed25519PrivateKey) -> str:
    """키 식별자 — 공개키 raw 32바이트의 SHA-256. 발급 대장·키 관리 기록에 남긴다."""
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "SHA256:" + hashlib.sha256(raw).hexdigest()


def _write_encrypted(path: Path, private: Ed25519PrivateKey, passphrase: bytes) -> None:
    """개인키를 암호화 PKCS#8 PEM으로 새 파일에 쓴다. 기존 파일은 절대 덮어쓰지 않는다."""
    _require_strong(passphrase)
    if path.exists():
        raise SystemExit(
            f"이미 파일이 있습니다: {path}\n"
            "덮어쓰면 기존 라이선스를 모두 갱신할 수 없게 됩니다. 다른 경로를 쓰세요."
        )
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, KEY_FILE_MODE)
    with os.fdopen(fd, "wb") as fp:
        fp.write(pem)
    restrict_permissions(path)


def generate_keypair(out_private: Path, passphrase: bytes) -> tuple[str, str]:
    """키 쌍을 만들고 개인키를 암호화해 저장한다. (공개키 Base32, fingerprint)를 돌려준다."""
    private = Ed25519PrivateKey.generate()
    _write_encrypted(out_private, private, passphrase)
    return public_key_of(private), fingerprint_of(private)


def _is_legacy_plaintext(data: bytes) -> bool:
    return (
        len(data) == _RAW_KEY_LEN and not data.startswith(b"-----")
    ) or data.lstrip().startswith(_PLAIN_PEM)


def _load_legacy_plaintext(data: bytes) -> Ed25519PrivateKey:
    if len(data) == _RAW_KEY_LEN and not data.startswith(b"-----"):
        return Ed25519PrivateKey.from_private_bytes(data)
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Ed25519 개인키가 아닙니다.")
    return key


def load_private_key(path: Path, get_passphrase: Callable[[], bytes]) -> Ed25519PrivateKey:
    """암호화된 개인키를 연다. 평문 키는 패스프레이즈를 묻기 전에 거부한다(변환만 허용)."""
    data = path.read_bytes()

    if _is_legacy_plaintext(data):
        _warn(f"암호화되지 않은 평문 개인키입니다: {path}")
        raise SystemExit(
            "평문 개인키로는 발급·조회할 수 없습니다. 먼저 암호화 파일로 변환하세요:\n"
            f"  python tools/issue_license.py --convert-legacy --key-file {path} "
            "--out-private <새 경로>\n"
            "변환본으로 발급을 확인한 뒤 평문 원본과 그 사본을 모두 폐기하세요."
        )
    if not data.lstrip().startswith(_ENCRYPTED_PEM):
        raise SystemExit(f"알 수 없는 개인키 형식입니다: {path}")

    _check_permissions(path)
    try:
        key = serialization.load_pem_private_key(data, password=get_passphrase())
    except (ValueError, TypeError):
        raise SystemExit("패스프레이즈가 틀렸거나 개인키 파일이 손상되었습니다.")
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Ed25519 개인키가 아닙니다.")
    return key


def convert_legacy_key(src: Path, out_private: Path, passphrase: bytes) -> str:
    """평문 개인키를 암호화 파일로 옮긴다. 원본은 지우지 않는다. fingerprint를 돌려준다."""
    if src.resolve() == out_private.resolve():
        raise SystemExit("변환 결과는 원본과 다른 경로여야 합니다(원본을 덮어쓰지 않는다).")
    data = src.read_bytes()
    if not _is_legacy_plaintext(data):
        raise SystemExit(f"평문 개인키가 아닙니다(이미 암호화됐거나 알 수 없는 형식): {src}")

    _warn(
        f"평문 개인키를 변환합니다: {src}\n"
        "  변환본으로 발급이 되는지 확인한 뒤, 평문 원본과 그 모든 사본(백업 포함)을 폐기하세요."
    )
    private = _load_legacy_plaintext(data)
    _write_encrypted(out_private, private, passphrase)
    return fingerprint_of(private)


def public_key_of(private: Ed25519PrivateKey) -> str:
    """개인키에서 공개키를 다시 뽑는다.

    keys.py 를 잃었거나 새로 셋업할 때 필요하다. 개인키만 있으면 공개키는
    언제든 복원할 수 있다(반대는 불가능하다).
    """
    return _b32encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def issue(
    private: Ed25519PrivateKey, customer: str, expires: date, today: date
) -> tuple[str, dict]:
    serial = secrets.token_hex(8)  # 128비트. 대장과 대조할 식별자라 충돌하면 안 된다.
    payload = {
        "v": PAYLOAD_VERSION,
        "cust": customer,
        "iss": today.isoformat(),
        "exp": expires.isoformat(),
        "lic": serial,
    }
    # 서명 대상은 '이 바이트열 그대로'다. 검증 측은 재직렬화하지 않고
    # Base32로 복원한 바이트에 바로 서명을 확인한다.
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = private.sign(payload_bytes)
    return build_key(payload_bytes, signature), payload


def append_ledger(ledger: Path, entry: dict) -> None:
    exists = ledger.exists()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=["lic", "cust", "iss", "exp"])
        if not exists:
            writer.writeheader()
        writer.writerow({k: entry[k] for k in ("lic", "cust", "iss", "exp")})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="라이선스 키를 발급한다 (개발·비상용 — 운영 발급은 라이선스 서버).",
        epilog=f"패스프레이즈는 프롬프트 또는 환경변수 {PASSPHRASE_ENV} 로만 받는다.",
        allow_abbrev=False,
    )
    parser.add_argument("--genkey", action="store_true", help="새 키 쌍 생성(암호화 저장)")
    parser.add_argument(
        "--convert-legacy",
        action="store_true",
        help="평문 개인키(--key-file)를 암호화 파일(--out-private)로 변환",
    )
    parser.add_argument("--out-private", type=Path, help="새 개인키 저장 경로")
    parser.add_argument(
        "--show-public",
        action="store_true",
        help="기존 개인키에서 공개키·fingerprint를 다시 출력 (keys.py 복구용)",
    )
    parser.add_argument("--key-file", type=Path, help="발급에 쓸 개인키 경로")
    parser.add_argument("--cust", help="고객사명")
    parser.add_argument("--months", type=int, help="오늘부터 N개월")
    parser.add_argument("--exp", help="만료일 YYYY-MM-DD (이 날까지 유효)")
    parser.add_argument("--ledger", type=Path, help="발급 대장 CSV (append)")
    return parser


def _ask_existing() -> bytes:
    return read_passphrase(confirm=False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.genkey:
        if not args.out_private:
            raise SystemExit("--genkey 에는 --out-private 가 필요합니다.")
        public_b32, fingerprint = generate_keypair(args.out_private, read_passphrase(confirm=True))
        print(f"개인키 저장(암호화): {args.out_private}")
        print(f"키 ID       : {fingerprint}")
        print("\n아래 값을 src/licensing/keys.py 의 LICENSE_PUBLIC_KEY_B32 에 넣으세요:\n")
        print(f'LICENSE_PUBLIC_KEY_B32 = "{public_b32}"\n')
        print("개인키를 잃으면 갱신·재발급이 불가능하고, 유출되면 누구나 키를 만들 수 있습니다.")
        print("패스프레이즈를 잃어도 분실과 같습니다. 개인키와 패스프레이즈는 따로 보관하세요.")
        return 0

    if args.convert_legacy:
        if not args.key_file or not args.out_private:
            raise SystemExit(
                "--convert-legacy 에는 --key-file(평문 원본)과 --out-private 가 필요합니다."
            )
        fingerprint = convert_legacy_key(
            args.key_file, args.out_private, read_passphrase(confirm=True)
        )
        print(f"변환 완료(암호화): {args.out_private}")
        print(f"키 ID       : {fingerprint}")
        return 0

    if args.show_public:
        if not args.key_file:
            raise SystemExit("--show-public 에는 --key-file 이 필요합니다.")
        private = load_private_key(args.key_file, _ask_existing)
        print(f'LICENSE_PUBLIC_KEY_B32 = "{public_key_of(private)}"')
        print(f"키 ID       : {fingerprint_of(private)}")
        return 0

    if not args.key_file or not args.cust:
        raise SystemExit("발급하려면 --key-file 과 --cust 가 필요합니다.")
    if bool(args.months) == bool(args.exp):
        raise SystemExit("--months 와 --exp 중 정확히 하나를 지정하세요.")

    today = date.today()
    if args.exp:
        try:
            expires = date.fromisoformat(args.exp)
        except ValueError:
            raise SystemExit(f"날짜 형식이 올바르지 않습니다: {args.exp} (YYYY-MM-DD)")
    else:
        expires = add_months(today, args.months)

    if expires < today:
        raise SystemExit(f"만료일이 과거입니다: {expires}")

    private = load_private_key(args.key_file, _ask_existing)
    key, payload = issue(private, args.cust, expires, today)

    if args.ledger:
        append_ledger(args.ledger, payload)

    print(f"고객사 : {payload['cust']}")
    print(f"발급일 : {payload['iss']}")
    print(f"만료일 : {payload['exp']}  (이 날까지 유효)")
    print(f"일련번호: {payload['lic']}")
    print(f"서명 키 : {fingerprint_of(private)}")
    print("\n라이선스 키:\n")
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
