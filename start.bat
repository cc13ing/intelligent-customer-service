@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === 智能客服一键启动 ===

if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
  )
)

if "%RAG_BACKEND%"=="" set RAG_BACKEND=cloud
if "%RETRIEVER_TYPE%"=="" set RETRIEVER_TYPE=bm25
if "%CHAT_PUBLIC_ACCESS%"=="" set CHAT_PUBLIC_ACCESS=true

set PY=
if exist "C:\ProgramData\anaconda3\python.exe" set PY=C:\ProgramData\anaconda3\python.exe
if "%PY%"=="" where python >nul 2>&1 && set PY=python
if "%PY%"=="" (
  echo 未找到 Python
  pause
  exit /b 1
)

echo [1/3] 数据库迁移...
"%PY%" -m alembic upgrade head
if errorlevel 1 (
  echo 迁移失败，仍尝试启动（create_all 会补表）
)

echo [2/3] 启动 API http://localhost:8000 ...
start "zhineng-kefu" /MIN "%PY%" -m uvicorn src.main:app --host 0.0.0.0 --port 8000

timeout /t 5 /nobreak >nul

echo [3/3] 打开浏览器...
start "" "http://localhost:8000"
start "" "http://localhost:8000/admin/"

echo 完成。管理后台侧栏填写 API_KEY（见 .env）。关掉本窗口不影响服务。
pause
