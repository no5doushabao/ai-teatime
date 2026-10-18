# AI 讨论会 · 一键安装（Windows）
#
#   做三件事：
#     ① 找到可用的 Python
#     ② 准备 keys.local.json（没有就从模板复制一份，等你填 key）
#     ③ 在桌面创建快捷方式（图标用 assets/ayu.ico）
#
#   用法：在这个目录里右键「在终端中打开」，然后
#     .\install.ps1                     # 装到桌面
#     .\install.ps1 -ShortcutDir D:\xx  # 装到指定目录
#     .\install.ps1 -NoShortcut         # 只做准备，不建快捷方式

param(
  [switch]$NoShortcut,
  [string]$ShortcutDir = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

"🍵 AI 讨论会 · 安装"
""

# ── ① 找 Python（不写死本机路径）──
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
  "❌ 没找到 Python。装一个（python.org），或用环境变量 AI_TEATIME_PYTHON 指定路径。"
  return
}

# ⚠️ 光看路径不够 —— Windows 商店那个 "python" 是个 **0 字节的占位存根**：
#    Test-Path 过得去、Get-Command 也找得到，但一跑就 `exit 9009`、啥也不输出。
#    （2026-10-18 在阿包这台机器上实测到的：`& python --version` → LASTEXITCODE=9009）
#    ⇒ 这里**真的跑一次** --version 来验，别让用户装完发现双击没反应。
$pyVer = ""
try {
  $pyVer = (& $py --version 2>&1 | Out-String).Trim()
  if ($LASTEXITCODE -ne 0 -or -not $pyVer) { throw "退出码 $LASTEXITCODE" }
} catch {
  "⚠️ 这个 python 跑不起来（$($_.Exception.Message)）：$py"
  if ($py -match '\\WindowsApps\\') {
    "   它是**微软商店的占位程序**，不是真的 Python —— 这就是「双击没反应 / 报 9009」的元凶。"
  }
  "   装一个真的（https://www.python.org/downloads/），或者用环境变量指定："
  '   $env:AI_TEATIME_PYTHON = "C:\path\to\python.exe"'
  "   然后再跑一次 .\install.ps1"
  return
}
"✅ Python：$py（$pyVer）"

# ── ② 准备 keys.local.json ──
if (-not (Test-Path "$root\keys.local.json")) {
  Copy-Item "$root\keys.example.json" "$root\keys.local.json"
  "📝 已从模板生成 keys.local.json —— ⭐ **记得往里填至少一个 API key**，否则只能跑 demo 模式"
} else {
  "✅ keys.local.json 已存在（没动它）"
}

# ── ③ 建快捷方式 ──
if (-not $NoShortcut) {
  if (-not (Test-Path $ShortcutDir)) { New-Item -ItemType Directory -Force -Path $ShortcutDir | Out-Null }
  $lnkPath = Join-Path $ShortcutDir 'AI 讨论会.lnk'
  $ws = New-Object -ComObject WScript.Shell
  $s = $ws.CreateShortcut($lnkPath)
  $s.TargetPath = 'powershell.exe'
  $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\run.ps1`""
  $s.WorkingDirectory = $root
  $ico = Join-Path $root 'assets\ayu.ico'
  if (Test-Path $ico) { $s.IconLocation = "$ico,0" }
  $s.Description = 'AI 讨论会 · 一键开场（起服务 + 开独立窗口）'
  $s.Save()
  "🪟 快捷方式已创建：$lnkPath"
}

""
"完成。双击那个快捷方式就能开场 🍵"
"（想直接在终端跑：.\run.ps1　关服务：.\run.ps1 -Stop　自测：python server.py --selftest）"
