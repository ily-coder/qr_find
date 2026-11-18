@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo              QR-FIND
echo Локальный запуск сайта и базы SQLite
echo ==========================================
where python >nul 2>nul
if errorlevel 1 (
  echo ОШИБКА: команда python не найдена.
  echo Установите Python 3.10+ с сайта python.org и отметьте Add Python to PATH.
  pause
  exit /b 1
)
python --version
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение...
  python -m venv .venv
)
echo Устанавливаю необходимые библиотеки...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить библиотеки. Проверьте подключение к интернету.
  pause
  exit /b 1
)
echo.
echo Сайт запускается. Не закрывайте это окно.
echo Откройте в браузере: http://127.0.0.1:8000
echo База данных будет храниться рядом с app.py в файле qrfind.db.
echo Для остановки нажмите Ctrl+C.
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
