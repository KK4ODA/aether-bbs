; Inno Setup script for the Aether BBS Windows installer.
; Built by .github/workflows/installers.yml:
;   iscc /DAppVersion=1.3.2 /DSourceDir=<dist\AetherBBS> /DOutputDir=<out>
;        /DIconFile=<aether-bbs.ico> /DLicenseFile=<LICENSE> aether-bbs.iss
;
; Installs for the current user by default (no administrator prompt), so the
; BBS's own updater can run it silently; the first page offers all users.

#ifndef AppVersion
  #error AppVersion must be defined
#endif

[Setup]
AppId={{7E4B1D2A-5C3F-4E8B-9A61-2F0D8C4B7A35}
AppName=Aether BBS
AppVersion={#AppVersion}
AppVerName=Aether BBS {#AppVersion}
AppPublisher=KK4ODA
AppPublisherURL=https://github.com/KK4ODA/aether-bbs
AppSupportURL=https://github.com/KK4ODA/aether-bbs/issues
AppUpdatesURL=https://github.com/KK4ODA/aether-bbs/releases
DefaultDirName={autopf}\Aether BBS
DefaultGroupName=Aether BBS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=AetherBBS-{#AppVersion}-windows-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\AetherBBS.exe
UninstallDisplayName=Aether BBS
LicenseFile={#LicenseFile}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Libraries of the previous version that this one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Aether BBS"; Filename: "{app}\AetherBBS.exe"
Name: "{autodesktop}\Aether BBS"; Filename: "{app}\AetherBBS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AetherBBS.exe"; Description: "{cm:LaunchProgram,Aether BBS}"; Flags: nowait postinstall skipifsilent

; Settings, messages and logs live in %APPDATA%\aether-bbs and are kept on
; uninstall, so a reinstall picks up where the station left off.
