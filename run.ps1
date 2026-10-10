# AI 讨论会 · 一键启动
#
#   .\run.ps1              起服务 + 开独立窗口（有 key 就真讨论，没有进 demo）
#   .\run.ps1 -Stop        关掉服务
#   .\run.ps1 -NoWindow    只起服务（不开窗口）
#
# ⭐ 模式自动判断：server.py 启动时看 keys.local.json / 环境变量里有没有 key ——
#    有 ≥1 个 → 真讨论（叫真模型）；一个都没有 → 回落 demo（假数据，验证界面）。
#    想显式指定：直接跑 server.py --real 或 --demo，或 --selftest 自测。

param(
  [switch]$Stop,
  [switch]$NoWindow
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$port = 8765
$url  = "http://127.0.0.1:$port"

# ⭐ 不写死本机路径（写死会把用户名带进公开仓库，别人也跑不起来）：按优先级探测
$py = $env:AI_TEATIME_PYTHON
if (-not $py -or -not (Test-Path $py)) {
  $cands = @(
    "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
  )
  $py = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
}
if (-not $py) {
  "❌ 找不到 Python。装一个，或用环境变量 AI_TEATIME_PYTHON 指定路径。"
  return
}

function Get-TeapartyProc {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*ai-teatime*server.py*" }
}

if ($Stop) {
  $procs = Get-TeapartyProc
  if ($procs) {
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "已停止 PID $($_.ProcessId)" }
  } else { "没有在跑的服务" }
  return
}

# 已经在跑就先停，避免端口占用
Get-TeapartyProc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Milliseconds 500

Start-Process -FilePath $py -ArgumentList "$root\server.py" -WindowStyle Hidden
Start-Sleep -Seconds 3

try {
  $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5
  "✅ 服务已起：$url  (HTTP $($r.StatusCode))"
} catch {
  "❌ 服务起不来：$_"
  return
}

if (-not $NoWindow) {
  $chrome = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($chrome) {
    # ⭐ URL 带时间戳：每次都是"新地址"，浏览器绝不会拿缓存里的旧页面
    #   （以前改完界面还得手动 Ctrl+F5，而且经常刷不出来）
    $urlFresh = "$url/?t=$(Get-Date -Format 'yyyyMMddHHmmss')"
    Start-Process $chrome -ArgumentList "--app=$urlFresh", '--window-size=640,940'
    "🪟 独立窗口已打开（带时间戳，绕开缓存）"
  } else {
    "（没找到 Chrome，手动开：$url）"
  }
}
