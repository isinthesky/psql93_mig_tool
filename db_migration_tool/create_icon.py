#!/usr/bin/env python3
"""
DB Migration Tool 아이콘 생성 스크립트
PostgreSQL 9.3 마이그레이션 도구 아이콘을 생성합니다.
"""
from PIL import Image, ImageDraw, ImageFont
import os
import math

def create_app_icon():
    """앱 아이콘 생성"""
    # 512x512 아이콘 생성
    size = 512
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 둥근 사각형 배경 (파란색)
    padding = 20
    corner_radius = 80
    
    # 둥근 사각형 그리기 함수
    def draw_rounded_rectangle(draw, box, radius, fill):
        x0, y0, x1, y1 = box
        # 모서리 원
        draw.ellipse([x0, y0, x0 + 2*radius, y0 + 2*radius], fill=fill)
        draw.ellipse([x1 - 2*radius, y0, x1, y0 + 2*radius], fill=fill)
        draw.ellipse([x0, y1 - 2*radius, x0 + 2*radius, y1], fill=fill)
        draw.ellipse([x1 - 2*radius, y1 - 2*radius, x1, y1], fill=fill)
        # 사각형 채우기
        draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
        draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    
    # 배경 그라데이션 효과
    bg_color1 = (30, 87, 153)  # 진한 파란색
    bg_color2 = (41, 116, 204)  # 밝은 파란색
    
    # 외곽 테두리
    draw_rounded_rectangle(draw, [padding, padding, size-padding, size-padding], 
                          corner_radius, bg_color1)
    
    # 내부 배경
    inner_padding = padding + 10
    draw_rounded_rectangle(draw, [inner_padding, inner_padding, 
                                 size-inner_padding, size-inner_padding], 
                          corner_radius-10, bg_color2)
    
    # 데이터베이스 실린더 그리기
    db_x = size * 0.25
    db_y = size * 0.35
    db_width = size * 0.25
    db_height = size * 0.3
    
    # 실린더 색상
    cylinder_color = (200, 210, 220)
    cylinder_dark = (150, 160, 170)
    cylinder_line = (100, 110, 120)
    
    # 실린더 상단 타원
    ellipse_height = db_height * 0.15
    draw.ellipse([db_x, db_y - ellipse_height/2, 
                  db_x + db_width, db_y + ellipse_height/2], 
                 fill=cylinder_color, outline=cylinder_line, width=3)
    
    # 실린더 몸통
    draw.rectangle([db_x, db_y, db_x + db_width, db_y + db_height], 
                   fill=cylinder_color, outline=None)
    
    # 실린더 하단 타원
    draw.ellipse([db_x, db_y + db_height - ellipse_height/2, 
                  db_x + db_width, db_y + db_height + ellipse_height/2], 
                 fill=cylinder_dark, outline=cylinder_line, width=3)
    
    # 실린더 구분선
    for i in range(2):
        y_pos = db_y + (db_height / 3) * (i + 1)
        draw.ellipse([db_x, y_pos - ellipse_height/4, 
                      db_x + db_width, y_pos + ellipse_height/4], 
                     fill=None, outline=cylinder_line, width=2)
    
    # 화살표 그리기 (주황색)
    arrow_color = (255, 152, 0)
    arrow_start_x = db_x + db_width + 30
    arrow_end_x = size * 0.75
    arrow_y = db_y + db_height / 2
    
    # 화살표 몸통
    arrow_height = 40
    draw.rectangle([arrow_start_x, arrow_y - arrow_height/2, 
                    arrow_end_x - 40, arrow_y + arrow_height/2], 
                   fill=arrow_color)
    
    # 화살표 머리
    arrow_points = [
        (arrow_end_x - 40, arrow_y - arrow_height),
        (arrow_end_x + 10, arrow_y),
        (arrow_end_x - 40, arrow_y + arrow_height)
    ]
    draw.polygon(arrow_points, fill=arrow_color)
    
    # "postgres 9.3" 텍스트 추가
    try:
        # 시스템 폰트 사용 시도
        font_size = 80
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except:
        # 기본 폰트 사용
        font = ImageFont.load_default()
    
    text = "postgres 9.3"
    # 텍스트 크기 계산
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    # 텍스트 위치 (하단 중앙)
    text_x = (size - text_width) / 2
    text_y = size * 0.75
    
    # 텍스트 그리기 (흰색)
    draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255))
    
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