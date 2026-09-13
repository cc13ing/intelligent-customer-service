# 智能客服一键启动（PowerShell）
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

Write-Host "=== 智能客服一键启动 ===" -ForegroundColor Magenta

# 加载 .env
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    if ($_ -match '^\s*([^=]+)=(.*)$') {
      [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim().Trim('"').Trim("'"), "Process")
    }
  }
}
if (-not $env:RAG_BACKEND) { $env:RAG_BACKEND = "cloud" }
if (-not $env:RETRIEVER_TYPE) { $env:RETRIEVER_TYPE = "bm25" }
if (-not $env:CHAT_PUBLIC_ACCESS) { $env:CHAT_PUBLIC_ACCESS = "true" }

function Find-Python {
  $candidates = @(
    "C:\ProgramData\anaconda3\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "python"
  )
  foreach ($c in $candidates) {
    if ($c -eq "python") {
      $cmd = Get-Command python -ErrorAction SilentlyContinue
      if ($cmd) { return $cmd.Source }
    } elseif (Test-Path $c) {
      return $c
    }
  }
  return $null
}

$py = Find-Python
if (-not $py) {
  Write-Host "未找到 Python，请安装 Anaconda/Python 或加入 PATH" -ForegroundColor Red
  exit 1
}
Write-Host "Python: $py"

Write-Host "[0/4] 依赖检查提示"
Write-Host "  - Postgres: $env:DATABASE_URL"
Write-Host "  - Redis:    $env:REDIS_URL (可选，挂了会 degraded)"

Write-Host "[1/4] alembic upgrade head"
& $py -m alembic upgrade head

Write-Host "[2/4] 启动 uvicorn :8000"
# 若端口占用则提示
$busy = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($busy) {
  Write-Host "端口 8000 已被占用 (PID=$($busy.OwningProcess))，将直接打开网页" -ForegroundColor Yellow
} else {
  $proc = Start-Process -FilePath $py -ArgumentList "-m","uvicorn","src.main:app","--host","0.0.0.0","--port","8000" -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Minimized
  Write-Host "PID=$($proc.Id)"
  Start-Sleep -Seconds 5
}

Write-Host "[3/4] 健康检查"
try {
  $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
  Write-Host ("health=" + ($health | ConvertTo-Json -Compress)) -ForegroundColor Green
} catch {
  Write-Host "健康检查失败: $_" -ForegroundColor Yellow
}

Write-Host "[4/4] 打开浏览器"
Start-Process "http://localhost:8000"
Start-Process "http://localhost:8000/admin/"

Write-Host "完成。管理后台请在侧栏填写 API_KEY=$($env:API_KEY)" -ForegroundColor Green
