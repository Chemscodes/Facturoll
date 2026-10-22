; FactuRoll Windows installer (Inno Setup 6)

#define MyAppName "FactuRoll"
#define MyAppVersion "0.1.1"
#define MyBuildDate GetDateTimeString('yyyy-mm-dd', '-', ':')
#define MyAppPublisher "FactuRoll"
#define MyAppExeName "FactuRoll.exe"
#define MyBuildDir "..\\build\\windows\\x64\\runner\\Release"
#define VCRedistExeName "vc_redist.x64.exe"

[Setup]
AppId={{A2ED8A1D-43DF-46B2-8F5E-3F50E6D6A8D0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=FactuRoll-Setup-v{#MyAppVersion}-{#MyBuildDate}
SetupIconFile=..\\windows\\runner\\resources\\app_icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; Excludes: "trial_desktop.exe"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "prerequisites\{#VCRedistExeName}"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\{#VCRedistExeName}"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Microsoft Visual C++ Runtime..."; Flags: waituntilterminated runhidden; Check: not IsVCRedistInstalled
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function IsVCRedistInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result := False;
  if RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then
  begin
    Result := (Installed = 1);
  end;
end;
