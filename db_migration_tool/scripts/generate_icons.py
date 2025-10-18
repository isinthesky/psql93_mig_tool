#!/usr/bin/env python3
"""
트레이 아이콘 생성 스크립트

기본 데이터베이스 아이콘과 실행 중 아이콘을 생성합니다.
"""

from pathlib import Path

from PIL import Image, ImageDraw


def create_database_icon(size=256, color="#3B82F6", running=False):
    """데이터베이스 아이콘 생성

    Args:
        size: 아이콘 크기 (픽셀)
        color: 기본 색상 (헥스 코드)
        running: 실행 중 표시 여부

    Returns:
        PIL Image 객체
    """
    # 투명 배경 이미지 생성
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 여백 설정
    margin = int(size * 0.1)
    cylinder_width = size - 2 * margin
    cylinder_height = size - 2 * margin

    # 실린더 상단 타원 높이
    ellipse_height = int(cylinder_height * 0.2)

    # 색상 설정
    if running:
        main_color = "#10B981"  # 녹색 (실행 중)
        accent_color = "#059669"
    else:
        main_color = color  # 파란색 (대기 중)
        accent_color = "#2563EB"

    # 실린더 몸통 (사각형)
    body_top = margin + ellipse_height // 2
    body_bottom = margin + cylinder_height - ellipse_height // 2

    draw.rectangle(
        [margin, body_top, margin + cylinder_width, body_bottom],
        fill=main_color,
        outline=accent_color,
        width=2,
    )

    # 실린더 하단 타원
    draw.ellipse(
        [
            margin,
            body_bottom - ellipse_height // 2,
            margin + cylinder_width,
            body_bottom + ellipse_height // 2,
        ],
        fill=main_color,
        outline=accent_color,
        width=2,
    )

    # 실린더 상단 타원 (어두운 색으로 3D 효과)
    draw.ellipse(
        [margin, margin, margin + cylinder_width, margin + ellipse_height],
        fill=accent_color,
        outline=accent_color,
        width=2,
    )

    # 실행 중이면 작은 표시 추가
    if running:
        indicator_size = int(size * 0.15)
        indicator_x = size - margin - indicator_size
        indicator_y = margin

        # 녹색 원형 표시
        draw.ellipse(
            [indicator_x, indicator_y, indicator_x + indicator_size, indicator_y + indicator_size],
            fill="#10B981",
            outline="#059669",
            width=2,
        )

    return img


def save_icon_multi_size(image, output_path, sizes=[16, 24, 32, 48, 64, 128, 256]):
    """여러 크기의 아이콘을 ICO 파일로 저장

    Args:
        image: 원본 이미지 (PIL Image)
        output_path: 저장 경로
        sizes: 포함할 크기 리스트
    """
    # 여러 크기의 이미지 생성
    icons = []
    for size in sizes:
        resized = image.resize((size, size), Image.Resampling.LANCZOS)
        icons.append(resized)

    # ICO 파일로 저장
    icons[0].save(output_path, format="ICO", sizes=[(img.width, img.height) for img in icons], append_images=icons[1:])


def main():
    """메인 함수"""
    # 출력 디렉토리 설정
    script_dir = Path(__file__).parent
    icons_dir = script_dir.parent / "resources" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    print("🎨 트레이 아이콘 생성 중...")

    # 1. 기본 아이콘 (파란색)
    print("  - app.ico (기본 아이콘) 생성...")
    normal_icon = create_database_icon(size=256, color="#3B82F6", running=False)
    save_icon_multi_size(normal_icon, icons_dir / "app.ico")
    print(f"    ✓ 저장: {icons_dir / 'app.ico'}")

    # 2. 실행 중 아이콘 (녹색 + 표시)
    print("  - app_running.ico (실행 중 아이콘) 생성...")
    running_icon = create_database_icon(size=256, color="#10B981", running=True)
    save_icon_multi_size(running_icon, icons_dir / "app_running.ico")
    print(f"    ✓ 저장: {icons_dir / 'app_running.ico'}")

    # 3. PNG 버전도 저장 (macOS용)
    print("  - PNG 버전 생성...")
    normal_icon.save(icons_dir / "app.png", "PNG")
    running_icon.save(icons_dir / "app_running.png", "PNG")
    print(f"    ✓ 저장: {icons_dir / 'app.png'}")
    print(f"    ✓ 저장: {icons_dir / 'app_running.png'}")

    print("\n✅ 아이콘 생성 완료!")
    print(f"\n생성된 파일:")
    print(f"  - {icons_dir / 'app.ico'} (기본, 다중 크기)")
    print(f"  - {icons_dir / 'app_running.ico'} (실행 중, 다중 크기)")
    print(f"  - {icons_dir / 'app.png'} (macOS용)")
    print(f"  - {icons_dir / 'app_running.png'} (macOS용)")


if __name__ == "__main__":
    main()
