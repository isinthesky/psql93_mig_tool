#!/usr/bin/env python3
"""
DB Migration Tool 아이콘 생성 스크립트
간단한 아이콘을 생성합니다.
"""
from PIL import Image, ImageDraw, ImageFont
import os

def create_app_icon():
    """앱 아이콘 생성"""
    # 512x512 아이콘 생성
    size = 512
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 배경 원 그리기 (그라데이션 효과)
    for i in range(size//2, 0, -2):
        color = int(255 * (1 - i/(size//2)))
        blue = 100 + int(155 * (i/(size//2)))
        draw.ellipse([size//2-i, size//2-i, size//2+i, size//2+i], 
                     fill=(color, color, blue, 255))
    
    # DB 아이콘 모양 그리기
    # 실린더 상단
    draw.ellipse([size//4, size//4, 3*size//4, size//4+size//8], 
                 fill=(255, 255, 255, 200))
    
    # 실린더 몸통
    draw.rectangle([size//4, size//4+size//16, 3*size//4, 3*size//4], 
                   fill=(255, 255, 255, 200))
    
    # 실린더 하단
    draw.ellipse([size//4, 3*size//4-size//16, 3*size//4, 3*size//4+size//16], 
                 fill=(255, 255, 255, 200))
    
    # 화살표 그리기 (마이그레이션 표시)
    arrow_color = (50, 200, 50, 255)
    arrow_width = 10
    # 화살표 몸통
    draw.rectangle([size//2-30, size//2-60, size//2+30, size//2+60], 
                   fill=arrow_color)
    # 화살표 머리
    draw.polygon([(size//2, size//2+100), 
                  (size//2-50, size//2+50), 
                  (size//2+50, size//2+50)], 
                 fill=arrow_color)
    
    # 아이콘 저장
    if not os.path.exists('assets'):
        os.makedirs('assets')
    
    img.save('assets/icon.png', 'PNG')
    print("✅ 아이콘 생성 완료: assets/icon.png")
    
    # macOS icns 파일 생성을 위한 다양한 크기 생성
    sizes = [16, 32, 64, 128, 256, 512]
    for s in sizes:
        resized = img.resize((s, s), Image.Resampling.LANCZOS)
        resized.save(f'assets/icon_{s}x{s}.png', 'PNG')
    
    print("💡 icns 파일을 생성하려면 다음 명령을 실행하세요:")
    print("   iconutil -c icns assets/icon.iconset")

if __name__ == "__main__":
    try:
        create_app_icon()
    except ImportError:
        print("❌ Pillow가 설치되지 않았습니다.")
        print("   pip install Pillow")