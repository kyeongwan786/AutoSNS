#ifndef AppVersion
  #error AppVersion must be passed by the build script
#endif

[Setup]
AppId={{C5B9DE57-FD18-4D84-9E4A-6F7F41D10CA2}
AppName=AutoSNS
AppVersion={#AppVersion}
AppPublisher=AutoSNS
DefaultDirName={localappdata}\Programs\AutoSNS
DefaultGroupName=AutoSNS
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\dist
OutputBaseFilename=AutoSNS-Setup-{#AppVersion}
SetupIconFile=..\autosns_icon.ico
UninstallDisplayIcon={app}\AutoSNS.exe
CloseApplications=yes
RestartApplications=no
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로 가기 만들기"; GroupDescription: "바로 가기:"; Flags: unchecked

[Files]
Source: "..\dist\AutoSNS.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\AutoSNS"; Filename: "{app}\AutoSNS.exe"
Name: "{autodesktop}\AutoSNS"; Filename: "{app}\AutoSNS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoSNS.exe"; Flags: nowait
