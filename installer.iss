; Inno Setup script for Study Maxxing.
;
; Installs per-user under {localappdata}\Programs (no admin elevation
; needed) rather than Program Files -- deliberate, not just a default:
; services/update_checker.py's self-update re-runs THIS installer
; silently while the app is running, and a Program Files install would
; need UAC elevation for that, which the update flow doesn't request.
;
; That self-update flow is also why CloseApplications/RestartApplications
; are set explicitly below (Inno Setup 6 already defaults to yes/yes, but
; naming them here documents that it's load-bearing, not incidental):
; Restart Manager detects StudyWarden.exe is running, closes it, installs
; over it, and relaunches it -- no custom directory-swap scripting needed
; the way a plain zip release would have required.
;
; MyAppVersion is passed in via the build command, not hardcoded here,
; so VERSION stays the single source of truth:
;   ISCC installer.iss /DMyAppVersion=0.1.0
;
; AppId is a fixed GUID (not the app name) -- Inno Setup uses it to
; recognize "this is an upgrade of the same app" across versions, so it
; must never change between releases or every future install would show
; up as a separate, duplicate entry in Add/Remove Programs.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Study Maxxing"
#define MyAppPublisher "Abdullah Alrabea"
#define MyAppExeName "StudyWarden.exe"

[Setup]
; Doubled braces are Inno Setup's escape for a literal "{" -- without
; them, the GUID's own braces get misparsed as a (nonexistent) {constant}
; reference during directive expansion.
AppId={{5FC9F329-D09B-469D-B1BF-42DB25136F13}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\StudyWarden
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=StudyWarden-Setup-v{#MyAppVersion}
SetupIconFile=ui\App Icon\Pepopolice.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=LICENSE
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=force
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\StudyWarden\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
