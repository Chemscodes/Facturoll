@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=%SCRIPT_DIR%..\.venv\Scripts\python.exe"
set "SCRIPT_PATH=%SCRIPT_DIR%filter_folder_images_gpt4_vision.py"
set "DEFAULT_CONDITIONS=%SCRIPT_DIR%filter_folder_images_gpt4_vision.conditions.example.json"

if "%~1"=="" goto :usage
if "%~2"=="" goto :usage

set "INPUT_DIR=%~1"
set "OUTPUT_DIR=%~2"
set "MODE=%~3"
set "CONDITIONS_FILE=%~4"
set "MODEL=%~5"

if "%MODE%"=="" set "MODE=invoice"
if "%CONDITIONS_FILE%"=="" set "CONDITIONS_FILE=%DEFAULT_CONDITIONS%"
if "%MODEL%"=="" set "MODEL=gpt-4o"

if not exist "%PYTHON_EXE%" (
  echo Python venv introuvable: "%PYTHON_EXE%"
  exit /b 1
)

if not exist "%SCRIPT_PATH%" (
  echo Script introuvable: "%SCRIPT_PATH%"
  exit /b 1
)

if not exist "%CONDITIONS_FILE%" (
  echo Fichier conditions introuvable: "%CONDITIONS_FILE%"
  exit /b 1
)

echo Input   : %INPUT_DIR%
echo Output  : %OUTPUT_DIR%
echo Mode    : %MODE%
echo Conds   : %CONDITIONS_FILE%
echo Model   : %MODEL%
echo.

"%PYTHON_EXE%" "%SCRIPT_PATH%" ^
  --input-dir "%INPUT_DIR%" ^
  --output-dir "%OUTPUT_DIR%" ^
  --mode "%MODE%" ^
  --conditions-file "%CONDITIONS_FILE%" ^
  --model "%MODEL%" ^
  --copy-rejected

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo ExitCode: %EXIT_CODE%
exit /b %EXIT_CODE%

:usage
echo Usage:
echo   filter_folder_images_gpt4_vision.bat ^<input_dir^> ^<output_dir^> [invoice^|requested^|tender_notice] [conditions.json] [model]
echo.
echo Exemple invoice:
echo   filter_folder_images_gpt4_vision.bat "C:\images_in" "C:\images_out"
echo.
echo Exemple requested:
echo   filter_folder_images_gpt4_vision.bat "C:\images_in" "C:\images_out" requested
echo.
echo Exemple tender notice:
echo   filter_folder_images_gpt4_vision.bat "C:\images_in" "C:\images_out" tender_notice "D:\SAAS\backend\scripts\filter_folder_tender_notice_medical_algeria.conditions.json" gpt-4o
echo.
echo Exemple avec conditions custom:
echo   filter_folder_images_gpt4_vision.bat "C:\images_in" "C:\images_out" invoice "C:\mes_regles.json" gpt-4o
exit /b 1
