; Build with build.ps1, which embeds the trusted uninstall command.
; /DInstallerTestMode=1 produces an isolated verification installer with no
; uninstall registration or shortcuts. It must never be published as a release.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef TranslationCleanupCommand
  #error "Use build.ps1 to embed the uninstall credential cleanup."
#endif
#define ProductName "Aisland"
#ifdef IncrementalUpdate
  #define PackageName ProductName + "-Update-" + UpdateBaseVersion + "-to-" + AppVersion + "-x64"
#else
  #define PackageName ProductName + "-Setup-" + AppVersion + "-x64"
#endif
#ifndef ProjectRoot
#define ProjectRoot AddBackslash(SourcePath) + ".."
#endif

#if FileExists(ProjectRoot + "\dist\Aisland\config.json")
  #error "Private config.json must not be present in the frozen application."
#endif
#if FileExists(ProjectRoot + "\dist\Aisland\_internal\config.json")
  #error "Private config.json must not be present in the frozen application."
#endif
#if FileExists(ProjectRoot + "\dist\Aisland\private_defaults.json")
  #error "Public installers must not contain bundled translation credentials."
#endif
#if FileExists(ProjectRoot + "\dist\Aisland\_internal\private_defaults.json")
  #error "Public installers must not contain bundled translation credentials."
#endif

