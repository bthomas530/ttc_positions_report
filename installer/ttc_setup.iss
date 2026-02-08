; TTC Positions Report - Inno Setup Installer Script
; This creates a proper Windows installer with desktop shortcut
; 
; Requirements:
;   - Inno Setup 6.x (free download from https://jrsoftware.org/isinfo.php)
;   - Built executable from PyInstaller (dist/TTC Positions Report.exe)
;   - Icon file (installer/icon.ico)
;
; To build the installer:
;   1. Install Inno Setup
;   2. Open this file in Inno Setup Compiler
;   3. Click Build > Compile
;   4. Find the installer in Output/ folder

#define MyAppName "TTC Positions Report"
#define MyAppVersion "2.0.4"
#define MyAppPublisher "TTC"
#define MyAppURL "https://github.com/your-username/ttc-positions"
#define MyAppExeName "TTC Positions Report.exe"

[Setup]
; Basic installer info
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; Output settings
OutputDir=..\Output
OutputBaseFilename=TTC_Positions_Setup_{#MyAppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

; Compression
Compression=lzma2/max
SolidCompression=yes

; Modern look
WizardStyle=modern
WizardSizePercent=100

; Privileges - install for current user (no admin needed)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Uninstall previous version first
CloseApplications=yes
CloseApplicationsFilter=*.exe

; Misc
DisableWelcomePage=no
DisableDirPage=auto
DisableFinishedPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Desktop icon task
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checked

[Files]
; Main executable
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Resources folder (if exists)
Source: "..\dist\resources\*"; DestDir: "{app}\resources"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: DirExists(ExpandConstant('..\dist\resources'))

[Icons]
; Start menu shortcut
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

; Desktop shortcut
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Option to launch app after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Custom code to handle upgrades gracefully

function InitializeSetup(): Boolean;
begin
  Result := True;
  // Could add pre-install checks here
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Post-install actions could go here
  end;
end;

// Check if a directory exists
function DirExists(Path: String): Boolean;
begin
  Result := DirExists(Path);
end;

[UninstallDelete]
; Clean up logs and config on uninstall (optional - comment out to keep user data)
; Type: filesandordirs; Name: "{app}\log"
; Type: files; Name: "{app}\ttc_watchlist.json"
; Type: files; Name: "{app}\version.json"

