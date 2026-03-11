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

[Dirs]
; Writable data directory (preserved across upgrades)
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\data\telemetry"; Permissions: users-modify
Name: "{app}\data\models"; Permissions: users-modify

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--dry-run"; Comment: "Launch KoL (dry-run mode)"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--dry-run"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch KoL Adaptive Lighting"; Parameters: "--dry-run"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up cache/temp files but NOT user data
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// Warn user that data directory is preserved on uninstall
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{app}\data')) then
    begin
      if MsgBox('Keep your telemetry data and settings?' + #13#10 +
                'Click Yes to keep the data directory, No to delete it.',
                mbConfirmation, MB_YESNO) = IDNO then
      begin
        DelTree(ExpandConstant('{app}\data'), True, True, True);
      end;
    end;
  end;
end;