[Setup]
AppId={{91E9797E-876F-44D0-A993-629B3E52A9AF}
AppName={#ProductName}
AppVersion={#AppVersion}
#ifdef IncrementalUpdate
AppVerName={#ProductName} {#UpdateBaseVersion} 至 {#AppVersion} 增量更新
#else
AppVerName={#ProductName} {#AppVersion}
#endif
AppPublisher={#ProductName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#ProductName}
VersionInfoDescription={#ProductName} 安装程序
SourceDir={#ProjectRoot}
DefaultDirName={commonpf32}\Aisland
DefaultGroupName={#ProductName}
#ifdef InstallerTestMode
PrivilegesRequired=lowest
#else
PrivilegesRequired=admin
AppMutex=Local\Aisland.App,Local\Wisland.App
#endif
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
DisableDirPage=no
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
SetupIconFile={#ProjectRoot}\assets\aisland.ico
UninstallDisplayIcon={app}\Aisland.exe
LicenseFile={#ProjectRoot}\LICENSE
OutputDir={#ProjectRoot}\dist
#ifdef InstallerTestMode
Compression=none
SolidCompression=no
OutputBaseFilename={#PackageName}-test
Uninstallable=no
UsePreviousAppDir=no
UsePreviousTasks=no
#else
Compression=zip/1
SolidCompression=no
OutputBaseFilename={#PackageName}
UsePreviousAppDir=yes
UsePreviousTasks=yes
#endif
WizardStyle=modern
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "{#ProjectRoot}\installer\Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
#ifndef InstallerTestMode
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
Name: "startmenuicon"; Description: "创建开始菜单快捷方式"; GroupDescription: "快捷方式："
#endif

[InstallDelete]
; Only an installation created by this installer may have its packaged runtime
; cleaned. User configuration/data is outside {app} and is never removed here.
#ifdef IncrementalUpdate
#include UpdateDeletes
#else
Type: filesandordirs; Name: "{app}\_internal"; Check: HasAislandInstallMarker
#endif
#ifndef InstallerTestMode
; Replace shortcuts from earlier releases, including those under the former name.
Type: files; Name: "{autodesktop}\Aisland 灵动岛.lnk"; Check: HasAislandInstallMarker
Type: files; Name: "{autoprograms}\Aisland\Aisland 灵动岛.lnk"; Check: HasAislandInstallMarker
Type: dirifempty; Name: "{autoprograms}\Aisland"; Check: HasAislandInstallMarker
Type: files; Name: "{autodesktop}\Wisland 灵动岛.lnk"
Type: files; Name: "{autoprograms}\Wisland\Wisland 灵动岛.lnk"
Type: dirifempty; Name: "{autoprograms}\Wisland"
#endif
Type: files; Name: "{app}\Wisland.exe"; Check: HasLegacyInstallMarker
Type: files; Name: "{app}\wisland-install.marker"; Check: HasLegacyInstallMarker

[Files]
#ifdef IncrementalUpdate
#include UpdateFiles
Source: "{#UpdateChecks}"; Flags: dontcopy
#else
Source: "dist\Aisland\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif
Source: "installer\aisland-install.marker"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: files; Name: "{app}\translation-install.id"

[Icons]
#ifndef InstallerTestMode
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\Aisland.exe"; WorkingDir: "{app}"; AppUserModelID: "Aisland.Desktop"; Tasks: desktopicon
Name: "{autoprograms}\{#ProductName}\{#ProductName}"; Filename: "{app}\Aisland.exe"; WorkingDir: "{app}"; AppUserModelID: "Aisland.Desktop"; Tasks: startmenuicon
#endif

[Run]
Filename: "{app}\Aisland.exe"; Description: "启动 {#ProductName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent runasoriginaluser unchecked; Check: CanLaunchApplication

[Code]
var
  MigrationFailed: Boolean;
  PendingTranslationInstall: String;

function CoCreateGuid(var Value: TGUID): Integer;
  external 'CoCreateGuid@ole32.dll stdcall';

function StringFromGUID2(var Value: TGUID; Buffer: String; Size: Integer): Integer;
  external 'StringFromGUID2@ole32.dll stdcall';

function UserHome: String;
begin
  Result := Trim(ExpandConstant('{param:USERDATA|}'));
  if Result = '' then
    Result := Trim(GetEnv('AISLAND_USER_HOME'));
  if Result = '' then
    Result := Trim(GetEnv('WISLAND_USER_HOME'));   { 旧品牌遗留的环境变量 }
  if Result <> '' then
    Exit;
  { 旧版本把用户数据放在 Wisland 目录，程序首次启动会整目录接管，这里先认它 }
  if FileExists(ExpandConstant('{localappdata}\Wisland\config.json')) and
     not FileExists(ExpandConstant('{localappdata}\Aisland\config.json')) then
    Result := ExpandConstant('{localappdata}\Wisland')
  else
    Result := ExpandConstant('{localappdata}\Aisland');
end;

function HasExistingUserConfig: Boolean;
begin
  Result := FileExists(AddBackslash(UserHome) + 'config.json');
end;

function MatchesInstallMarker(const MarkerName: String): Boolean;
var
  MarkerText: AnsiString;
begin
  Result := False;
  if LoadStringFromFile(AddBackslash(ExpandConstant('{app}')) + MarkerName, MarkerText) then
    Result := Trim(String(MarkerText)) = '91E9797E-876F-44D0-A993-629B3E52A9AF';
end;

function HasLegacyInstallMarker: Boolean;
begin
  Result := MatchesInstallMarker('wisland-install.marker');
end;

function HasAislandInstallMarker: Boolean;
begin
  Result := HasLegacyInstallMarker or
    (FileExists(ExpandConstant('{app}\Aisland.exe')) and MatchesInstallMarker('aisland-install.marker'));
end;

#ifdef IncrementalUpdate
function UpdateFileAttributes(const Path: String): Integer;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function UpdatePathSafe(const Relative: String): Boolean;
var
  Path, Parent, Remaining, Part: String;
  Index, Attributes: Integer;
begin
  Result := False;
  Path := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  Parent := Path;
  repeat
    Attributes := UpdateFileAttributes(Parent);
    if (Attributes <> -1) and ((Attributes and $400) <> 0) then Exit;
    Part := ExtractFileDir(Parent);
    if Part = Parent then Break;
    Parent := Part;
  until Parent = '';
  Remaining := Relative;
  repeat
    Index := Pos('\', Remaining);
    if Index = 0 then Part := Remaining else Part := Copy(Remaining, 1, Index - 1);
    if (Part = '') or (Part = '.') or (Part = '..') or (Pos(':', Part) > 0) then Exit;
    Path := AddBackslash(Path) + Part;
    Attributes := UpdateFileAttributes(Path);
    if (Attributes <> -1) and ((Attributes and $400) <> 0) then Exit;
    if Index = 0 then Break;
    Delete(Remaining, 1, Index);
  until False;
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Checks: TArrayOfString;
  Index, Separator: Integer;
  Expected, Relative, Path: String;
begin
  Result := '此增量包仅适用于 Aisland {#UpdateBaseVersion} 升级到 {#AppVersion}。' + #13#10 +
    '请选择旧版安装目录。版本不同或文件缺失、损坏时，请使用完整安装包。';
  if not HasAislandInstallMarker then Exit;
  ExtractTemporaryFile('update-checks.txt');
  if not LoadStringsFromFile(ExpandConstant('{tmp}\update-checks.txt'), Checks) then Exit;
  for Index := 0 to GetArrayLength(Checks) - 1 do begin
    Separator := Pos('|', Checks[Index]);
    if Separator = 0 then Exit;
    Expected := Copy(Checks[Index], 1, Separator - 1);
    Relative := Copy(Checks[Index], Separator + 1, MaxInt);
    if not UpdatePathSafe(Relative) then Exit;
    Path := AddBackslash(ExpandConstant('{app}')) + Relative;
    try
      if Expected = '-' then begin
        if FileExists(Path) or DirExists(Path) then Exit;
      end else begin
        if not FileExists(Path) then Exit;
        if CompareText(GetSHA256OfFile(Path), Expected) <> 0 then begin
          Log('Incremental base mismatch: ' + Relative);
          Exit;
        end;
      end;
    except
      Log('Incremental base file could not be read: ' + Relative);
      Exit;
    end;
  end;
  Result := '';
end;
#endif

procedure PrepareTranslationInstall;
var
  Upgrade: Boolean;
  Identity: TGUID;
  Buffer: String;
begin
  Upgrade := HasAislandInstallMarker;
  PendingTranslationInstall := '';
  if (not Upgrade) or (not FileExists(ExpandConstant('{app}\translation-install.id'))) then begin
    Buffer := StringOfChar(#0, 39);
    if (CoCreateGuid(Identity) <> 0) or (StringFromGUID2(Identity, Buffer, 39) <> 39) then
      RaiseException('无法创建翻译连接的安装记录。');
    if Upgrade then
      PendingTranslationInstall := 'upgrade:'
    else
      PendingTranslationInstall := 'fresh:';
    PendingTranslationInstall := PendingTranslationInstall + Copy(Buffer, 1, 38);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Source: String;
  AccessReport: String;
  ResultCode: Integer;
  Started: Boolean;
begin
  if CurStep = ssInstall then
    PrepareTranslationInstall;
  if CurStep <> ssPostInstall then
    Exit;
  if (PendingTranslationInstall <> '') and
     (not SaveStringToFile(ExpandConstant('{app}\translation-install.id'),
       AnsiString(PendingTranslationInstall), False)) then
    RaiseException('无法保存翻译连接的安装记录。');
  AccessReport := Trim(ExpandConstant('{param:ACCESSLOG|}'));
  if AccessReport <> '' then begin
    Started := ExecAsOriginalUser(ExpandConstant('{app}\Aisland.exe'),
      '--diagnose-access ' + AddQuotes(AccessReport),
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Log(Format('Original-user access diagnostic started: %d; exit code: %d', [Ord(Started), ResultCode]));
  end;
  Source := Trim(ExpandConstant('{param:MIGRATEFROM|}'));
  if (Source <> '') and (not HasExistingUserConfig) then begin
    Log('Importing legacy data using the application migration command.');
    Started := False;
    ResultCode := -1;
    if Pos('"', Source) = 0 then
      Started := ExecAsOriginalUser(ExpandConstant('{app}\Aisland.exe'),
        '--import-from ' + AddQuotes(Source) + ' --import-only',
        ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    MigrationFailed := (not Started) or (ResultCode <> 0);
    if MigrationFailed then begin
      Log(Format('Legacy data import failed. Process started: %d; exit code: %d', [Ord(Started), ResultCode]));
      SuppressibleMsgBox('程序已安装，但旧版数据导入未完成。' + #13#10 +
        '请核对指定的旧版目录和用户数据目录中的迁移日志，然后重试。',
        mbError, MB_OK, IDOK);
    end;
  end;
end;

function CanLaunchApplication: Boolean;
begin
  Result := (not MigrationFailed) and
    (ExpandConstant('{param:NORUN|0}') <> '1');
end;

function GetCustomSetupExitCode: Integer;
begin
  if MigrationFailed then
    Result := 10
  else
    Result := 0;
end;

function InitializeUninstall: Boolean;
begin
  Result := not CheckForMutexes('Local\Aisland.App');
  if not Result then
    SuppressibleMsgBox('{#ProductName} 正在运行，请先从托盘菜单退出，再卸载。',
      mbInformation, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StartupCommand: String;
  CleanupExitCode: Integer;
begin
  if CurUninstallStep <> usUninstall then
    Exit;
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -NonInteractive -EncodedCommand {#TranslationCleanupCommand}',
      ExpandConstant('{sys}'), SW_HIDE, ewWaitUntilTerminated, CleanupExitCode)) or
      (CleanupExitCode <> 0) then
    RaiseException('无法清除 Aisland 保存的大模型 API Key。请检查本机配置文件后重试卸载。');
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'aisland', StartupCommand) then
    if CompareText(RemoveQuotes(Trim(StartupCommand)),
        ExpandConstant('{app}\Aisland.exe')) = 0 then
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'aisland');
  { 旧品牌的随机启动项指向已不存在的 Wisland.exe，一并清掉 }
  RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'wisland');
end;
