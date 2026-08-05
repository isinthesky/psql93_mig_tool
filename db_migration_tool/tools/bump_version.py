"""버전을 올리고 흩어진 세 곳에 한 번에 반영한다.

버전이 사는 곳:
  - pyproject.toml                  패키징 메타데이터
  - src/version.py                  앱이 실행 중 표시하는 값
  - installer/DBMigrationTool.iss   인스톨러 이름·표시 버전

셋을 따로 고치면 반드시 어긋난다. 실제로 인스톨러는 1.2.1인데 앱 정보
창은 1.0.0을 말하는 상태가 있었다. 그래서 이 스크립트만 쓰게 한다.

사용법:
    python tools/bump_version.py            # patch +1 (1.2.1 -> 1.2.2)
    python tools/bump_version.py --set 1.3.0
    python tools/bump_version.py --sync     # 올리지 않고 현재 값만 전파
    python tools/bump_version.py --show     # 현재 버전만 출력

파일을 바이트로 읽고 쓴다. iss와 bat에는 한글이 들어 있고 인코딩이
제각각인데(UTF-8 / CP949), 텍스트로 다시 쓰면 인코딩이 바뀌면서 파일이
깨진다. 버전 문자열은 ASCII라 바이트 치환으로 충분하다.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (경로, 정규식) — 정규식은 그룹1=접두, 그룹2=버전, 그룹3=접미 형태여야 한다.
TARGETS: list[tuple[Path, bytes]] = [
    (ROOT / "pyproject.toml", rb'(?m)^(version = ")(\d+\.\d+\.\d+)(")'),
    (ROOT / "src" / "version.py", rb'(?m)^(__version__ = ")(\d+\.\d+\.\d+)(")'),
    (
        ROOT / "installer" / "DBMigrationTool.iss",
        rb'(?m)^(#define AppVersion ")(\d+\.\d+\.\d+)(")',
    ),
]

# 버전의 기준점. 나머지는 여기에 맞춘다.
SOURCE_OF_TRUTH = TARGETS[0]


def read_version() -> str:
    path, pattern = SOURCE_OF_TRUTH
    match = re.search(pattern, path.read_bytes())
    if not match:
        raise SystemExit(f"{path.name}에서 version을 찾지 못했습니다.")
    return match.group(2).decode("ascii")


def bump_patch(version: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def write_version(new: str) -> list[str]:
    """세 파일에 새 버전을 기록하고, 바뀐 내용을 사람이 읽을 형태로 돌려준다."""
    changes = []
    for path, pattern in TARGETS:
        if not path.exists():
            raise SystemExit(f"파일이 없습니다: {path}")

        data = path.read_bytes()
        replaced, count = re.subn(pattern, rb"\g<1>" + new.encode("ascii") + rb"\g<3>", data)
        if count == 0:
            raise SystemExit(f"{path.name}에서 버전 패턴을 찾지 못했습니다.")
        if count > 1:
            raise SystemExit(f"{path.name}에 버전 패턴이 {count}개 있습니다. 확인이 필요합니다.")

        if replaced != data:
            path.write_bytes(replaced)
            changes.append(f"  {path.relative_to(ROOT)}")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description="버전을 올리고 세 파일에 반영한다.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set", dest="explicit", metavar="X.Y.Z", help="버전을 직접 지정")
    group.add_argument("--sync", action="store_true", help="올리지 않고 현재 값만 전파")
    group.add_argument("--show", action="store_true", help="현재 버전만 출력")
    args = parser.parse_args()

    current = read_version()

    if args.show:
        print(current)
        return 0

    if args.explicit:
        if not re.fullmatch(r"\d+\.\d+\.\d+", args.explicit):
            raise SystemExit(f"버전 형식이 올바르지 않습니다: {args.explicit} (X.Y.Z 형태)")
        new = args.explicit
    elif args.sync:
        new = current
    else:
        new = bump_patch(current)

    changes = write_version(new)

    if args.sync:
        print(f"[bump_version] {new} 로 정렬" + (" (변경 없음)" if not changes else ""))
    else:
        print(f"[bump_version] {current} -> {new}")
    for line in changes:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
