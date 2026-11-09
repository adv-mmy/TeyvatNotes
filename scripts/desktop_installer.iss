#ifndef AppVersion
  #define AppVersion "0.8.1"
#endif
#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef AppIdentity
  #define AppIdentity "{{58FCC0B6-2C5F-4524-B8DE-7C6D3B7496E2}"
#endif
#ifndef ShortcutName
  #define ShortcutName "提瓦特手记"
#endif

[Setup]
AppId={#AppIdentity}
AppName=提瓦特手记
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\TeyvatNotes
DefaultGroupName=提瓦特手记
UninstallDisplayIcon={app}\TeyvatNotes.exe
OutputBaseFilename=TeyvatNotes-Setup
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
Compression=lzma2/fast
SolidCompression=yes
DisableProgramGroupPage=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[InstallDelete]
; 清理早期构建误带入的 ICU，仅限已有的提瓦特手记安装目录。
Type: files; Name: "{app}\_internal\icuuc.dll"; Check: ExistingTeyvatBundle
Type: files; Name: "{app}\_internal\icudt78.dll"; Check: ExistingTeyvatBundle

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "desktop_config.env"; DestDir: "{localappdata}\TeyvatNotes"; DestName: ".env"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{userprograms}\{#ShortcutName}"; Filename: "{app}\TeyvatNotes.exe"; WorkingDir: "{app}"

[Code]
function ExistingTeyvatBundle: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\TeyvatNotes.exe'));
end;
