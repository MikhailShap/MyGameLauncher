; CyberLauncher — Inno Setup script
; Сборка: установить Inno Setup (https://jrsoftware.org/isdl.php),
; затем запустить:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Или в PowerShell из корня проекта:
;   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; На выходе: installer_output\CyberLauncher_Setup.exe — один файл, который
; можно отдать другу. Установщик сам распакует приложение, создаст ярлыки
; и зарегистрирует деинсталлятор.

#define MyAppName "CyberLauncher"
#define MyAppVersion "1.9.1"
#define MyAppPublisher "MikhailShap"
#define MyAppURL "https://github.com/MikhailShap/MyGameLauncher"
#define MyAppExeName "CyberLauncher.exe"
#define MySourceDir "dist\CyberLauncher"

[Setup]
; Уникальный AppId — нужен Windows для отслеживания установок и обновлений.
; Сгенерирован один раз; НЕ менять между версиями того же приложения.
AppId={{8A6F4C2D-5B91-4E37-9D4F-1A5C9E2B7F84}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Иконка установщика и uninstaller'а
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; Сжатие — lzma2/ultra даёт ~30-40% экономии для PyInstaller bundle
Compression=lzma2/ultra
SolidCompression=yes
; Куда складывать готовый installer
OutputDir=installer_output
OutputBaseFilename=CyberLauncher_Setup
; 64-битная архитектура (PyInstaller собирает x64)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Современный wizard
WizardStyle=modern
; Юзер сможет выбрать "только для меня" или "для всех" если не админ
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Опциональные ярлыки — пользователь снимает галочку если не нужно
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Тащим всё содержимое dist/CyberLauncher/ в {app}/
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Меню Пуск
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
; Рабочий стол (опционально, если юзер выбрал task)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; После установки предложить сразу запустить
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; При деинсталляции почистить cache и data, иначе папка останется с мусором
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\*.log"
