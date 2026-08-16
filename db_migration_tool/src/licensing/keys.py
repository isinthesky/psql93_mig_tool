"""라이선스 검증용 공개키.

개인키는 이 저장소에 **없다.** 개발사가 따로 보관하며,
`tools/issue_license.py --genkey`로 쌍을 만들고 여기에는 공개키만 붙여넣는다.

공개키가 노출되는 것은 문제가 되지 않는다 — 서명을 만들 수는 없기 때문이다.
반대로 대칭키(Fernet)를 썼다면 앱 안의 키로 누구나 라이선스를 위조할 수 있다.
"""

from __future__ import annotations

# Ed25519 공개키 32바이트를 Base32(패딩 없음, 대문자)로 적는다.
# 값이 비어 있으면 검증이 항상 실패하므로, 배포 전에 반드시 채워야 한다.
#
# 이 값을 바꾸면 **이미 발급한 모든 라이선스가 한꺼번에 무효가 된다.**
# 개인키를 잃어 어쩔 수 없는 경우가 아니면 건드리지 않는다.
# 값을 잃었다면 개인키에서 언제든 다시 뽑을 수 있다:
#     python tools/issue_license.py --show-public --key-file <개인키경로>
LICENSE_PUBLIC_KEY_B32 = "NIVHG532XSXX7YERKROEOP3SKSP5VNODHIJ7URFKQ43Y747KR5DQ"


def has_public_key() -> bool:
    """공개키가 심어져 있는가.

    비어 있으면 모든 키가 INVALID가 된다. 개발 중에 이 상태로 빌드해
    "왜 전부 미인증이지"로 헤매는 일이 잦으므로 따로 확인할 수 있게 둔다.
    """
    return bool(LICENSE_PUBLIC_KEY_B32.strip())
