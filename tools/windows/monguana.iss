; Monguana's Windows installer — ROADMAP phase 38. Built by
; tools/windows/build.py --installer, which passes the defines below.
;
; Per-user, no administrator rights: installs to
; %LOCALAPPDATA%\Programs\Monguana. Data (SQLite, keys, logs) lives apart in
; %LOCALAPPDATA%\Monguana, so upgrades keep it; uninstall asks about it.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (build.py does)
#endif
#ifndef BundleDir
  #error Pass /DBundleDir=<build\windows\bundle> (build.py does)
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "monguana.ico"
#endif

[Setup]
; Never change AppId: Windows matches upgrades and the uninstaller by it.
AppId={{6C1B2B8E-4F0B-4B3A-9E55-5D3A5F0C7A21}
AppName=Monguana
AppVersion={#AppVersion}
AppVerName=Monguana {#AppVersion}
AppPublisher=OldManGan
AppPublisherURL=https://github.com/El-Iguana/monguana_wapyt
DefaultDirName={localappdata}\Programs\Monguana
DefaultGroupName=Monguana
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Monguana-{#AppVersion}-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\app\monguana.ico
UninstallDisplayName=Monguana
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile={#BundleDir}\app\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Monguana when I sign in (in the tray, without opening the browser)"; GroupDescription: "Startup:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the runtime and the app wholesale, so a package or
; module dropped by the new version does not linger from the old one.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Monguana"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\monguana_launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\monguana.ico"; Comment: "MongoDB manager in your browser"
Name: "{group}\Reset Monguana password"; Filename: "{app}\Reset Monguana password.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\app\monguana.ico"
Name: "{group}\Uninstall Monguana"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Monguana"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\monguana_launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\monguana.ico"; Tasks: desktopicon
Name: "{userstartup}\Monguana"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\monguana_launcher.py"" --no-browser"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\monguana.ico"; Tasks: startup

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\monguana_launcher.py"""; WorkingDir: "{app}\app"; Description: "Start Monguana now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; A running server holds files open under {app}; stop it first.
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\monguana_launcher.py"" --stop"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "StopMonguana"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\app\appcode"
Type: filesandordirs; Name: "{app}\python"

[Code]
const
  DataDirName = 'Monguana';

// Stop a running Monguana before files are replaced during an upgrade.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Launcher, Python: String;
  ResultCode: Integer;
begin
  Result := '';
  Launcher := ExpandConstant('{app}\app\monguana_launcher.py');
  Python := ExpandConstant('{app}\python\python.exe');
  if FileExists(Launcher) and FileExists(Python) then
    Exec(Python, '"' + Launcher + '" --stop', ExpandConstant('{app}\app'),
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Saved connections, accounts and the key that decrypts their passwords live
// outside {app}. Keep them unless the person says otherwise; a silent
// uninstall always keeps them.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\' + DataDirName);
  if not DirExists(DataDir) or UninstallSilent then
    exit;
  if MsgBox('Also delete your Monguana data?' + #13#10 + #13#10 +
            'This removes your accounts, saved connections and the key that ' +
            'decrypts their passwords:' + #13#10 + DataDir,
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
