; ================================================================
;  Sayvo — Inno Setup installer script
;  Requires Inno Setup 6 (free): https://jrsoftware.org/isinfo.php
;
;  Build:  1. Update SourceExe below to the latest dist\Sayvo_vN.exe
;          2. Compile with:  ISCC.exe installer.iss
;  Output: installer_output\Sayvo_Setup.exe
; ================================================================

#define AppName    "Sayvo"
#define AppVersion "1.0"
#define SourceExe  "dist\Sayvo_v39.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Steven Amador
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\Sayvo.exe
OutputDir=installer_output
OutputBaseFilename=Sayvo_Setup
SetupIconFile=sayvo_icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Folder (onedir) build — instant launch, no per-startup unpack.
Source: "dist\Sayvo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Sayvo.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Sayvo.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Sayvo.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
