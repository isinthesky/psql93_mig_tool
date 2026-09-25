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
#define AppVersion "1.2.7"
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

[Dirs]
; 라이선스 씨앗 전용 폴더(감사 M-07). 앱이 활성화에 성공하면 씨앗을 곧바로 지우는데,
; 관리자 설치(Program Files)에서는 일반 사용자가 설치 폴더에 쓸 수 없다. 이 폴더에만
; Users 수정 권한을 주고 설치 폴더 나머지는 그대로 읽기 전용으로 둔다.
Name: "{app}\seed"; Permissions: users-modify

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

[UninstallDelete]
; 설치 중 만든 씨앗 파일. [Files] 로 넣은 게 아니라 자동 삭제되지 않는다.
; 사용자별 license.key 와 .activation 은 지우지 않는다 — 재설치 후에도 등록이 유지돼야 한다.
Type: filesandordirs; Name: "{app}\seed"
; 1.2.7 이하 인스톨러가 남긴 위치.
Type: files; Name: "{app}\license.seed"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Code]
const
  { 앱(src/licensing/activation.py)의 SEED_DIRNAME / LICENSE_SEED_FILENAME 과 같아야 한다. }
  SeedDirName = 'seed';
  SeedFileName = 'license.seed';

var
  LicensePage: TInputQueryWizardPage;
  LicenseKeyFromFile: String;

(* 라이선스 키 전달 규칙 (감사 M-07)

   - 키를 명령줄 값으로 받지 않는다. /LICENSEKEY=<키> 는 프로세스 목록·배포 스크립트·
     설치 로그(명령줄 기록)에 평문으로 남는다. 넘어오면 설치를 중단하고 /LICENSEFILE 을 안내한다.
   - 무음 설치(/VERYSILENT)는 /LICENSEFILE=<경로> 로 키가 든 파일의 경로만 넘긴다.
     로그에는 경로만 남는다. 파일 관리(배포 후 삭제)는 배포 담당자의 몫이다.
   - 입력 필드는 마스킹한다. 키 값은 어디에도 Log() 하지 않는다. *)

function FindParamValue(const Prefix: String): String;
var
  I: Integer;
  Param: String;
begin
  Result := '';
  for I := 1 to ParamCount do
  begin
    Param := ParamStr(I);
    if Pos(Prefix, Uppercase(Param)) = 1 then
    begin
      Result := Copy(Param, Length(Prefix) + 1, MaxInt);
      Exit;
    end;
  end;
end;

function HasLicenseKeyParam: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if Pos('/LICENSEKEY=', Uppercase(ParamStr(I))) = 1 then
    begin
      Result := True;
      Exit;
    end;
end;

{ 키 문자(영숫자·하이픈)만 남긴다. 파일의 BOM·줄바꿈·공백을 걸러낸다.
  하이픈·대소문자 정규화와 서명 검증은 앱이 한다. }
function SanitizeKey(const S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if ((C >= '0') and (C <= '9')) or ((C >= 'A') and (C <= 'Z')) or
       ((C >= 'a') and (C <= 'z')) or (C = '-') then
      Result := Result + C;
  end;
end;

{ /LICENSEFILE 을 읽는다. 지정했는데 읽을 수 없거나 비어 있으면 False — 무음 배포가
  조용히 미등록 상태로 끝나지 않게 설치를 멈춘다. }
function LoadLicenseFileParam: Boolean;
var
  Path: String;
  Raw: AnsiString;
begin
  Result := True;
  LicenseKeyFromFile := '';
  Path := RemoveQuotes(FindParamValue('/LICENSEFILE='));
  if Path = '' then
    Exit;
  if not LoadStringFromFile(Path, Raw) then
  begin
    Log('License file could not be read: ' + Path);
    Result := False;
    Exit;
  end;
  LicenseKeyFromFile := SanitizeKey(String(Raw));
  if LicenseKeyFromFile = '' then
  begin
    Log('License file is empty: ' + Path);
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  LicensePage := CreateInputQueryPage(wpSelectDir,
    '라이선스 등록',
    '공급사에서 받은 라이선스 키를 입력하세요.',
    '지금 비워 두어도 설치는 진행됩니다. 나중에 프로그램의 라이선스 창에서 등록할 수 있습니다.' + #13#10 +
    '하이픈과 대소문자는 신경 쓰지 않아도 됩니다. 입력한 키는 화면에 표시되지 않습니다.');
  { 두 번째 인자 True = 비밀번호 필드(마스킹). }
  LicensePage.Add('라이선스 키:', True);
end;

(* 키를 설치 폴더의 씨앗 전용 폴더({app}\seed)에 남긴다.

   사용자 로컬 폴더에 직접 쓰지 않는 이유: 그 경로는 '설치를 실행한 계정'을 가리킨다.
   관리자로 승격해 설치하면 실제로 프로그램을 쓰는 사용자와 다른 계정이 되어
   키가 엉뚱한 곳에 남는다. 앱이 첫 실행에서 자기 계정 폴더로 옮기고, 활성화에
   성공하면 씨앗을 곧바로 지운다(평문 키 최소 수명). *)
procedure SaveLicenseSeed;
var
  Key: String;
  LegacySeed: String;
  Legacy: AnsiString;
begin
  Key := '';
  if LicensePage <> nil then
    Key := Trim(LicensePage.Values[0]);
  if Key = '' then
    Key := LicenseKeyFromFile;

  (* 1.2.7 이하는 {app}\license.seed 에 두었다. 새 키가 없으면 새 위치로 옮기고
     (앱이 활성화 후 지운다), 어느 쪽이든 옛 위치의 파일은 여기서 지운다. *)
  LegacySeed := ExpandConstant('{app}\' + SeedFileName);
  if FileExists(LegacySeed) then
  begin
    if (Key = '') and LoadStringFromFile(LegacySeed, Legacy) then
      Key := SanitizeKey(String(Legacy));
    DeleteFile(LegacySeed);
  end;

  { 빈 값이면 아무것도 하지 않는다. 덮어쓰면 앱에서 갱신한 키가 무음 업그레이드로 지워진다. }
  if Key = '' then
    Exit;

  ForceDirectories(ExpandConstant('{app}\' + SeedDirName));
  SaveStringToFile(ExpandConstant('{app}\' + SeedDirName + '\' + SeedFileName), Key + #13#10, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveLicenseSeed;
end;

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

  if HasLicenseKeyParam then
  begin
    Log('Rejected /LICENSEKEY= parameter (license keys must not be passed on the command line).');
    SuppressibleMsgBox('/LICENSEKEY 옵션은 더 이상 지원하지 않습니다.' + #13#10 +
      '명령줄의 라이선스 키는 프로세스 목록과 설치 로그에 노출됩니다.' + #13#10#13#10 +
      '키를 파일에 저장한 뒤 /LICENSEFILE=<경로> 로 지정하세요.',
      mbCriticalError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;

  if not LoadLicenseFileParam then
  begin
    SuppressibleMsgBox('/LICENSEFILE 로 지정한 라이선스 파일을 읽을 수 없거나 비어 있습니다.',
      mbCriticalError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;

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
