; ============================================================
; KoL Adaptive Lighting - Inno Setup Installer Script
;
; Prerequisites:
;   1. Run build_exe.bat first to create dist\KoL\
;   2. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php
;   3. Open this file in Inno Setup Compiler and click Build
;
; Output: Output\KoL-Setup-{version}.exe
; ============================================================

#define MyAppName "KoL Adaptive Lighting"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "KoL Project"
#define MyAppExeName "KoL.exe"
#define MyAppURL "https://github.com/AndrewsTallon/KoL-v2"

[Setup]
AppId={{B8F3A2E1-7C5D-4F9A-A3E8-1D2B4C6F8A90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=KoL-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Branding - generate kol.ico from logo.png before building installer
; SetupIconFile=assets\kol.ico
; WizardSmallImageFile=assets\kol-wizard-small.bmp
PrivilegesRequired=admin
; Allow the user to change install dir
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Main application (entire dist\KoL directory)
Source: "dist\KoL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ------------------------------------------------------------
; Silicon Labs CP210x USB-to-UART Virtual COM Port driver.
; Staged to {tmp} during install and registered with pnputil
; (see [Run] below). Download the "CP210x Universal Windows
; Driver" ZIP from Silicon Labs and extract it into
;   drivers\cp210x\
; so that drivers\cp210x\silabser.inf exists.
; See README-build.md for details.
;
; This folder is gitignored; the [Files] line is skipped
; automatically if the folder is empty (skipifsourcedoesntexist).
; ------------------------------------------------------------
Source: "drivers\cp210x\*"; DestDir: "{tmp}\cp210x"; \
    Flags: deleteafterinstall recursesubdirs createallsubdirs skipifsourcedoesntexist

[Dirs]
; Writable data directory (preserved across upgrades)
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\data\telemetry"; Permissions: users-modify
Name: "{app}\data\models"; Permissions: users-modify
Name: "{app}\data\profiles"; Permissions: users-modify

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Launch KoL Adaptive Lighting"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Install the CP210x driver with pnputil (built into Windows 7+). Idempotent:
; re-running a setup with the same INF is a no-op. We only run this when the
; staged INF is actually present (see Check function).
Filename: "{sys}\pnputil.exe"; \
    Parameters: "/add-driver ""{tmp}\cp210x\silabser.inf"" /install"; \
    StatusMsg: "Installing CP210x USB-to-UART driver (Silicon Labs)..."; \
    Flags: waituntilterminated runhidden; \
    Check: Cp210xDriverStaged

Filename: "{app}\{#MyAppExeName}"; Description: "Launch KoL Adaptive Lighting"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up cache/temp files but NOT user data
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// True when the CP210x driver INF has been staged to {tmp}\cp210x.
// Used to skip the pnputil [Run] step on builds that don't bundle the driver.
function Cp210xDriverStaged(): Boolean;
begin
  Result := FileExists(ExpandConstant('{tmp}\cp210x\silabser.inf'));
end;

// Warn user that data directory is preserved on uninstall
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{app}\data')) then
    begin
      if MsgBox('Keep your telemetry, profiles, evaluations, and settings?' + #13#10 +
                'Click Yes to keep the data directory, No to delete it.',
                mbConfirmation, MB_YESNO) = IDNO then
      begin
        DelTree(ExpandConstant('{app}\data'), True, True, True);
      end;
    end;
  end;
end;
