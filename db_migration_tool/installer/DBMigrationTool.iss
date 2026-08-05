; DB Migration Tool - Inno Setup 인스톨러
;
; 빌드: installer\build_installer.bat  (또는)
;       "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\DBMigrationTool.iss
;
; 선행 조건: dist\DBMigrationTool.exe 가 있어야 한다(PyInstaller 빌드 먼저).
;
; 이 인스톨러가 푸는 문제:
;   대상 PC에 VC++ 런타임이 없으면 PySide6의 QtCore DLL 로드가 실패한다(커밋 0c53eb6).
;   지금까지는 prerequisites 폴더와 안내문을 같이 주고 사용자가 순서대로 설치해야 했다.
;   여기서는 인스톨러가 조용히 선설치한다.

#define AppName "DB Migration Tool"
#define AppVersion "1.0.0"
#define AppPublisher "CIMON"
#define ExeName "DBMigrationTool.exe"

; 저장소 루트 기준 상대 경로 (이 .iss는 installer\ 안에 있다)
#define SourceDir ".."

[Setup]
AppId={{8F3A2D71-4C6B-4E29-9A17-3B5D8E2F71C4}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\DBMigrationTool
DefaultGroupName={#AppName}
; 관리자 권한이 있으면 전체 PC에, 없으면 사용자 계정에만 설치한다.
; 사내 PC에 관리자 권한이 없는 경우가 흔하다.
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#SourceDir}\dist\installer
OutputBaseFilename=DBMigrationTool-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\resources\icons\psql_migration_tool.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; exe가 이미 64비트다. 32비트 Windows에서는 설치를 막는다.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows Server 2012 R2 지원 (6.3). 그 이하는 막는다.
MinVersion=6.3
UninstallDisplayIcon={app}\{#ExeName}
DisableProgramGroupPage=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 아이콘:"

[Files]
Source: "{#SourceDir}\dist\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion
; VC++ 런타임 재배포 패키지. 설치 후에는 남기지 않는다.
Source: "{#SourceDir}\dist\prerequisites\vc_redist.x64.exe"; DestDir: "{tmp}"; \
    Flags: deleteafterinstall
; Windows Server 2012 R2용 KB2999226 안내. 이건 자동 설치가 불가능하다
; (Windows Update 패키지라 별도 절차가 필요하다).
Source: "{#SourceDir}\dist\prerequisites\설치안내.txt"; DestDir: "{app}"; \
    DestName: "사전요구사항 안내.txt"; Flags: ignoreversion isreadme

[Run]
; 이미 설치돼 있으면 vc_redist가 알아서 건너뛴다. 재부팅은 막는다
; (설치 도중 재부팅되면 사용자가 상황을 잃는다).
Filename: "{tmp}\vc_redist.x64.exe"; \
    Parameters: "/install /quiet /norestart"; \
    StatusMsg: "Visual C++ 런타임 확인 중..."; \
    Check: NeedsVCRedist; \
    Flags: waituntilterminated
Filename: "{app}\{#ExeName}"; Description: "{#AppName} 실행"; \
    Flags: nowait postinstall skipifsilent

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Code]
function NeedsVCRedist: Boolean;
var
  Installed: Cardinal;
begin
  { VC++ 2015-2022 재배포 패키지의 설치 표식.
    이미 있으면 설치 관리자를 띄우지 않는다 — 매번 돌리면 설치가 느려지고,
    최신 버전이 깔린 PC에서 다운그레이드 경고가 뜬다. }
  Result := True;
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                        'Installed', Installed) then
    Result := Installed = 0;
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  { Windows Server 2012 R2(6.3)에서는 KB2999226이 먼저 필요하다.
    UCRT가 없으면 VC++ 런타임을 깔아도 실행되지 않는다. 막지는 않고 알린다 —
    이미 설치된 PC를 구분할 확실한 방법이 없다. }
  if (GetWindowsVersion shr 24 = 6) and ((GetWindowsVersion shr 16) and $FF = 3) then
    MsgBox('Windows 8.1 / Server 2012 R2에서는 KB2999226(Universal C Runtime)이'
           + #13#10 + '먼저 설치되어 있어야 합니다.' + #13#10#13#10
           + '설치 후 프로그램이 실행되지 않으면 설치 폴더의'
           + #13#10 + '"사전요구사항 안내.txt"를 참고하세요.',
           mbInformation, MB_OK);
end;
