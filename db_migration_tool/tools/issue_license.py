"""라이선스 키 발급 — 개발사 전용.

개인키는 이 저장소에 두지 않는다. 경로를 인자로만 받고, 기본값도 환경변수도 두지 않는다.
실수로 커밋되는 경로를 아예 만들지 않기 위해서다.

사용법:

    # 최초 1회 — 키 쌍을 만든다. 공개키는 src/licensing/keys.py 에 붙여넣는다.
    python tools/issue_license.py --genkey --out-private C:\\secure\\license_private.key

    # 발급
    python tools/issue_license.py --key-file C:\\secure\\license_private.key \\
        --cust "OO전자" --months 12
    python tools/issue_license.py --key-file C:\\secure\\license_private.key \\
        --cust "OO전자" --exp 2027-12-31

발급 내역은 `--ledger`로 준 CSV에 append 한다. 이 파일도 저장소에 넣지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import sys
from datetime import date
from pathlib import Path

# 저장소 루트를 import 경로에 넣는다 (tools/ 에서 직접 실행하므로).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from src.licensing.payload import _b32encode, build_key  # noqa: E402

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


def generate_keypair(out_private: Path) -> str:
    """키 쌍을 만들고 개인키를 저장한다. 공개키(Base32)를 돌려준다."""
    if out_private.exists():
        raise SystemExit(
            f"이미 파일이 있습니다: {out_private}\n"
            "덮어쓰면 기존 라이선스를 모두 갱신할 수 없게 됩니다. 다른 경로를 쓰세요."
        )

    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out_private.parent.mkdir(parents=True, exist_ok=True)
    out_private.write_bytes(raw)
    try:
        out_private.chmod(0o600)
    except (OSError, NotImplementedError):
        pass

    return public_key_of(private)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    raw = path.read_bytes()
    if len(raw) != 32:
        raise SystemExit(f"개인키는 32바이트여야 합니다(현재 {len(raw)}). 경로를 확인하세요.")
    return Ed25519PrivateKey.from_private_bytes(raw)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="라이선스 키를 발급한다 (개발사 전용).")
    parser.add_argument("--genkey", action="store_true", help="새 키 쌍 생성")
    parser.add_argument("--out-private", type=Path, help="--genkey 로 만들 개인키 저장 경로")
    parser.add_argument(
        "--show-public",
        action="store_true",
        help="기존 개인키에서 공개키를 다시 출력 (keys.py 복구용)",
    )
    parser.add_argument("--key-file", type=Path, help="발급에 쓸 개인키 경로")
    parser.add_argument("--cust", help="고객사명")
    parser.add_argument("--months", type=int, help="오늘부터 N개월")
    parser.add_argument("--exp", help="만료일 YYYY-MM-DD (이 날까지 유효)")
    parser.add_argument("--ledger", type=Path, help="발급 대장 CSV (append)")
    args = parser.parse_args()

    if args.genkey:
        if not args.out_private:
            raise SystemExit("--genkey 에는 --out-private 가 필요합니다.")
        public_b32 = generate_keypair(args.out_private)
        print(f"개인키 저장: {args.out_private}")
        print("\n아래 값을 src/licensing/keys.py 의 LICENSE_PUBLIC_KEY_B32 에 넣으세요:\n")
        print(f'LICENSE_PUBLIC_KEY_B32 = "{public_b32}"\n')
        print("개인키를 잃으면 갱신·재발급이 불가능하고, 유출되면 누구나 키를 만들 수 있습니다.")
        return 0

    if args.show_public:
        if not args.key_file:
            raise SystemExit("--show-public 에는 --key-file 이 필요합니다.")
        print(f'LICENSE_PUBLIC_KEY_B32 = "{public_key_of(load_private_key(args.key_file))}"')
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

    private = load_private_key(args.key_file)
    key, payload = issue(private, args.cust, expires, today)

    if args.ledger:
        append_ledger(args.ledger, payload)

    print(f"고객사 : {payload['cust']}")
    print(f"발급일 : {payload['iss']}")
    print(f"만료일 : {payload['exp']}  (이 날까지 유효)")
    print(f"일련번호: {payload['lic']}")
    print("\n라이선스 키:\n")
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
