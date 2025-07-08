#!/usr/bin/env python3
"""
PNG 아이콘을 ICO 형식으로 변환
"""
from PIL import Image
import os

def create_ico():
    """ICO 파일 생성"""
    # PNG 파일 로드
    img = Image.open('assets/icon.png')
    
    # ICO에 포함할 크기들
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    
    # 각 크기별 이미지 생성
    icons = []
    for size in icon_sizes:
        # 리사이즈
        resized = img.resize(size, Image.Resampling.LANCZOS)
        icons.append(resized)
    
    # ICO 파일로 저장
    icons[0].save('src/resources/icons/app.ico', format='ICO', 
                  sizes=icon_sizes, append_images=icons[1:])
    
    print("✅ ICO 파일 생성 완료: src/resources/icons/app.ico")
    
    # macOS용 ICNS 파일 생성을 위한 iconset 디렉토리 생성
    iconset_path = 'assets/icon.iconset'
    if not os.path.exists(iconset_path):
        os.makedirs(iconset_path)
    
    # ICNS용 크기와 파일명
    icns_sizes = [
        (16, 'icon_16x16.png'),
        (32, 'icon_16x16@2x.png'),
        (32, 'icon_32x32.png'),
        (64, 'icon_32x32@2x.png'),
        (128, 'icon_128x128.png'),
        (256, 'icon_128x128@2x.png'),
        (256, 'icon_256x256.png'),
        (512, 'icon_256x256@2x.png'),
        (512, 'icon_512x512.png'),
    ]
    
    for size, filename in icns_sizes:
        resized = img.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(os.path.join(iconset_path, filename))
    
    print(f"✅ iconset 생성 완료: {iconset_path}")
    print("💡 macOS ICNS 파일을 생성하려면:")
    print(f"   iconutil -c icns {iconset_path}")

if __name__ == "__main__":
    create_ico()