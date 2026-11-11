; SPDX-License-Identifier: GPL-3.0-or-later
; Inno Setup script for the Windows installer. Build the app with PyInstaller first, then:
;   iscc /DAppVersion=0.1.0 packaging\windows\open_HOWI.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; The publisher shown in Apps & features and in the installer's file details (the same as in
; open_HOWI.spec). The "Publisher" in Windows' SmartScreen warning comes only from a code signature.
#define Publisher "Carlo Cecchetti"

[Setup]
AppId={{6E3C0F4B-9D5A-4C1E-8F2B-0A7D3E9B6C41}
AppName=open_HOWI
AppVersion={#AppVersion}
AppVerName=open_HOWI {#AppVersion}
AppPublisher={#Publisher}
AppCopyright=Copyright (C) 2026 {#Publisher}. GPL-3.0-or-later
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription=open_HOWI setup
VersionInfoProductName=open_HOWI
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#Publisher}. GPL-3.0-or-later
AppPublisherURL=https://github.com/ccarl0/open_HOWI
AppSupportURL=https://github.com/ccarl0/open_HOWI/issues
DefaultDirName={autopf}\open_HOWI
DefaultGroupName=open_HOWI
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=open_HOWI-{#AppVersion}-windows-setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\open_HOWI.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\open_HOWI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\open_HOWI"; Filename: "{app}\open_HOWI.exe"; Comment: "Hot-wire foam cutter slicer and machine control"
Name: "{autodesktop}\open_HOWI"; Filename: "{app}\open_HOWI.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\open_HOWI.exe"; Description: "{cm:LaunchProgram,open_HOWI}"; Flags: nowait postinstall skipifsilent
