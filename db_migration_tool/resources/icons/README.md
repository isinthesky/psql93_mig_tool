# 아이콘 리소스

트레이 아이콘 및 애플리케이션 아이콘 파일들을 이 디렉토리에 배치합니다.

## 필요한 아이콘 파일

### 1. 기본 아이콘
- `app.ico` - 애플리케이션 기본 아이콘 (Windows/macOS)
  - Windows: 16x16, 32x32, 48x48 픽셀 포함 .ico 파일
  - macOS: .icns 또는 .png (시스템이 자동 조정)

### 2. 실행 중 아이콘 (선택)
- `app_running.ico` - 마이그레이션 실행 중 표시할 아이콘
  - 기본 아이콘과 동일한 형식
  - 시각적으로 구분되는 색상/디자인 권장 (예: 녹색, 애니메이션 효과 등)

## 아이콘 생성 방법

### Windows
1. 온라인 아이콘 생성 도구 사용
   - https://www.iconarchive.com/
   - https://www.favicon-generator.org/

2. 로컬 도구
   - GIMP + ICO plugin
   - IcoFX (유료)

### macOS
1. 온라인 도구
   - https://cloudconvert.com/png-to-icns
   - https://iconverticons.com/online/

2. 로컬 도구
   ```bash
   # PNG를 ICNS로 변환
   mkdir MyIcon.iconset
   sips -z 16 16     icon.png --out MyIcon.iconset/icon_16x16.png
   sips -z 32 32     icon.png --out MyIcon.iconset/icon_16x16@2x.png
   sips -z 32 32     icon.png --out MyIcon.iconset/icon_32x32.png
   sips -z 64 64     icon.png --out MyIcon.iconset/icon_32x32@2x.png
   sips -z 128 128   icon.png --out MyIcon.iconset/icon_128x128.png
   sips -z 256 256   icon.png --out MyIcon.iconset/icon_128x128@2x.png
   sips -z 256 256   icon.png --out MyIcon.iconset/icon_256x256.png
   sips -z 512 512   icon.png --out MyIcon.iconset/icon_256x256@2x.png
   sips -z 512 512   icon.png --out MyIcon.iconset/icon_512x512.png
   sips -z 1024 1024 icon.png --out MyIcon.iconset/icon_512x512@2x.png
   iconutil -c icns MyIcon.iconset
   ```

## 임시 대체 방안

아이콘 파일이 없어도 애플리케이션은 정상 작동합니다.
- TrayIconManager가 아이콘 파일이 없으면 기본 애플리케이션 아이콘 사용
- 애플리케이션 아이콘도 없으면 Qt 기본 아이콘 사용

## 디자인 가이드

### 색상
- 기본 아이콘: 파란색/회색 계열 (데이터베이스 이미지)
- 실행 중: 녹색/주황색 (활동 중임을 표시)

### 모양
- 데이터베이스 실린더 아이콘
- 화살표 (마이그레이션 방향 표시)
- 단순하고 명확한 디자인 (작은 크기에서도 인식 가능)

### 플랫폼별 스타일
- **Windows**: 풀 컬러, 3D 효과 가능
- **macOS**: 단색 템플릿 아이콘 권장 (시스템이 자동으로 다크/라이트 모드 적용)
