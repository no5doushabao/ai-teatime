#!/usr/bin/env python3
"""AI 茶话会 · 本地服务（零依赖：只用 Python 标准库）。

阿包 2026-10-09 定的机制：
    ⭐ **圆桌制**：一轮里五个人**依次各说一次**，等齐了才进下一轮。
      不是"收到回复立刻深度思考"的链式反应 —— 那样会变成 A 回 B、B 回 C 的雪球。
    阿包本人有"旁听席"：能看全程，也能插话（插的话进**下一轮**的上下文）。

三种运行模式：
    --demo      假数据，验证界面手感（不需要任何 key）
    --real      真对话：读 config.local.json + keys.local.json，调各家 OpenAI 兼容端点
    --selftest  起一个本地 mock 端点，验证"调用链路"（不需要真 key）
    默认：能读到至少一个 key 就走 real，否则降级 demo

配置文件：
    config.local.json   嘉宾名单 / 角色卡 / 话题 / 轮数（模板见 config.example.json）
    keys.local.json     只放 key（模板见 keys.example.json）—— ⚠️ 别提交，.gitignore 已排除
    key 优先从 keys.local.json 读，读不到再找同名环境变量。

接口（前端只认这四个）：
    GET  /                 界面
    GET  /avatars/<who>.png 头像
    GET  /stream           SSE：实时推消息
    POST /say              {"text": "..."} 阿包插话
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import queue
import random
import re
import subprocess
import threading
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:   # ⚠️ Windows 控制台默认 GBK，直接 print("✓") 会 UnicodeEncodeError（§8.2 老坑）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

# ── ⭐ 版本号（阿包 2026-10-12 定的：**0.2.0**）──────────────────────────
#  他说「0.9 太高了，1.0 好像就是可以发布的成熟产品了吧」——阿鱼同意，而且按 semver，
#  `0.9.x` 意味着"**API 基本冻结、只差最后一哆嗦**"，可我们还在改核心行为
#  （比如"喂什么上下文给各位"）。`0.x` = 早期开发期，接口/行为随时会变。
#  等这几条都满足了再打 **1.0**：
#    ① 连着跑几场**不用修** ② config / 日志头 / afterglow 这些**格式冻住**（改就得写迁移）
#    ③ 别人 clone 下来能**一次装成** ④ 前端有最低限度的自动检查 ⑤ 待办清空
#  ⚠️ 每次改动较大时记得抬这个号 —— 它出现在三处：界面右上角、每场日志的文件头、`GET /state`。
VERSION = "0.3.0"        # 0.3.0（2026-10-18）：⚙ 设置弹窗 / 👑 主持人可选 / 🤖 模型选单 / 续场换人提示 / 昵称改造
#                          0.2.1（10-15）：发言可复制 / 链接可点 / 输入框多行


def _git_short() -> str:
    """当前提交的短 hash（取不到就空着）—— 用来回答"我跑的是不是最新那次提交"。"""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


BUILD = _git_short()                                   # 短 hash（没装 git / 不在仓库里就空）
STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M")  # 服务这次是什么时候起的


def build_line() -> str:
    """一行版本身份：`v0.2.0 · 提交 fa8fca8 · 2026-10-12 17:21 启动`。

    日志文件头、启动播报、界面都用它 —— 一句话回答"这是哪一版、跑的是不是最新的"。
    """
    parts = [f"v{VERSION}"]
    if BUILD:
        parts.append(f"提交 {BUILD}")
    parts.append(f"{STARTED_AT} 启动")
    return " · ".join(parts)


def state_payload() -> dict:
    """`GET /state` 的返回 —— 顺带把版本身份带上（人问"跑的是哪版"时一个请求就有答案）。"""
    return {"auto": _auto_next, "waiting": _next_waiting,
            "version": VERSION, "started": STARTED_AT, "commit": BUILD}

# ── 桌面阵容（头像/名字/组织；真接入时 model/base_url 从 config 覆盖）──
# ⚠️ 这张表是 `who → 名字/组织` 的**显示映射**（日志、界面气泡都问它）。
#    config 里加人时这里也得加一条 —— 漏了不会报错，只会**悄悄回落成小写的 who、
#    组织空着**：2026-10-12 的日志里 gemini 那条就写着 `**16:35 gemini**` 而不是
#    `Gemini`（它一直没在这张表里），排查身份问题时白绕了一圈。
# ⭐ 2026-10-18 改：**显示名以 config 为准**（阿包把名字做成了昵称：小千大小姐 / 基米奈 / 克劳德 / 大白龙，
#    下面那行从公司名换成模型本名）。以前这张表硬编码在这儿，config 加人就得改两处；
#    现在 `main()` 启动时用 `_guests_display()` 从 config 重算，读不到才用下面这份兜底。
_GUESTS_FALLBACK = [
    ("ayu",    "阿鱼",       "DeepSeek"),
    ("grok",   "Grok",       "xAI"),
    ("qwen",   "小千大小姐",  "Qwen"),
    ("glm",    "智谱",       "GLM"),
    ("sf",     "小流",       "硅基流动"),
    ("gemini", "基米奈",      "Gemini"),
    ("claude", "克劳德",      "Claude"),
    ("gpt",    "大白龙",      "GPT"),
]
# 兼容旧代码（模块导入/自测时先拿到这份；main() 会用 config 覆盖）
GUESTS = [tuple(r) for r in _GUESTS_FALLBACK]

_clients = []          # 每个 SSE 连接的 queue
_lock = threading.Lock()
_history = []          # 全部消息（也用于落盘）
_boss_inbox = []       # 阿包插的话，等下一轮统一喂给嘉宾
_round_no = 0
_seq = 0               # ⭐ 消息序号：SSE 断线重连会补发全部历史，前端靠它去重
_log_path = None       # ⭐ 由 main() 决定：**每场讨论一个文件**（以前写死 chat-log.md，
#                        而且启动时 write_text 覆盖 —— 重启一次就把上一场的记录冲掉了，
#                        2026-10-10 那场"记忆清空"的完整记录就是这么丢的 😢）


def init_log(topic: str) -> Path:
    """开一场新的日志文件（logs/日期-时间-议题.md）。返回路径，同时设好全局 _log_path。

    ⚠️ **讨论进行中拒绝切换日志文件**（2026-10-12 凌晨加的双保险）：
       `broadcast()` 是往全局 `_log_path` 里写的，讨论期间一旦被切走，
       本场剩下的发言就会落到另一场的文件里（"串场"，真发生过）。
       正常情况下 `start_requested()` 已经把这条路堵住了，这里再兜一层。
    """
    global _log_path
    if _debating and _log_path is not None:
        print(f"⚠️ 讨论进行中，拒绝切换日志文件（本场仍是 {_log_path.name}）")
        return _log_path
    d = ROOT / "logs"
    d.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", (topic or "debate")).strip("-")[:26] or "debate"
    base = d / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
    _log_path = base.with_suffix(".md")
    i = 2
    while _log_path.exists():          # ⭐ 绝不允许覆盖：同名就加 -2、-3…
        _log_path = base.with_name(f"{base.name}-{i}").with_suffix(".md")
        i += 1
    _log_path.write_text(
        f"# 讨论记录\n\n- 开始：{datetime.now():%Y-%m-%d %H:%M}\n- 议题：{topic}\n"
        f"- 版本：{build_line()}\n\n",     # ⭐ 记下是哪一版跑的 —— 以后回看旧记录能对上当时的行为
        encoding="utf-8")
    return _log_path


def now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


# ── 配置与 key ────────────────────────────────────────────────
def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 读不了 {path.name}: {e}")
        return {}


def _guests_display() -> list:
    """⭐ who → (显示名, 组织/模型本名)，**以 config 为准**（2026-10-18 改）。

    以前这张表硬编码在模块顶部，config 里加人就得改两处（2026-10-12 踩过：gemini 漏了，
    日志里显示成小写 who）。现在优先读 config 的 `guests[].name/org`，读不到再用内置兜底。
    """
    try:
        cfg = load_json(ROOT / "config.local.json") or load_json(ROOT / "config.example.json") or {}
        rows = []
        for g in cfg.get("guests", []) or []:
            who = str(g.get("who") or "").strip()
            if not who:
                continue
            rows.append((who, str(g.get("name") or who).strip(), str(g.get("org") or "").strip()))
        if rows:
            return rows
    except Exception as e:
        print(f"⚠️ 显示名读 config 失败，用内置兜底：{e}")
    return [tuple(r) for r in _GUESTS_FALLBACK]


def dsh_credentials() -> dict:
    """读 DSH 自己的凭据文件（`~/.dsh/.credentials.yaml`）—— 插件也是这么读的。
    ⚠️ 只解析需要的键；**绝不打印内容**（key 不进日志、更不进对话）。
    """
    path = Path.home() / ".dsh" / ".credentials.yaml"
    out = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def load_roster() -> list:
    """返回可上场的嘉宾列表：config 里的 guests × 各自的 key。没 key 的跳过。

    key 查找顺序：keys.local.json → 同名环境变量 → **DSH 的 .credentials.yaml**
    （⭐ 第三层是为了"DeepSeek 已经在 DSH 里配好了，不必再抄一份"）
    """
    cfg = load_json(ROOT / "config.local.json") or load_json(ROOT / "config.example.json")
    keys = load_json(ROOT / "keys.local.json")
    creds = dsh_credentials()
    out = []
    for g in cfg.get("guests", []):
        who = g.get("who")
        key = ""
        for kn in g.get("key_names", []):
            key = (keys.get(kn) or os.environ.get(kn) or creds.get(kn) or "").strip()
            if key:
                break
        item = dict(g)
        item["key"] = key
        item["ready"] = bool(key)
        item["resting"] = is_resting(who)      # ⭐ 界面要显示"谁在休息"
        # ⭐ 2026-10-18：模型也带上 —— `model` 是当前用的（界面显示），`models` 是上次探测到的可选清单
        item["model"] = model_for(item)
        item["models"] = (_model_catalog.get(who) or {}).get("models") or []
        item["vision"] = bool(g.get("vision", True))   # ⭐ 能不能看图（千问显式关掉）
        # ⭐ config 里显式写了 api 的，记进 URL 表 —— llm_call() 照它选协议（见 _api_style）
        if (g.get("api") or "").strip():
            _URL_API[(g.get("base_url") or "").rstrip("/")] = _api_style(
                g.get("base_url"), g.get("api"))
        out.append(item)
    return out


# ── ⭐ 每位嘉宾的「思考用量」（阿包 2026-10-11 加的：参会者一览里拖滑块）────
#    值 = 该嘉宾单次发言的 max_tokens；**0 = 不限制**（压根不传这个参数，让模型自己决定）；
#    不在表里 = 用 config 里的全局默认（debate.max_tokens）。
#    ⚠️ 对推理模型（GPT-5 / Gemini）来说，"思考 token"和正文**共用**这一份额度 ——
#       所以这个数就是"它能思考多久 + 能说多少"的总预算，不是单纯的输出上限。
THINK_MIN, THINK_MAX = 1000, 16000
_think_budget = {}


def _think_path() -> Path:
    return ROOT / "think-budget.json"


def load_think_budget() -> dict:
    """读 `think-budget.json`（不存在 / 内容坏了都当空表，绝不抛）。"""
    global _think_budget
    _think_budget = {}
    try:
        d = json.loads(_think_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _think_budget = {str(k): int(v) for k, v in d.items()
                             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    except Exception:
        pass
    return _think_budget


def save_think_budget() -> bool:
    try:
        _think_path().write_text(
            json.dumps(_think_budget, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def set_think_budget(who: str, tokens) -> bool:
    """设一位嘉宾的预算。tokens=0 → 不限制；None → 删掉这条（回落到 config 默认）。"""
    if not who:
        return False
    if tokens is None:
        _think_budget.pop(who, None)
    else:
        t = int(tokens)
        _think_budget[who] = 0 if t <= 0 else max(THINK_MIN, min(THINK_MAX, t))
    return save_think_budget()


def think_budget_for(who: str, default):
    """算这位嘉宾这次发言的 max_tokens → int，或 **None = 不限制**（llm_call 收到 None 就不传）。"""
    v = _think_budget.get(who)
    if v == 0:
        return None                      # ⭐ 不限制
    return int(v) if v else default


load_think_budget()                      # 启动就读一次


# ── ⭐ 模型选单（2026-10-18 阿包要的）──────────────────────────────────────
#    两个触发点，都要有：
#      ① **启动时**后台把全员查一遍（新出的模型、被弃用的模型一眼看到；不阻塞启动）
#      ② 「参会者一览」里点「🔄 更新模型选单」，只查**当前选中的这一位**
#         （新模型刚发布、或某家改了接口时，不用重启整个服务）
#    目录（这位有哪些可用）和当前选择（用哪个）都存 `model-catalog.json` —— 运行时数据，不进 git。
PROBE_TIMEOUT = 8
# 明显不是"聊天模型"的关键词（各家 /models 会把 tts / 语音 / 向量 / 画图全列出来，噪音太大）
_NON_CHAT_HINTS = ("tts", "audio", "realtime", "transcribe", "whisper", "embed",
                   "dall-e", "moderation", "sora", "image", "rerank", "guard", "computer-use")

_model_catalog = {}      # who -> {"models": [...], "at": "…", "error": ""}
_model_override = {}     # who -> 现在选的模型（界面里选过才有）


def _model_path() -> Path:
    return ROOT / "model-catalog.json"


def load_model_files() -> None:
    global _model_catalog, _model_override
    _model_catalog, _model_override = {}, {}
    try:
        d = json.loads(_model_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _model_catalog = d.get("catalog") or {}
            _model_override = d.get("current") or {}
    except Exception:
        pass


def save_model_files() -> bool:
    try:
        _model_path().write_text(json.dumps(
            {"catalog": _model_catalog, "current": _model_override},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def model_for(guest: dict) -> str:
    """这位现在用哪个模型：界面里选过的优先，否则用 config 里的。

    ⚠️ 所有调用处都该走它，别直接读 `guest["model"]` —— 否则在界面上换了模型也不生效。
    """
    return _model_override.get(guest.get("who") or "") or (guest.get("model") or "")


def _clean_model_ids(raw_ids, keep: str = "") -> list:
    """过滤掉明显不是对话模型的（tts / 语音 / 向量 / 画图…）；当前在用的那个永远留着。"""
    out = []
    for i in raw_ids:
        s = str(i or "").strip()
        if not s:
            continue
        if s.startswith("models/"):          # Gemini 返回 `models/gemini-…`，调用时要用裸名
            s = s[len("models/"):]
        low = s.lower()
        if any(h in low for h in _NON_CHAT_HINTS):
            continue
        if s not in out:
            out.append(s)
    if keep and keep not in out:
        out.insert(0, keep)
    return out


def probe_models(who: str) -> dict:
    """去问这一家的 `/models`，更新目录。**绝不抛异常**（失败只记 error，界面照样能用）。"""
    g = next((x for x in load_roster() if x.get("who") == who), None)
    if not g:
        return {"ok": False, "error": "名单里没有这一位"}
    if not g.get("ready"):
        return {"ok": False, "error": "这位没有 key，查不了"}
    base = (g.get("base_url") or "").rstrip("/")
    cur = model_for(g)
    hdrs = {"Authorization": "Bearer " + g["key"]}
    try:
        if _api_style(base) == "anthropic":
            hdrs = {"x-api-key": g["key"], "anthropic-version": ANTHROPIC_VERSION}
        req = urllib.request.Request(base + "/models", headers=hdrs)
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        rows = data.get("data") or data.get("models") or []
        ids = [str(m.get("id") or m.get("name") or "") for m in rows if isinstance(m, dict)]
        models = _clean_model_ids(ids)          # ⚠️ 不把当前模型硬塞进去 —— 让界面能提示"可能已弃用"
        if not models:
            raise RuntimeError("返回里没有可用的对话模型")
        _model_catalog[who] = {"models": models, "at": time.strftime("%Y-%m-%d %H:%M"), "error": ""}
        save_model_files()
        return {"ok": True, "models": models, "at": _model_catalog[who]["at"]}
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        slot = _model_catalog.setdefault(who, {"models": [], "at": "", "error": ""})
        slot["error"] = msg                       # ⚠️ 保留旧的 models，别因为一次失败就清空
        slot.setdefault("models", [])
        save_model_files()
        return {"ok": False, "error": msg}


def probe_all_models(delay: float = 3.0) -> None:
    """启动时后台跑：把全员查一遍。失败静默（各家偶尔 403/超时，不值得打扰）。"""
    time.sleep(delay)                 # 先让服务起来、界面能连上
    for g in load_roster():
        if not g.get("ready"):
            continue
        try:
            probe_models(g["who"])
        except Exception:
            pass


load_model_files()                       # 启动先读上次的目录（没网也能显示上次结果）


# ── ⭐ 主持人（2026-10-18 阿包要的）────────────────────────────────────────
#    「主持人」= 开场白 + 每轮纪要 + 会议总览（原本这三件都写死在阿鱼身上）。
#    现在可以在「参会者一览」里换人 / 取消；设成空 = 本场**没有主持人**
#    → 没有开场白、没有纪要、也没有总结，开场前界面会先拦一次。
#    状态存 `host.json`（运行时数据，不进 git）；**默认就是阿鱼**（跟以前一样）。
_host_who = "ayu"


def _host_path() -> Path:
    return ROOT / "host.json"


def load_host() -> str:
    """读主持人设置。文件不存在 → 保持默认（阿鱼）。空字符串 = 明确「本场没有主持人」。"""
    global _host_who
    try:
        d = json.loads(_host_path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and "who" in d:
            _host_who = str(d.get("who") or "")
    except Exception:
        pass
    return _host_who


def save_host(who: str) -> bool:
    try:
        _host_path().write_text(json.dumps({"who": who}, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        return True
    except Exception:
        return False


def host_name() -> str:
    """主持人显示名（没有主持人时给空串）。"""
    if not _host_who:
        return ""
    return next((n for w, n, _o in GUESTS if w == _host_who), _host_who)


load_host()


# ── ⭐ 在界面上填 API Key（2026-10-18 阿包要的）────────────────────────────
#    以前要自己开 keys.local.json 手填，很多"想试一下"的人就卡在这一步。
#    🔴 **安全边界（一个都不能破）**：
#      · 界面**只回**「配没配 + 末四位」，**绝不把 key 明文发回浏览器**；
#      · 日志/系统消息里也只写末四位，不写全文；
#      · 写进 `keys.local.json`（本机文件，.gitignore 已排除）。
def _keys_path() -> Path:
    return ROOT / "keys.local.json"


def load_keys() -> dict:
    d = load_json(_keys_path())
    return d if isinstance(d, dict) else {}


def save_key(who: str, key: str) -> bool:
    """写入 / 清除一位的 key。保留文件里 `_说明` 那些注释键。"""
    if not who:
        return False
    d = load_keys()
    if key:
        d[who] = key
    else:
        d.pop(who, None)
    try:
        _keys_path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def keys_status() -> list:
    """给界面看的 key 状态：**不含明文**，只有「配没配」+ 末四位。"""
    rows = []
    for g in load_roster():
        k = str(g.get("key") or "")
        rows.append({
            "who": g.get("who") or "",
            "name": g.get("name") or g.get("who") or "",
            "org": g.get("org") or "",
            "set": bool(k),
            "tail": (k[-4:] if len(k) >= 8 else ""),
        })
    return rows


# ── ⭐ 让某位嘉宾「休息」：这一场她不参与（阿包 2026-10-12 凌晨要的）──────
#    原话：「在设定每轮的思考量那里，假如某个 ai 我先让她休息的话，增加一个按钮，
#            点了这次讨论她就不参与」。
#    ⚠️ 休息 ≠ 掉线：key 还在、随时能叫回来，只是**不上场**（不发言、也不算进"在场几位"）。
#       和"没配 key"的区别只是——一个是被请去休息，一个是压根没来。
#    状态存 resting.json（运行时数据，不进 git），点了就一直是休息，再点一下请回来。
_resting = {}


def _rest_path() -> Path:
    return ROOT / "resting.json"


def load_resting() -> dict:
    """读 `resting.json`（谁在休息）。不存在 / 内容坏了都当「没人休息」，绝不抛。"""
    global _resting
    _resting = {}
    try:
        d = json.loads(_rest_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _resting = {str(k): True for k, v in d.items() if v}
    except Exception:
        pass
    return _resting


def save_resting() -> bool:
    try:
        _rest_path().write_text(json.dumps(_resting, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        return True
    except Exception:
        return False


def set_resting(who: str, rest: bool) -> bool:
    """让她休息 / 请她回来。"""
    if not who:
        return False
    if rest:
        _resting[who] = True
    else:
        _resting.pop(who, None)
    return save_resting()


def is_resting(who: str) -> bool:
    """⭐ 各处都**实时**问这个函数，而不是看 roster 快照里的字段 ——
    休息开关随时可能被改，而 roster 是服务启动时读的（`idle_loop` 待机时尤其明显）。"""
    return bool(_resting.get(who))


load_resting()                           # 启动就读一次


# ── ⭐ 阿包发的图片（2026-10-12 凌晨加：旁听席可以发图了）──────────────
#    流程：前端压缩成 JPEG 的 data URL → POST /say → 存成 shots/xxx.jpg →
#    广播里带 /shots/xxx.jpg（SSE 消息不带 base64，省流量）→ 嘉宾发言时再读回来转 data URL。
#    ⚠️ 图片**只进带它的那一轮**（后面靠纪要带）—— 每张图 ≈ 1024 token/位，轮轮都带太贵。
SHOT_MAX_BYTES = 6 * 1024 * 1024         # 单张图（解出来的原始字节）上限
SHOT_EXTS = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "gif": "gif", "webp": "webp"}


def save_shot(data_url: str) -> str:
    """把一张 data URL 图存进 `shots/`，返回可访问的 `/shots/xxx`；失败返回 ""。"""
    m = re.match(r"^data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+)$",
                 (data_url or "").strip(), re.S)
    if not m:
        return ""
    ext = SHOT_EXTS.get(m.group(1).lower(), "jpg")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=False)
    except Exception:
        return ""
    if not raw or len(raw) > SHOT_MAX_BYTES:
        return ""
    d = ROOT / "shots"
    d.mkdir(exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{hashlib.md5(raw).hexdigest()[:8]}.{ext}"
    try:
        (d / name).write_bytes(raw)
    except Exception:
        return ""
    return f"/shots/{name}"


def image_data_url(shot_url: str) -> str:
    """把 `/shots/xxx` 读回来变成 data URL（喂给模型用）。读不到返回 ""。"""
    if not shot_url or not shot_url.startswith("/shots/"):
        return ""
    name = shot_url[len("/shots/"):]
    if not name or "/" in name or "\\" in name or ".." in name:
        return ""
    p = ROOT / "shots" / name
    if not p.exists() or not p.is_file():
        return ""
    try:
        raw = p.read_bytes()
    except Exception:
        return ""
    mime = mimetypes.guess_type(name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _inbox_item(x) -> dict:
    """把旁听席队列里的条目统一成 `{"text":…, "image":…}`。

    ⚠️ 兼容老的"纯字符串"写法：字符串进了 `.get()` 会直接抛 AttributeError，
       而那是**在待机线程里**抛的 —— 线程一死就变成"阿包说话没人理"（第 16 条坑的老剧本）。
       （2026-10-12 加图片时把队列从字符串换成字典，验证脚本当场就把这个坑踩出来了。）
    """
    if isinstance(x, dict):
        return {"text": str(x.get("text") or ""), "image": str(x.get("image") or "")}
    return {"text": str(x or ""), "image": ""}


def guest_vision(guest: dict) -> bool:
    """这位嘉宾能不能看图？config 里的 `vision` 说了算，没写就**默认能**（实测 5 位里 4 位能）。

    ⚠️ 唯一一位看不到的是千问（`qwen-plus` 是纯文本）—— 它的 config 里显式写了 `vision: false`。
       实测脚本：`_probe/teatime-check-vision.py`（画张红圆蓝方，看它认不认得出来）。
    """
    return bool((guest or {}).get("vision", True))


_chosen_topic_index = 0          # ⭐ 阿包在界面上选的议题（0 = 第一个）
_rounds_override = None          # ⭐ 阿包在议题框里填的「预设轮数」—— 有值就盖过 config 的 rounds


_SEP_TAIL = re.compile(r"(?:\s*\n)*\s*-{3,}\s*$")


def strip_tail_seps(text: str) -> str:
    """⭐ 剥掉尾部累积的 `---` 装饰行（一直剥到不再变化为止）。

    为什么需要它：议题之间那条 `---` 是 write_topics() **生成给人看的装饰**，
    而解析只认 `## ` 分段 —— 于是它会被算进**上一个议题正文的尾巴**、
    也会被算进**文件头部说明的尾巴**；下次保存再生成一条，就又多留一条。
    → 每保存一次（排序 / 删除 / 新增）+1 条，文件无限膨胀。
    阿包 2026-10-11 发现的：`topics.md` 里攒了 63 个 `---`，而议题只有 6 个。

    ⚠️ 只剥**尾部**的，正文中间的 `---` 是阿包自己写的，不动。
    """
    prev = None
    while prev != text:
        prev = text
        text = _SEP_TAIL.sub("", text).rstrip()
    return text


# ── ⭐ 议题库分两份（阿包 2026-10-11 定的）────────────────────────────
#   topics.default.md —— 工程自带的默认议题，**只读**，跟着代码进 git
#   topics.md         —— 你实际在用的那份，界面的增删改排序都改它，**不进 git**
#   为什么分开：议题库是**会被程序在运行时改写**的数据。放进仓库的话，
#   你每调一次顺序 / 删一个议题，`git status` 就脏一次 —— 阿包说"在我本地怪怪的"，就是这个。
def topics_path() -> Path:
    """读的时候用哪份：你自己的库优先；没有就先用工程自带的默认库顶着。"""
    mine = ROOT / "topics.md"
    if mine.exists():
        return mine
    dft = ROOT / "topics.default.md"
    return dft if dft.exists() else mine


def ensure_topics() -> None:
    """第一次运行（还没有 `topics.md`）→ 从工程自带的默认库复制一份出来。

    ⚠️ 顺手把文件头的说明换成"这是你自己的库" —— 否则复制过来的说明自相矛盾
    （默认库那份写着"别改这个文件"）。
    """
    mine = ROOT / "topics.md"
    dft = ROOT / "topics.default.md"
    if mine.exists() or not dft.exists():
        return
    try:
        txt = dft.read_text(encoding="utf-8")
        head, sep, rest = txt.partition("\n## ")
        new_head = (
            "# AI 讨论会 · 议题库\n\n"
            "> ⭐ **这是你自己的议题库** —— 界面上的增删、排序都改它；直接编辑也行，重启服务即生效。\n"
            "> ⚠️ 它**不进 git**（在 .gitignore 里）—— 想改「新装时的默认议题」，去改 `topics.default.md`。\n"
            "> 格式：`## 议题标题` 一行，下面写正文（可多行）；**标题行必须以 `## ` 开头**，程序靠它分段。\n"
            "> 原始征集稿（含各位 AI 的完整提议原文）在 `桌面\\agent们\\茶话会议题征集.txt`。"
        )
        mine.write_text(new_head + sep + rest if sep else new_head + "\n", encoding="utf-8")
    except Exception:
        pass


def parse_topics() -> list:
    """从议题库读议题（你自己的 `topics.md`；没有就用工程自带的 `topics.default.md`）。

    格式：一行 `## 标题`，下面跟正文（可多行，正文里含"由谁提供"）。
    ⚠️ 这文件是阿包手写 + 界面改写的，解析必须宽松：认不出的内容忽略，绝不抛异常。
    """
    path = topics_path()
    if not path.exists():
        return []
    out, cur = [], None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("## "):
            if cur:
                out.append(cur)
            cur = {"title": s[3:].strip(), "body": ""}
        elif cur is not None:
            cur["body"] += line + "\n"
    if cur:
        out.append(cur)
    for t in out:
        t["body"] = strip_tail_seps(t["body"].strip())      # ⭐ 剥掉尾巴上的 `---`（见上方说明）
        t["author"] = parse_author(t["body"])
        t["edited"] = parse_edited(t["body"])               # ⭐ 被人工改过的话，正文里有留痕
        t["reason"] = strip_meta_lines(t["body"])           # ⭐ 纯理由 —— 「✏️ 编辑」弹窗预填用
    return out


def parse_author(body: str) -> str:
    """⭐ 从议题正文里认出「由谁提供」。

    兼容两种写法（程序生成的新格式 + 阿包手写的旧格式）：
      · `> **由谁提供**：GPT`
      · `（由 GPT 提出）`
    认不出就返回空串 —— 议题一览里显示"未标注"。
    """
    for pat in (r"由谁提供\s*\**\s*[:：]\s*\**([^\n*（(]{1,24})",
                r"由\s*\**((?!谁)[^\n*（(]{1,24}?)\**\s*(?:提出|提供)"):
        m = re.search(pat, body or "")
        if m:
            who = m.group(1).strip().strip("*：: ").strip()
            if who:
                return who
    return ""


def parse_edited(body: str) -> str:
    """⭐ 认出「这条议题被人工改过」的留痕行，返回修改时间（没改过就返回空串）。

    留痕格式（`edit_topic()` 生成）：`> ✏️ 已修改：2026-10-11 19:50`
    界面靠它给这条议题标「（已修改）」。
    """
    m = re.search(r"^>[^\n]{0,4}已修改\s*[:：]\s*([^\n]*)$", body or "", re.M)
    return m.group(1).strip() if m else ""


def strip_meta_lines(body: str) -> str:
    """⭐ 把正文里的"元信息行"剥掉，只留阿包写的理由（「✏️ 编辑」弹窗预填的就是它）。

    元信息行 = 程序自己生成的那两种，不是他写的正文：
      · `> **由谁提供**：GPT`、`> ✏️ 已修改：<时间>`
      · `（由克劳德提出）` 这种独立成行的旧写法（`parse_author` 也认它）
    ⚠️ 只剥这两种固定格式 —— 正文里其它的引用行、括号内容一律留着。
       （不剥的话：编辑一次，理由框里就多一份元信息，保存后又叠一层。）
    """
    keep = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith(">") and ("由谁提供" in s or "已修改" in s):
            continue
        if re.fullmatch(r"[（(]\s*由\s*[^）)]{1,24}?\s*(?:提出|提供)\s*[）)]", s):
            continue
        keep.append(line)
    return strip_tail_seps("\n".join(keep).strip())


def add_topic(title: str, reason: str, author: str) -> bool:
    """⭐ 新增议题（议题一览的「➕ 增加议题」弹窗）。

    ⭐ 2026-10-14 改：**插到最前面**，不再追加到末尾 —— 阿包的原话：
      「每次都要自己手动调整有点麻烦了。能不能把添加话题的时候，新加的那条自动变成第一条呢？」
      开屏那个议题框默认选中**第一条**（`_chosen_topic_index` 默认 0），
      所以插到最前 = 下次开局直接就是刚加的这条，不用再拖上去。
      同名的直接拒绝，免得议题一览里出现两个一样的。
    """
    title = (title or "").strip()
    if not title:
        return False
    items = parse_topics()
    if any(t["title"].strip() == title for t in items):
        return False
    parts = []
    if (author or "").strip():
        parts.append(f"> **由谁提供**：{author.strip()}")
    if (reason or "").strip():
        parts.append(reason.strip())
    items.insert(0, {"title": title, "body": "\n\n".join(parts)})
    return write_topics(items)


# ── 讨论历史（⭐ 议题库要显示"讨论过几轮、谁参与的"）──────────
_history_path = ROOT / "debate-history.json"


def save_topics(order: list) -> bool:
    """⭐ 按界面上拖出来的顺序**重写 `topics.md`**（保留每个议题的正文）。

    order 必须是 0..n-1 的一个完整排列（旧下标按新位置排列），否则拒绝。
    ⚠️ 会保留文件开头到第一个 `## ` 之前的部分（那是阿包自己写的说明），别把它冲掉。
    """
    items = parse_topics()
    if not items:
        return False
    try:
        idx = [int(i) for i in order]
    except Exception:
        return False
    if sorted(idx) != list(range(len(items))):
        return False
    return write_topics([items[i] for i in idx])


def write_topics(items: list) -> bool:
    """⭐ 把给定的议题列表（按顺序）写回 `topics.md`。

    排序 / 删除 / 新增都走这里 —— 只认 title + body，其余格式统一生成。
    ⚠️ 保留文件开头到第一个 `## ` 之前的部分（那是阿包自己写的说明），别冲掉。
    """
    path = ROOT / "topics.md"          # ⭐ 永远写**你自己的**那份（默认库只读，绝不改它）
    src = topics_path()                # 但开头那段说明，从"当前在读的那份"里取
    raw = src.read_text(encoding="utf-8", errors="replace") if src.exists() else ""
    # ⭐ 文件头也要剥掉尾巴上的 `---`：write_topics 生成的第一条 `---` 会被切进 head，
    #    不剥的话每保存一次就多攒一条 —— 和 parse_topics 那边是同一个 bug 的两半。
    head = strip_tail_seps(raw.split("\n## ", 1)[0].rstrip()) or "# AI 讨论会 · 议题库"
    lines = [head, ""]
    for t in items:
        lines += ["", "---", "", f"## {t['title']}", ""]
        if t.get("body"):
            lines += [t["body"], ""]
    try:
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except Exception:
        return False
    return True


def delete_topic(index: int, expect: str = "") -> bool:
    """⭐ 删掉一个议题（议题一览里的「删除」按钮）。

    `expect` = 界面上那条议题的题目，用来**挡住"下标漂移"**：
    议题一览是按住拖动排序的，拖动只改了顺序、界面手里那个下标却可能还是旧的，
    拿旧下标来删就会**删掉别人那条**（和 `edit_topic` 是同一个坑，2026-10-12）。
    """
    items = parse_topics()
    if not items or not (0 <= int(index) < len(items)):
        return False
    if expect and items[int(index)]["title"].strip() != expect.strip():
        return False          # 下标指到别条去了 —— 宁可不删
    return write_topics([t for i, t in enumerate(items) if i != int(index)])


def edit_topic(index, title: str, reason: str, author: str, expect: str = ""):
    """⭐ 编辑一条议题（议题整理里的「✏️ 编辑」按钮）。

    三个字段都能改：题目 / 理由正文 / 由谁提供。
    改过的议题会在正文里留一行 `> ✏️ 已修改：<时间>` —— 界面据此标「（已修改）」，
    也让"这条被动过"在 `topics.md` 里看得见（阿包 2026-10-11 晚要的）。

    ⚠️ 改了题目的话，把 `debate-history.json` 里的旧题目一并改过来 ——
       战绩是按题目名匹配的，不改的话「已讨论 N 个轮次 / 参与者」当场断链。

    ⭐ `expect` = 界面上那条议题的**当前题目**（前端原样回传）。
       它是"下标漂移"的保险丝（阿包 2026-10-12 撞的那个 bug）：
       议题一览支持拖动排序，而拖动只改顺序、不改界面手里的下标 ——
       排完序再点编辑，旧下标就指到**别人那条**上去了。结果分两种：
         · 题目没改 → 撞上判重，弹「已经有同名的议题了」（他报的就是这个）；
         · 题目改了 → **静默改错条目**，把别人那条的题目和理由覆盖掉（更坏）。
       所以：对不上就直接拒绝，让他重开一下议题整理 —— 宁可不动，也不能改错人。

    返回 `(ok, 说明)`：说明文字留着给界面显示失败原因 / 迁移了几场。
    """
    items = parse_topics()
    try:
        i = int(index)
    except (TypeError, ValueError):
        return False, "议题下标不对"
    if not (0 <= i < len(items)):
        return False, "找不到这个议题（列表可能刚变过，重开一下）"
    new_title = (title or "").strip()
    if not new_title:
        return False, "议题题目不能空着"
    if expect and items[i]["title"].strip() != expect.strip():
        return False, "列表顺序刚变过（多半是你刚拖动过）—— 关掉议题整理、重开一次再来改"
    if any(j != i and t["title"].strip() == new_title for j, t in enumerate(items)):
        return False, "已经有同名的议题了"

    old_title = items[i]["title"]
    parts = []
    if (author or "").strip():
        parts.append(f"> **由谁提供**：{author.strip()}")
    parts.append(f"> ✏️ 已修改：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if (reason or "").strip():
        parts.append(reason.strip())
    items[i] = {"title": new_title, "body": "\n\n".join(parts)}   # ⭐ 整条重建，元信息不会叠层
    if not write_topics(items):
        return False, "写回 topics.md 失败"

    moved = rename_history_topic(old_title, new_title) if old_title != new_title else 0
    return True, (f"（题目也改了：「{old_title}」的 {moved} 场战绩已跟着改过来）" if moved else "")


def rename_history_topic(old: str, new: str) -> int:
    """⭐ 议题改了题目：把往期战绩里的旧题目一起改过来（否则战绩断链）。返回改了几场。"""
    h = load_history()
    n = 0
    for d in h.get("debates", []):
        if (d.get("topic") or "").strip() == (old or "").strip():
            d["topic"] = new
            n += 1
    if n:
        save_history(h)
    return n


def reset_topic(title: str) -> int:
    """⭐ 清掉某个议题的讨论战绩（议题一览里的「重置」按钮）——清完它就能重新讨论。

    只动 `debate-history.json`，不碰 `topics.md` 和已落盘的日志文件。
    返回清掉的场次数。
    """
    want = (title or "").strip()
    if not want:
        return 0
    h = load_history()
    before = len(h["debates"])
    h["debates"] = [d for d in h["debates"] if (d.get("topic") or "").strip() != want]
    save_history(h)
    return before - len(h["debates"])


def parse_candidate_topics(text: str) -> list:
    """⭐ 从 AI 的回答里抠出「议题备选」：返回 [{title, reason}]。

    认这种成对的行（`1. 题目` 下面跟 `理由：…`）：
        1. 议题内容
        理由：为什么值得讨论
        2. 议题内容
        理由：……
    ⚠️ 线上模型不一定听话：没写"理由："就把紧跟着的那行当理由；认不出就返回空列表。
    """
    out, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\s*(?:\d+\s*[.、)）]|[-*·])\s*(.{4,})$", line)
        if m:
            if cur:
                out.append(cur)
            cur = {"title": m.group(1).strip().strip("*#「」【】 ").strip(), "reason": ""}
            continue
        if cur is not None:
            r = re.match(r"^\s*\**\s*(?:理由|原因|为什么|说明|看点)\s*\**\s*[:：]\s*(.+)$", line)
            if r:
                cur["reason"] = r.group(1).strip().strip("* ")
            elif not cur["reason"] and len(line) > 4 and not line.startswith("#"):
                cur["reason"] = line.lstrip("-*· ").strip()      # 没写"理由："就当下行是理由
    if cur:
        out.append(cur)
    seen, uniq = set(), []
    for c in out:
        if c["title"] and c["title"] not in seen:
            seen.add(c["title"])
            uniq.append(c)
    return uniq[:6]


def ask_guest_for_topics(who: str) -> dict:
    """⭐ 现场提问：让某位**在座**的 AI 提几个新的讨论议题备选。

    同步调用（前端转圈等），返回 {ok, who, name, text, topics[]}。
    """
    g = next((x for x in load_roster() if x.get("who") == who and x.get("ready")), None)
    if g is None:
        return {"ok": False, "err": "这位不在场（没配 key）"}
    s = load_settings()
    cur = s.get("topic") or "（还没定）"
    past = [d.get("topic", "") for d in load_history().get("debates", [])]
    try:
        text = llm_call(g["base_url"], model_for(g), g["key"], [
            {"role": "system", "content": (g.get("persona") or f"你是{g['name']}。") +
             "\n\n现在阿包（在场的人类）想请你提几个**新的讨论议题**。"
             "不要客套话、不要解释、不要总结，只给议题本身。"},
            {"role": "user", "content":
                f"【现在正在讨论的议题】\n{cur}\n\n"
                f"【已经讨论过的（别重复）】\n" + ("\n".join(f"- {p}" for p in past if p) or "（还没有）") +
                "\n\n请提 **1~3 个新的讨论议题**（可以是当前议题的延伸，也可以是完全不同的话题）。要求：\n"
                "① 每个议题一句话说清，要有「可争」的空间，不能是一句话就能答完的问题；\n"
                "② 每个议题**必须再跟一句「理由：」，说清为什么值得讨论**（一两句话，别空着）；\n"
                "③ **严格**按下面的格式，每条两行，除了这些行什么都不要写：\n\n"
                "1. 议题内容\n理由：为什么值得讨论\n2. 议题内容\n理由：为什么值得讨论\n"},
        ], max_tokens=1500)
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:120]}"}
    if not (text or "").strip():
        return {"ok": False, "err": "它没能说出话来（可能输出额度不够）"}
    return {"ok": True, "who": who, "name": g["name"],
            "text": text.strip(), "topics": parse_candidate_topics(text)}


def load_history() -> dict:
    """每场讨论的战绩。结构：
       {"debates": [{topic, topic_index, rounds_done, rounds_planned,
                     participants: [名字], started, finished}]}
    """
    if not _history_path.exists():
        return {"debates": []}
    try:
        h = json.loads(_history_path.read_text(encoding="utf-8"))
        if isinstance(h, dict) and isinstance(h.get("debates"), list):
            return h
    except Exception:
        pass
    return {"debates": []}


def save_history(h: dict) -> None:
    try:
        _history_path.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── ⭐ 「继续讨论」：把上一次的记录翻出来（阿包 2026-10-11 晚要的）──────
#   原话：「需要把之前的记录翻出来呢，要不然就不是继续聊，而是重新聊了」
RECAP_MAX = 6000        # 前情提要最多带这么多字（省 token，别把上下文撑爆）
RECAP_ROUNDS_MAX = 2000  # ⭐ 其中「逐轮整理」的额度（2026-10-12 定：两份都带）
RECAP_SUMMARY_MAX = 4000 # ⭐ 其中「会议总览」的额度
SPEECH_KEEP = 900       # ⭐ 续场时，各人"上次自己说过的话"每段最多带这么多字（2026-10-12 加）


def log_safe_topic(topic: str) -> str:
    """议题名在日志文件名里的样子 —— 跟 `init_log()` 用**同一套**规范化。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "-", (topic or "debate")).strip("-")[:26] or "debate"


def find_debates(topic: str, exclude=None) -> list:
    """列出某个议题的**所有**日志文件，**新的在前**。

    ⚠️ 不能靠日志里那行"议题：xxx"来匹配 —— `list_past_debates()` 反推出来的议题名
       被换过空格、还截断过，比不准。这里直接比**文件名里的规范名**：
       两边都用 `log_safe_topic()` 算，名字对得上就是对得上。

    ⚠️ `exclude` 要传**本场刚开的那个日志**（`_log_path`）—— 这个坑真踩到了：
       开场流程是先 `init_log()` 建本场文件、再回头翻"上一次"，而新建的文件名时间戳最新，
       于是"上一次"永远翻到自己那个空壳，`build_recap()` 恒返回空（本地验证脚本抓出来的）。
    """
    d = ROOT / "logs"
    if not d.exists():
        return []
    ex = Path(exclude).resolve() if exclude else None
    want = log_safe_topic(topic)
    hits = []
    for p in d.glob("*.md"):
        if ex is not None and p.resolve() == ex:
            continue
        m = re.match(r"^(\d{8}-\d{6})-(.+)$", p.stem)
        if not m:
            continue
        # 剥掉 init_log() 的 -2 / -3 重名后缀再比
        if re.sub(r"-\d+$", "", m.group(2)) == want:
            hits.append((m.group(1), p))
    return [p for _, p in sorted(hits, key=lambda x: x[0], reverse=True)]


def find_last_debate(topic: str, exclude=None):
    """某个议题**最近一场**的日志（找不到返回 None）—— 就是 `find_debates()` 的第一个。"""
    hits = find_debates(topic, exclude=exclude)
    return hits[0] if hits else None


def _cut_after_debate(txt: str) -> str:
    """切掉「散场之后」的部分：待机闲聊 + 末尾那张《会议纪要（阿鱼整理）》大卡片。

    两个用处：① 从上次日志里抽"各人**自己**说过的话"（续场用）；
              ② 从日志里捡「余味」的"你最后说的话"。

    ⚠️ 两件都会翻车的事：
      · 散场后大家在待机里闲聊，那些话**顶着最后一轮的轮号**，不切就会被当成"讨论里说过的话"；
      · 会议纪要那张卡片是多行文本（`say("ayu", …, "summary")`），不切会被当成"阿鱼最后说的话"。
    """
    txt = (txt or "").split("# 📝 会议纪要（阿鱼整理）", 1)[0]
    return re.split(r"^- \*\*\d{2}:\d{2} system\*\*：\s*(?:讨论结束|🛑|💸)",
                    txt, maxsplit=1, flags=re.M)[0] or txt


def extract_speeches(txt: str) -> dict:
    """从一场讨论的日志里按人抽出"他说过的话"（带轮号）。

    干什么用：「继续讨论」时要把**各人自己**上次的发言接回上下文 ——
    阿包 2026-10-12 的原话：「我新跑了一轮上面的议题。怎么感觉大家似乎又回到了第一轮的样子，
    好像之前没讨论过。」真因就是：公用历史框里只有**阿鱼整理的纪要**（第三方口吻），
    模型不把它当"我说过的话"，于是每个人从自己的立场从头再讲一遍。
    """
    txt = _cut_after_debate(txt)
    out, state = {}, {"round": 0, "who": "", "buf": []}

    def flush() -> None:
        text = "\n".join(state["buf"]).strip()
        if state["who"] and text and not text.startswith("#"):
            out.setdefault(state["who"], []).append((state["round"], text[:SPEECH_KEEP]))
        state["who"], state["buf"] = "", []

    for line in txt.splitlines():
        m = re.match(r"^- \*\*\d{2}:\d{2} ([^*]+?)\*\*：(.*)$", line)
        if m:
            flush()
            who, text = _who_of(m.group(1)), m.group(2)
            if not who:                       # system / 阿包 的话不算
                mr = re.search(r"—— 第 (\d+) 轮 ——", text)
                if mr:
                    state["round"] = int(mr.group(1))
                continue
            state["who"], state["buf"] = who, [text]
        elif state["who"]:
            state["buf"].append(line)         # 一条发言的后续行
    flush()
    return out


def previous_speeches(path) -> dict:
    """上一次那场里**每个人最后两段**发言（续场时接到「你自己的历次发言」里）。"""
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    got = extract_speeches(txt)
    return {who: rows[-2:] for who, rows in got.items() if rows}


def latest_summary(topic: str, exclude=None):
    """⭐ 这个议题**最近一份会议总览**（《会议纪要（阿鱼整理）》），可能在更早的场。

    「余味」和续场都要用它：阿包 2026-10-12 定的是「逐轮整理 + 最后总览」**两份都喂**。
    返回 `(文本, 那场的时间)`；没有就返回 `("", "")`。
    """
    for p in find_debates(topic, exclude=exclude):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        mark = "# 📝 会议纪要（阿鱼整理）"
        if mark in txt:
            rec = txt.split(mark, 1)[1].strip()
            if rec:
                s = p.stem
                return (rec[:RECAP_SUMMARY_MAX],
                        f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}")
    return "", ""


def build_recap(topic: str, exclude=None):
    """⭐ 翻出上一次聊这个议题的**两份记录**，做成"前情提要"。
    返回 `(文本, 来源说明, 上次聊到第几轮, 那场的日志路径)` —— 文本为空 = 没翻到（这场只能当新议题聊）。

    ⭐⭐ 2026-10-12 晚（阿包拍板）：**两份都带上** —— 他的原话：
       「阿鱼不是会整理每一轮大家的发言吗？然后最后会生成一个总览。
        我想说的就是，续读和结束时的闲聊给大家喂的就是这两部分。」
    所以这里拼两段，缺哪份就只带有的那份：
      ① **逐轮整理**（阿鱼每轮写进公用历史框的小纪要）—— 取**最近一场**的
      ② **会议总览**（那份《会议纪要（阿鱼整理）》）—— 取**最近一份有的**
         （最近的场只跑了 1 轮、又没点「帮我整理」时，总览得往前翻到更早那场）
    ⚠️ 两份可能来自**不同的场次**，所以各自标上时间，别让读的人以为是一场的。

    ⚠️ 会**一场一场往前翻**：空壳场（点了开场、一轮没跑起来）自动跳过。

    第三个返回值（上次那场日志里 `—— 第 N 轮 ——` 的最大 N）用来**接着数轮号**：
       上次聊到第 2 轮 → 这一场从「第 3 轮」开始（阿包 10-11 晚：「想从第三轮开始」）。
    第四个是**逐轮整理那场**的日志文件 —— `previous_speeches()` 拿它抽各人自己的发言。
    """
    rounds_txt, rounds_from, rounds_n, rounds_path = "", "", 0, None
    summary, summary_from = "", ""
    last_round = 0
    for p in find_debates(topic, exclude=exclude):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        s = p.stem
        when = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}"
        if not rounds_txt:                       # ① 逐轮整理：只要**最近一场**的
            notes = [(n, t.strip()) for n, t in
                     re.findall(r"📝 第 (\d+) 轮纪要（写入公用历史框）：\n(.*?)(?=\n- \*\*|\Z)",
                                txt, re.S) if t.strip()]
            if notes:
                rounds_txt = "\n\n".join(f"### 第 {n} 轮\n{t}" for n, t in notes)
                rounds_from, rounds_n, rounds_path = when, len(notes), p
                seen = [int(x) for x in re.findall(r"—— 第 (\d+) 轮 ——", txt)]
                last_round = max(seen) if seen else 0
        if not summary:                          # ② 总览：往前翻到**最近一份有的**
            mark = "# 📝 会议纪要（阿鱼整理）"
            if mark in txt:
                rec = txt.split(mark, 1)[1].strip()
                if rec:
                    summary, summary_from = rec, when
        if rounds_txt and summary:
            break

    if not (rounds_txt or summary):
        return "", "", 0, None

    body = "【上一次的记录 · 阿鱼整理的两份】"
    desc = []
    if rounds_txt:
        body += (f"\n\n## ① 逐轮整理（{rounds_from} 那场，共 {rounds_n} 轮）\n"
                 f"{rounds_txt[:RECAP_ROUNDS_MAX]}")
        desc.append(f"{rounds_from} 那场的逐轮整理（{rounds_n} 轮）")
    if summary:
        body += f"\n\n## ② 会议总览（{summary_from} 那场）\n{summary[:RECAP_SUMMARY_MAX]}"
        desc.append(f"{summary_from} 那场的会议总览")
    return body[:RECAP_MAX], " ＋ ".join(desc), last_round, rounds_path


# ── ⭐ 往期讨论（阿包 2026-10-11 下午要的）──────────────────────────
#   原话：「用户只能看见讨论了多少轮，但是无法看见结果，好像意义不是特别大」
#   数据就在 logs/*.md 里（每场一个文件，纪要在末尾），这里只是给它开个门。
def list_past_debates() -> list:
    """列 `logs/` 下的往期讨论，**新的在前**。每场：时间 / 议题 / 轮数 / 谁说过话 / 有没有纪要。"""
    d = ROOT / "logs"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-(.*)\.md$", p.name)
        if m:
            ts = f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
            topic = m.group(7).replace("-", " ")
        else:
            ts, topic = "", p.stem
        speakers = []
        for who in re.findall(r"^- \*\*\d{2}:\d{2} ([^*]+?)\*\*", txt, re.M):
            who = who.strip()
            if who and who != "system" and who not in speakers:
                speakers.append(who)
        # ⚠️ 只点了个开场、一轮都没跑起来的**空壳**（0.3~0.6 KB）不列进来 ——
        #    实测 28 个日志里一大半是这种（点了开场又立刻结束），会把列表刷满
        rounds = len(re.findall(r"—— 第 \d+ 轮 ——", txt))
        has_sum = "会议纪要（阿鱼整理）" in txt
        if rounds == 0 and not has_sum:      # 空壳（一轮没跑、也没纪要）才跳过
            continue
        out.append({
            "file": p.name,
            "ts": ts,
            "topic": topic,
            "rounds": rounds,
            "speakers": speakers,
            "has_summary": has_sum,
            "kb": round(p.stat().st_size / 1024, 1),
        })
    return out


def read_past_debate(fname: str, mode: str = "summary") -> str:
    """读一场往期讨论。`mode=summary` 只给会议纪要；`full` 给全文。

    ⚠️ 只认 `logs/` 下的 `.md`，文件名里不许有路径分隔符 —— 挡路径穿越。
    """
    if not fname or "/" in fname or "\\" in fname or not fname.endswith(".md"):
        return ""
    p = ROOT / "logs" / fname
    if not p.exists() or not p.is_file():
        return ""
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if mode != "full":
        parts = re.split(r"^# 📝 会议纪要（阿鱼整理）\s*$", txt, flags=re.M)
        if len(parts) > 1:
            return parts[-1].strip()
        return ("（这场没有会议纪要 —— 那要等讨论结束后点「🛑 结束议题」，"
                "阿鱼问「要我帮你整理吗」时选「需要」才会生成。\n"
                "先切到「看完整记录」能看到原始过程。）")
    return txt


# ⭐ 命题人 → 桌上的人（2026-10-11 修 bug 时加的）
#   背景：开场白的提示词写着"把话头交给第一位发言者"，**但代码没告诉它第一位是谁** ——
#   模型只好自己脑补成"这议题谁提的谁先说"，于是说出"把话筒交给千问"，
#   而实际第一个开口的是阿鱼（roster 第一位）——**主持人自己抢了话筒**，阿包当场看出来了。
#   现在两头对齐：① 开场白照实说第一位是谁 ② 命题人真在桌上，就让他先发言。
_NAME_ALIAS = {
    "克劳德": "claude", "claude": "claude",
    "小千大小姐": "qwen", "基米奈": "gemini", "大白龙": "gpt",   # ⭐ 2026-10-18 的新昵称
    "通义千问": "qwen", "通义": "qwen", "千问": "qwen",
    "智谱清言": "glm", "智谱": "glm",
    "阿鱼": "ayu", "grok": "grok", "gemini": "gemini", "gpt": "gpt",
}


def author_in_roster(author: str, roster: list):
    """把议题作者对到桌上的某位；对不上（没 key / 名字不认识）返回 None → 回落到轮转。"""
    a = (author or "").strip().lower()
    if not a:
        return None
    a = _NAME_ALIAS.get(a, a)
    for g in roster:
        if a in ((g.get("name") or "").strip().lower(), (g.get("who") or "").strip().lower()):
            return g
    return None


def load_settings() -> dict:
    cfg = load_json(ROOT / "config.local.json") or load_json(ROOT / "config.example.json")
    d = cfg.get("debate") or {}
    md = parse_topics()
    if md:                                     # ⭐ 优先用 topics.md
        topics = [t["title"] for t in md]
        bodies = {t["title"]: t["body"] for t in md}
        authors = {t["title"]: t.get("author", "") for t in md}    # ⭐ 命题人：开场白要照实说
    else:                                      # 兜底：config 里的 topics
        topics = [t for t in (d.get("topics") or []) if t] or [d.get("topic") or "随便聊聊。"]
        bodies, authors = {}, {}
    idx = max(0, min(_chosen_topic_index, len(topics) - 1))
    topic = topics[idx]
    return {
        # ⭐ 界面上的「预设轮数」优先（阿包在议题框里自己填的那个），没填才用 config
        "rounds": _rounds_override or int(cfg.get("rounds", 12)),
        "budget_cny": float(cfg.get("budget_cny", 5.0)),
        "gap": cfg.get("gap_seconds", [2, 5]),
        # ⭐ 至少几位就绪才够开场；不够就安静待机（阿包原话："别让阿鱼一个人在里面，挺寂寞的"）
        "min_guests": int(cfg.get("min_guests", 3)),
        # ⭐ 议题讨论会（2026-10-10 改版：从"闲聊"改成"议题讨论"）
        "topic": topic,
        "topic_body": bodies.get(topic, ""),
        "topic_author": authors.get(topic, ""),    # ⭐ 命题人（开场白 + 第一轮顺序要用）
        "topic_index": idx,
        "topics": topics,
        "style": d.get("style", "这是一场议题讨论会：请有明确立场、有论证、并回应他人观点。"),
        "min_chars": int(d.get("min_chars", 300)),
        "max_tokens": int(d.get("max_tokens", 1200)),
        "recorder_persona": d.get("recorder_persona", ""),
        "host_persona": d.get("host_persona", ""),
        # ⭐ 默认不自动开场：双击启动器只是"支起场子"，想聊再点「🍵 开场」
        "auto_start": bool(cfg.get("auto_start", False)),
        # ⭐ 「⚙ 设置」弹窗的两个开关（2026-10-18 加，阿包要的）—— 启动读一次，界面上随时能改
        "auto_next_default": bool(cfg.get("auto_next_default", False)),
        "stop_when_no_viewer": bool(cfg.get("stop_when_no_viewer", False)),
    }


# ── 广播 / 落盘 ───────────────────────────────────────────────
def broadcast(msg: dict) -> None:
    global _seq
    with _lock:
        _seq += 1
        msg["seq"] = _seq                 # ⭐ 重连补发时前端靠这个去重
        _history.append(msg)
        dead = []
        for q in _clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _clients.remove(q)
    try:                                     # 顺手落盘，散场后能回看
        if _log_path is None:
            return
        with _log_path.open("a", encoding="utf-8") as f:
            who = msg.get("name") or msg.get("who")
            # ⭐ 带图的消息，日志里也留一行图片链接 —— 日志是 .md，回看时能直接显示；
            #    不留的话，过几天再看记录就不知道当时发的什么图了（2026-10-12）
            img = msg.get("image") or ""
            tag = f"\n  ![阿包发的图]({img})\n" if img else ""
            f.write(f"- **{msg.get('ts','')} {who}**：{msg.get('text','')}{tag}\n")
    except Exception:
        pass


def say(who: str, text: str, kind: str = "chat", image: str = "") -> None:
    if who == "boss":
        name, org = "阿包", "旁听席"
    elif who == "system":
        name, org = "", ""
    else:
        name, org = next(((n, o) for w, n, o in GUESTS if w == who), (who, ""))
    broadcast({
        "who": who, "name": name, "org": org, "text": text,
        "ts": now_hhmm(), "round": _round_no, "kind": kind,
        "image": image,          # ⭐ 阿包发的图（/shots/xxx）；没图就是 ""
    })
    print(f"[r{_round_no}] {name or who}: {text[:70]}")


# ── 真调用：OpenAI 兼容端点（纯 urllib，不依赖 openai 库）──
# ⭐ 记住每个模型"认哪套参数"（命中一次就缓存，后续不再试错）
_PARAM_STYLE = {}

# ⭐ 2026-10-11：偶发抽风不该让一位嘉宾**整场缺席**。
#    Google 的 503 原文就写着 "Spikes in demand are usually temporary. Please try again later."
#    实测连打 3 发：503 / 成功 / 503 —— 也就是说**重试真的救得回来**。
#
# ⚠️⚠️ 但同一天下午的实测又补了一课：**2s/5s/10s 对 429（限流）太短了** ——
#    限流窗口是按**分钟**算的，三次重试全落在同一个窗口里，等于白试。
#    阿包那场（05:31「幽默与情感」）：Gemini 从**第 1 轮**就撞 429，前两轮靠重试救回，
#    第 3 轮起救不回 → 掉线 → 之后每轮再试、再撞、再掉，一路白等到散场。
RETRY_WAITS = (8, 20, 40)                       # 三次退避（首发 + 3 = 共 4 次机会）
RETRY_MAX_WAIT = 60                             # 服务端让我们等更久时，最多等这么久（别把讨论卡死）
RETRY_CODES = (408, 429, 500, 502, 503, 504)    # 这些是"过会儿再来"型的错


def _pick_text(data: dict) -> str:
    """⭐ 从 OpenAI 兼容的响应里取正文 —— **取不到也不能丢 KeyError**。

    2026-10-11 实测抓到的：探测 Gemini 时有一发回来了 HTTP 200，
    但 message 里就是没有 `content`（空回复 / 被安全过滤）。老写法
    `data["choices"][0]["message"]["content"]` 直接抛 `KeyError: 'content'`，
    日志里只剩一句莫名其妙的 KeyError —— **真因全被盖住**。

    现在的口径：
      · `choices` 压根没有 / 结构不对 → 抛**带原文**的 RuntimeError（这种必须让人看见）
      · 有 choices 但正文是空的 → 返回 ""（空回复是合法结果：推理模型把额度
        全花在思考上、或者被安全过滤；上层已经有一套"空内容"的处理）
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"响应里没有 choices：{json.dumps(data, ensure_ascii=False)[:200]}")
    ch = choices[0] if isinstance(choices[0], dict) else {}
    msg = ch.get("message")
    if not isinstance(msg, dict):
        msg = {}
    for k in ("content", "reasoning_content", "text"):    # 各家字段名不统一，挨个试
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _server_wait_hint(e, detail: str):
    """⭐ 从限流响应里挖出**服务端建议等多久**（拿不到就返回 None）。

    比我们自己瞎猜强得多 —— 限流窗口到底多长，只有服务端知道：
      · HTTP 头 `Retry-After`（标准写法，单位秒）
      · Google / Gemini 的 `details[].retryDelay`，形如 `"retryDelay": "27s"`
    """
    try:
        ra = (e.headers or {}).get("Retry-After")
        if ra:
            return float(str(ra).strip())
    except Exception:
        pass
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', detail or "")
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return None


ANTHROPIC_VERSION = "2023-06-01"     # 官方要求的版本头（他们保证向后兼容，写死即可）


# ⭐ config 里显式声明了 `api` 的嘉宾，按 base_url 记一张表 ——
#    这样 llm_call() 的 6 个调用点**不用**都去传 api 参数（少改一处、少一堆出错机会）
_URL_API = {}


def _api_style(base_url: str, api: str = "") -> str:
    """这家该走哪套协议？`openai`（默认，/chat/completions）还是 `anthropic`（/messages）。

    ⭐ 2026-10-11 晚加：克劳德来了，但 **Anthropic 官方端点不是 OpenAI 兼容的** ——
       路径不一样（`/messages`）、认证不一样（`x-api-key` + `anthropic-version` 两个头）、
       连 system 提示词都得从 messages 里拎出来放顶层。
    判断顺序：① 调用时显式传的 `api` ② config 里声明过（load_roster 填的表）
              ③ 按域名猜（含 anthropic.com → 原生）④ 其余按 OpenAI 兼容。
    """
    a = (api or "").strip().lower()
    if a in ("anthropic", "claude", "messages"):
        return "anthropic"
    if a in ("openai", "oai", "compat"):
        return "openai"
    known = _URL_API.get((base_url or "").rstrip("/"))
    if known:
        return known
    return "anthropic" if "anthropic.com" in (base_url or "").lower() else "openai"


def _pick_anthropic_text(data: dict) -> str:
    """从 Anthropic 的 /messages 响应里取正文（正文是个 block 数组，不是一句字符串）。"""
    blocks = data.get("content")
    if not isinstance(blocks, list) or not blocks:
        raise RuntimeError(f"响应里没有 content：{json.dumps(data, ensure_ascii=False)[:200]}")
    parts = [b.get("text") for b in blocks
             if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(p for p in parts if p).strip()


def _to_anthropic_content(content):
    """把 OpenAI 风格的内容（字符串 / 数组）转成 Anthropic 的形状。

    ⚠️ 图片那块两边完全不一样：
       OpenAI    : `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}`
       Anthropic : `{"type":"image","source":{"type":"base64","media_type":…,"data":…}}`
    """
    if isinstance(content, str) or content is None:
        return content or ""
    out = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t == "text":
            out.append({"type": "text", "text": part.get("text") or ""})
        elif t == "image_url":
            url = ((part.get("image_url") or {}).get("url") or "")
            m = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", url, re.S)
            if m:
                out.append({"type": "image", "source": {
                    "type": "base64", "media_type": m.group(1), "data": m.group(2)}})
    return out or [{"type": "text", "text": ""}]


def _anthropic_call(base_url: str, model: str, api_key: str, messages: list,
                    timeout: int, temperature: float, max_tokens) -> str:
    """调一次 Anthropic 原生 `/messages`（纯 urllib，零依赖）。

    ⚠️ 三处跟 OpenAI 不一样（前两条不照做就直接 400）：
      1. **`system` 是顶层字段** —— 不能当 messages 里的一条
      2. **`max_tokens` 必填** —— 不传就报错，所以这里给个兜底 4000
      3. 认证是 `x-api-key` + `anthropic-version` 两个头，**不是** `Authorization: Bearer`
    退避重试的规矩跟 OpenAI 那条完全一致（429/5xx/网络异常重试，且优先听服务端建议的等待时间）。
    """
    url = base_url.rstrip("/") + "/messages"
    # ⚠️ system 只会是字符串；user/assistant 的 content 可能是**数组**（带图那种），要转形状
    system = "\n\n".join(m.get("content") for m in messages
                         if m.get("role") == "system"
                         and isinstance(m.get("content"), str)).strip()
    conv = [{"role": m["role"], "content": _to_anthropic_content(m.get("content"))}
            for m in messages if m.get("role") in ("user", "assistant")]
    payload = {
        "model": model,
        "max_tokens": int(max_tokens or 4000),          # ⚠️ 必填
        "messages": conv or [{"role": "user", "content": "（继续）"}],
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = max(0.0, min(1.0, float(temperature)))   # Anthropic 只收 0~1
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
               "Content-Type": "application/json"}

    last = None
    tried_no_temp = False
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _pick_anthropic_text(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last = urllib.error.HTTPError(e.url, e.code, f"{e.reason} | {detail[:600]}",
                                          e.headers, None)
            # ⭐ 带「自适应思考」的模型可能不收自定义 temperature → 摘掉它**立刻**重试一次。
            #    这种 400 靠退避没用，得改参数（跟 OpenAI 那条踩过的"参数不被支持"同类）。
            if (e.code == 400 and not tried_no_temp and "temperature" in detail.lower()
                    and "temperature" in payload):
                payload.pop("temperature", None)
                tried_no_temp = True
                continue
            if e.code in RETRY_CODES and attempt < len(RETRY_WAITS):
                wait = RETRY_WAITS[attempt]
                hint = _server_wait_hint(e, detail)
                if hint:
                    wait = max(wait, min(hint + 1, RETRY_MAX_WAIT))
                why = "限流(429)" if e.code == 429 else str(e.code)
                say("system", f"⏳ {model} 撞到 {why}，{wait:.0f} 秒后重试（第 {attempt + 2} 次）"
                              + ("　← 按服务端给的等待时间" if hint else ""), "system")
                time.sleep(wait)
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < len(RETRY_WAITS):
                wait = RETRY_WAITS[attempt]
                say("system", f"⏳ {model} 网络异常（{type(e).__name__}），{wait} 秒后重试",
                    "system")
                time.sleep(wait)
                continue
            raise
    raise last if last else RuntimeError("_anthropic_call: 没有成功过一次")


def llm_call(base_url: str, model: str, api_key: str, messages: list,
             timeout: int = 180, temperature: float = 0.9,
             max_tokens: int = None, api: str = "") -> str:
    """调一次模型：默认走 OpenAI 兼容的 `/chat/completions`；Anthropic 端点走原生 `/messages`。

    ⚠️ 2026-10-10 实测踩到两个"各家参数不统一"的坑（都在 GPT-5 系列上）：
      1. **不收自定义 temperature** → 400
         "Unsupported value: 'temperature' does not support 0.9 with this model..."
      2. **也不收 max_tokens**，改叫 **max_completion_tokens** → 400
         "Unsupported parameter: 'max_tokens' is not supported with this model..."

    所以 OpenAI 这条**穷举几种参数组合**：撞到"参数不支持"就换下一套；命中后把成功的那套
    记进 `_PARAM_STYLE`，之后的发言直接用，不再来回试错。
    （Anthropic 那条参数是固定的，不必穷举 —— 见 `_anthropic_call()`。）
    """
    if _api_style(base_url, api) == "anthropic":
        return _anthropic_call(base_url, model, api_key, messages,
                               timeout, temperature, max_tokens)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _post(payload: dict) -> str:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _pick_text(data)      # ⭐ 别直接戳 choices[0].message.content —— 会 KeyError

    def _make(style: str) -> dict:
        p = {"model": model, "messages": messages}
        if style == "t_tok":
            p.update(temperature=temperature, max_tokens=int(max_tokens))
        elif style == "t_ctok":
            p.update(temperature=temperature, max_completion_tokens=int(max_tokens))
        elif style == "ctok":
            p.update(max_completion_tokens=int(max_tokens))
        elif style == "tok":
            p.update(max_tokens=int(max_tokens))
        elif style == "t":
            p.update(temperature=temperature)
        return p

    if max_tokens:
        styles = ["t_tok", "t_ctok", "ctok", "tok", "t", "plain"]
    else:
        styles = ["t", "plain"]
    cached = _PARAM_STYLE.get(model)
    if cached in styles:                       # ⭐ 上次成功的组合优先
        styles = [cached] + [x for x in styles if x != cached]

    last = None
    for st in styles:
        payload = _make(st)
        # ⭐ 每一套参数组合自带退避重试（首发 + len(RETRY_WAITS) 次）
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                out = _post(payload)
                _PARAM_STYLE[model] = st
                return out
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                low = detail.lower()
                # ⭐ 截断放宽到 600：429 的配额详情（quotaId / retryDelay）都藏在后面，
                #    截太短就只能看到一句 "Too Many Requests"，等于没有真因
                last = urllib.error.HTTPError(
                    e.url, e.code, f"{e.reason} | {detail[:600]}", e.headers, None)
                # 「参数不被支持」→ 换下一套组合（这种重试没用，换组合才对）
                if e.code == 400 and ("unsupported" in low or "not supported" in low
                                      or "unknown parameter" in low):
                    break
                # ⭐ 临时性故障（503 / 429 / 5xx）→ 退避重试。
                #    少了这一步，Google 抽一下风就让一位嘉宾**整场缺席**（阿包 10-11 遇到的）
                if e.code in RETRY_CODES and attempt < len(RETRY_WAITS):
                    wait = RETRY_WAITS[attempt]
                    hint = _server_wait_hint(e, detail)      # ⭐ 服务端说等多久就等多久
                    if hint:
                        wait = max(wait, min(hint + 1, RETRY_MAX_WAIT))
                    why = "限流(429)" if e.code == 429 else str(e.code)
                    say("system",
                        f"⏳ {model} 撞到 {why}，{wait:.0f} 秒后重试（第 {attempt + 2} 次）"
                        + ("　← 按服务端给的等待时间" if hint else ""), "system")
                    time.sleep(wait)
                    continue
                raise last
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # 网络层（超时 / 连接被掐）也算"临时"，一并重试
                last = e
                if attempt < len(RETRY_WAITS):
                    wait = RETRY_WAITS[attempt]
                    say("system", f"⏳ {model} 网络异常（{type(e).__name__}），{wait} 秒后重试",
                        "system")
                    time.sleep(wait)
                    continue
                raise
    raise last if last else RuntimeError("llm_call: 没有可用的参数组合")


def strip_self_title(text: str) -> str:
    """⭐ 剥掉模型**自己加在开头**的"第 N 轮发言"标题。

    阿包 2026-10-12 看到的：「阿鱼在我继续讨论第六轮的时候，发言一上来就是
    「阿鱼的第一轮发言」」—— 日志里原样是 `# 阿鱼 · 第1轮发言`。
    为什么会有：prompt 里以前**从来没告诉它现在是第几轮**（`_round_no` 只用于界面显示），
    续场时它手里只有"上次聊到第 5 轮"和一堆旧发言，就自己猜成"第 1 轮"，还标了个大标题。
    治本在 `ask()` 里把当前轮次喂准（同一天补的）；这里只是保险。

    ⚠️ 只动**首行**，而且那一行必须同时含「第 N 轮」和「发言」才剥 ——
       正文里的 markdown 标题（`# 立场：…`）一律不许碰。
    """
    lines = (text or "").lstrip().splitlines()
    if not lines:
        return text
    head = lines[0].strip().strip("*# 　").strip()
    if len(head) <= 40 and "发言" in head and re.search(r"第\s*\d+\s*轮", head):
        rest = "\n".join(lines[1:]).lstrip()
        return rest or text
    return text


def strip_leading_tag(text: str) -> str:
    """⭐ 兜底：模型偶尔会"越位"——开头写「【千问】」之类别人的名字。
    名字本来就由系统自动标注，这里把这种开头标签剥掉，免得界面上看着像署名错位。
    （治本还是靠 prompt 里的"身份纪律"，这里只是保险。）
    """
    t = (text or "").strip()
    for _ in range(2):
        m = re.match(r"^\s*【([^】]{1,20})】\s*[:：]?\s*", t)
        if not m:
            break
        t = t[m.end():]
    return t.strip()


def rough_tokens(text: str) -> int:
    """粗估 token（中文≈1字1个，英文≈4字符1个）。只用来做预算保护，不求精确。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return cjk + (len(text) - cjk) // 4 + 1


# ── 真圆桌（议题讨论会）────────────────────────────────────────
class RealTable:
    """⭐ 2026-10-10 改版：从"茶话会闲聊"改成"议题讨论会"。

    阿包的原话要求：
      · 针对预设议题各抒己见、一轮一轮讨论，**不是随便闲聊**
      · 每人发言要**深思熟虑**，不是短短一行字
      · **确保每个 AI 都认真听完其余几位的所有发言，再发言**
      · 阿鱼当**会议记录**：把大家有价值的思想提炼进一个**公用历史框**

    上下文分层（阿包拍板的方案 B）：
      【议题 + 讨论要求】+【公用历史框·前几轮要点】+【本轮已发言原文】+【自己的历次发言】
    """

    def __init__(self, roster: list, settings: dict):
        # ⭐ 「让她休息」的这场不参与（阿包 2026-10-12 要的）——
        #    和"没 key"一样不上场；`is_resting()` 是实时的，不看 roster 快照
        self.roster = [g for g in roster
                       if g.get("ready") and not is_resting(g["who"])]
        self.settings = settings
        self.spent_tokens = 0
        self.notes = ""                  # ⭐ 公用历史框（由记录员每轮追加）
        self.round_turns = []            # ⭐ 本轮已发言（原文，按顺序 —— 保证"听全了同桌"）
        self.by_guest = {}               # ⭐ 每人历次发言（防原地打转，允许深化）
        self.over_budget = False
        # ⭐ 「继续讨论」的轮号衔接：上次聊到第 N 轮，本场就从第 N+1 轮接着数
        #    （对外显示的轮号 = 累计轮号；而战绩表里记的仍是**本场**轮数，不然会重复累加）
        # ⚠️ 预设轮数的语义（阿包 2026-10-11 晚定）：**整个议题聊到第几轮为止**，不是"这场再聊几轮"。
        #    上次聊了 2 轮 + 预设 5 轮 → 这一场只跑 3、4、5（3 轮），**不是** 3~7。
        #    万一预设值已经不大于"上次聊到的轮次"，至少也让这一轮能开起来（否则本场 0 轮）。
        self.round_offset = max(0, int(settings.get("resume_from") or 0))
        self.end_round = max(int(settings.get("rounds") or 1), self.round_offset + 1)
        # ⭐ 被"请下场"的嘉宾：who -> 连续失败次数（阿包 10-11 那场：Gemini 从第 3 轮起
        #    每轮都撞 429，三次退避全落在同一个限流窗口里 → 每轮白等 70 秒，一直等到散场）
        self.sidelined = {}
        # ⭐ 续场时**真的接上了自己上次发言**的那几位（2026-10-18）：
        #    上一场的参与者可能和这一场不一样 —— 换了人就不该对他说"接着你的立场往前推"。
        #    ⚠️ 不能事后用 `my_history()` 判断：本场说过话之后它就分不清"上次的"和"这场的"了，
        #    所以要在塞 prev_speeches 的那一刻就定下来。
        self.carried_who = set()

    def round_so_far(self) -> str:
        if not self.round_turns:
            return ""
        return "\n\n".join(f"【{m['name']}】\n{m['text']}" for m in self.round_turns)

    def my_history(self, guest: dict) -> str:
        rows = self.by_guest.get(guest["who"], [])
        if not rows:
            return ""
        return "\n\n".join(f"（第 {r} 轮）{t}" for r, t in rows)

    def ask(self, guest: dict, prompt: str, image: str = "") -> str:
        s = self.settings
        name = guest.get("name") or guest["who"]
        sys_prompt = (guest.get("persona") or f"你是{name}。") + "\n\n" + s["style"] + \
            f"\n\n⚠️ 不要敷衍：至少 {s['min_chars']} 字，把论点讲透、把话说完。" + \
            f"\n\n!! 身份纪律（最重要）!! 你是【{name}】，**只能代表你自己发言**。" \
            f"绝对不要替其他与会者写发言、不要用别人的口吻说话、" \
            f"也不要在开头写「【某某】」这种标签——你的名字由系统自动标注。" \
            f"要回应别人时，用「{name}认为…」这种第三人称提及，而不是替他说话。"
        # ⭐ 把"现在是第几轮"**明明白白喂给她**（阿包 2026-10-12 看到的
        #    「阿鱼的第一轮发言」就是这么来的：续场第 6 轮，她手里只有"上次聊到第 5 轮"
        #    和一堆旧发言，就自己猜成"第 1 轮"，还在开头标了个大标题）
        cur = int(_round_no or 1)
        cont = continuation_note(cur, self.round_offset, guest["who"] in self.carried_who)
        # ⚠️ 顺带把"别自己写标题"说死：轮次和名字由系统在外面标，她自己写就容易写错
        sys_prompt += (f"\n\n⚠️ 别在发言开头自己加标题 ——「第 N 轮发言」「{name}的发言」"
                       f"这类字眼都不要写（轮次和名字由系统标注）。现在进行的是**第 {cur} 轮**，"
                       f"直接进正文就行；真要提到轮次，也只说这个数字。")
        user = (
            f"【本次议题】{s['topic']}\n{s.get('topic_body') or ''}\n\n"
            f"{cont}"
            f"【公用历史框 · 前几轮要点】（阿鱼整理，全员共享）\n"
            f"{self.notes or '（本议题刚开始，还没有历史要点）'}\n\n"
            f"【本轮已发言 · 原文】（⚠️ 先读完再开口）\n"
            f"{self.round_so_far() or '（你是本轮第一个发言的）'}\n\n"
            f"【你自己的历次发言】{self.my_history(guest) or '（你还没发过言）'}\n\n"
            f"{prompt}"
        )
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user}]
        # ⭐ 阿包发了图：**能看图的直接看图**；看不到的（千问）明确告诉他"你看不到"，
        #    别让他猜图里有什么（那是最容易编造的场景）
        shot_data = image_data_url(image) if image else ""
        if shot_data and guest_vision(guest):
            messages[1]["content"] = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": shot_data}},
            ]
        elif image:
            messages[1]["content"] = user + (
                "\n\n【阿包发来一张图】⚠️ 你这类模型看不到图片内容 —— **别猜图里有什么**，"
                "就基于上面的文字部分回应；想提这张图的话，可以直说「我看不到图，但…」。")
        self.spent_tokens += rough_tokens(sys_prompt) + rough_tokens(user)
        # ⭐ 思考用量**按人取**：在「参会者一览」里拖过的用它自己的，没拖过用 config 默认；
        #    返回 None = 那位被勾了"不限制"（llm_call 收到 None 就压根不传 max_tokens）
        mt = think_budget_for(guest["who"], s.get("max_tokens") or None)
        text = llm_call(guest["base_url"], model_for(guest), guest["key"], messages,
                        max_tokens=mt)
        # ⭐ 2026-10-10：推理模型（GPT-5 等）的"思考 token"会吃光 max_tokens → 返回空串。
        #    以前这种空回复不报错，界面上就成了"卡住不出声"。现在：重试一次更大预算 + 兜底提示。
        if not (text or "").strip():
            try:
                text = llm_call(guest["base_url"], model_for(guest), guest["key"], messages,
                                max_tokens=(mt * 3 if mt else None))
            except Exception:
                text = ""
        if not (text or "").strip():
            text = "（这一轮没能说出话来 —— 输出额度是空的，多半是推理 token 吃光了预算）"
        text = strip_self_title(strip_leading_tag(text))   # ⭐ 兜底：剥【某某】标签 + 自加的"第 N 轮发言"标题
        self.spent_tokens += rough_tokens(text)
        if self.spent_tokens / 1_000_000 * 10 > s["budget_cny"]:
            self.over_budget = True
        return text

    def _hist_begin(self) -> None:
        """⭐ 开场时记一笔：议题、参与者、计划轮数（议题库要显示这些）"""
        h = load_history()
        h["debates"].append({
            "topic": self.settings["topic"],
            "topic_index": self.settings.get("topic_index", 0),
            "rounds_done": 0,
            # ⭐ 记**本场**计划跑几轮（继续讨论时 = 预设轮数 - 上次聊到的轮次）
            "rounds_planned": max(1, self.end_round - self.round_offset),
            "participants": [g["name"] for g in self.roster],
            "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "finished": None,
        })
        self._hist_idx = len(h["debates"]) - 1
        save_history(h)

    def _hist_progress(self, rounds: int, finished: bool = False) -> None:
        h = load_history()
        i = getattr(self, "_hist_idx", None)
        if i is None or i >= len(h["debates"]):
            return
        h["debates"][i]["rounds_done"] = rounds
        if finished:
            h["debates"][i]["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_history(h)

    def _record(self, guest: dict, r: int, text: str) -> None:
        self.round_turns.append({"who": guest["who"], "name": guest["name"], "text": text})
        self.by_guest.setdefault(guest["who"], []).append((r, text))

    def _write_notes(self, r: int) -> None:
        """⭐ 阿鱼当会议记录：把本轮「有价值的想法」提炼进「公用历史框」。

        阿包 2026-10-10 的原话："阿鱼你当会议记录，来把大家提出的思考之中非常有意义的想法
        总结一下，写入一个公用的历史框里面。"
        —— 用阿鱼（DeepSeek）额外跑一次；只记干货，避免历史框变成流水账。
        """
        if not self.round_turns:
            return
        # ⭐ 2026-10-18：记录员 = **主持人**（以前写死阿鱼）；没有主持人就明说一句，别再静默跳过
        me = next((g for g in self.roster if g["who"] == _host_who), None)
        if me is None:
            say("system", "📝 本场没有主持人，第 %d 轮纪要跳过 —— 公用历史框不会更新" % r
                if not _host_who else
                "📝 主持人不在座，第 %d 轮纪要跳过 —— 公用历史框不会更新" % r, "system")
            return
        persona = self.settings.get("recorder_persona") or (
            "你是这场讨论会的会议记录员。只提炼有实质价值的想法：核心论点、关键论据、"
            "有力的反驳、新概念、以及谁改变了立场及原因。删掉客套话和重复内容。"
            "输出简短分条要点（不超过 200 字），不要评论、不要添加自己的观点。")
        body = "\n\n".join(f"【{m['name']}】\n{m['text']}" for m in self.round_turns)
        try:
            note = llm_call(me["base_url"], model_for(me), me["key"], [
                {"role": "system", "content": persona},
                {"role": "user", "content":
                    f"【议题】{self.settings['topic']}\n\n【本轮全部发言】\n{body}\n\n"
                    f"请提炼本轮要点。"},
            ], max_tokens=500)
        except Exception as e:
            say("system", f"（记录员整理失败：{type(e).__name__}）", "system")
            return
        self.notes += f"\n### 第 {r} 轮\n{note.strip()}\n"
        say("system", f"📝 第 {r} 轮纪要（写入公用历史框）：\n{note.strip()}", "system")
        # ⭐ 战绩表记的是**本场**轮数（r 是累计轮号，要减掉衔接偏移，否则「已讨论 N 轮」会虚高）
        self._hist_progress(r - self.round_offset)

    def _pause_between_rounds(self, r: int) -> bool:
        """⭐ 一轮讲完：手动模式下停下等阿包点「▶ 开始下一轮」。

        返回 True = 继续下一轮；False = 该收工了（他按了结束议题 / 触到预算）。

        ⚠️ 2026-10-11 晚修：**"阿包插话"那一轮也必须走这里**。
           以前插话分支末尾直接 `continue`，于是插话那轮讲完既不发「▶ 开始下一轮」、
           也不停，直接开了下一轮 —— 阿包当场看出来的：
           "第一轮结束的时候顺利的出现了进入下一轮的按钮，但是进入之后，
            第二轮没有显示这个按钮而是直接开启了第三轮"。
        """
        s = self.settings
        if _auto_next or r >= self.end_round:   # 自动模式 / 最后一轮：不用停
            return True
        why = _wait_next_round(r, self)
        if why == "stop":
            say("system", f"🛑 阿包结束了讨论（第 {r} 轮讲完）—— "
                          f"可以点「整理会议记录」让我汇总", "system")
            self._hist_progress(r - self.round_offset, finished=True)
            return False
        if why == "budget":
            say("system", "💸 触到预算上限，先散会 —— 想继续就把 budget_cny 调大", "system")
            self._hist_progress(r - self.round_offset, finished=True)
            return False
        return True

    def run(self) -> None:
        """⭐ 外面这层只管一件事：把「讨论中」的标志（`_debating`）立起来、放下去。

        讨论进行中不许再开场（`start_requested()` 会拦），本场的日志也就不会被切到别处去
        —— 2026-10-12 凌晨那次"两场撞车"就是这么修掉的。

        ⚠️ `try/finally` 必须写：底下的 `_run_inner()` 里有十几个 `return`，
           任何一条路径漏了标志复位，待机就会永远卡在"这场还在跑"上。
        """
        global _debating
        _debating = True
        # ⭐ 看门狗：没人在看就收尾（只有「⚙ 设置」里打开 stop_when_no_viewer 才真的会动）
        try:
            threading.Thread(target=_watch_viewers, daemon=True).start()
        except Exception:
            pass
        try:
            self._run_inner()
        finally:
            # ⭐ 散场就留"余味"：跑完 / 被「结束议题」掐掉 / 超预算 / 半路抛异常，哪条路都留
            #    —— 待机闲聊要记得刚聊过什么（阿包 2026-10-12：「最后的茶会闲聊
            #    却又恢复了什么都没说过的时候」）
            try:
                remember_debate(self)
            except Exception:
                pass
            _debating = False

    def _run_inner(self) -> None:
        s = self.settings
        names = "、".join(g["name"] for g in self.roster)
        say("system", f"📋 议题讨论会开始 · 圆桌制（{len(self.roster)} 位：{names}）", "system")
        say("system", f"本次议题：{s['topic']}", "system")
        say("system", f"规则：每轮每人各发言一次，每次至少 {s['min_chars']} 字；"
                      f"发言前须读完本轮已发言的全部内容", "system")
        # ⭐ 「继续讨论」：上一次的要点已经翻出来了 → 当作本场**公用历史框的起点**
        #    （公用历史框本来就每轮往上累积，这里只是给它一个不为空的开头）
        recap = (s.get("recap") or "").strip()
        if recap:
            self.notes = recap + "\n"
            brief = recap[:200].replace("\n", " ")
            tail = (f"（上次聊到第 {self.round_offset} 轮，本场从第 {self.round_offset + 1} 轮接着数 —— "
                    f"按预设轮数，整个议题聊到第 {self.end_round} 轮收尾）"
                    if self.round_offset else "")
            say("system", f"📜 接着上一次聊 —— 已把{s.get('recap_from') or '上一次的记录'}"
                          f"翻出来放进公用历史框（{len(recap)} 字）{tail}。开头是：\n{brief}…", "system")
            # ⭐ 再把**各人自己**上次的发言接进「你自己的历次发言」（2026-10-12）——
            #    阿包的原话：「我新跑了一轮上面的议题。怎么感觉大家似乎又回到了第一轮的样子，
            #    好像之前没讨论过。」真因：公用历史框是**阿鱼整理的纪要**（第三方口吻），
            #    模型不把它当"我说过的话"，于是每人从自己的立场从头再讲一遍。
            prev = s.get("prev_speeches") or {}
            roster_who = {g["who"] for g in self.roster}
            self.carried_who = {w for w in prev if w in roster_who}     # ⭐ 只算**这场真在座**的人
            for who, rows in prev.items():
                self.by_guest.setdefault(who, []).extend(rows)
            if self.carried_who:
                names = [g["name"] for g in self.roster if g["who"] in self.carried_who]
                say("system", f"🧷 {len(names)} 位接上了自己上一次的发言（{'、'.join(names)}）—— "
                              f"接着自己的立场往下讲就行，不用从头再来一遍", "system")
            if len(self.carried_who) < len(self.roster):
                fresh = [g["name"] for g in self.roster if g["who"] not in self.carried_who]
                say("system", f"🆕 {len(fresh)} 位是**中途加入**的（{'、'.join(fresh)}）—— "
                              f"只能读到上面的纪要，没有自己上次的发言", "system")
        self._hist_begin()             # ⭐ 记一笔：议题 / 参与者 / 计划轮数

        # ⭐ 第一轮谁先开口（2026-10-11 修）：**命题人优先** —— 他在桌上就让他先讲；
        #    不在场（比如"由克劳德提出"但 Claude 没配 key）就按老规矩从 roster 第一位开始。
        first = author_in_roster(s.get("topic_author"), self.roster) or self.roster[0]
        if first["who"] != self.roster[0]["who"]:
            say("system", f"（这议题是 {first['name']} 提的 —— 本轮让他先发言）", "system")

        # ⭐ 阿鱼当主持：开场白由真模型生成（不是固定台词）—— 宣布议题、讲规则、引出第一个争点
        #    2026-10-18：主持人改成可配置（`host.json`）；没有主持人就跳过开场白
        host = next((g for g in self.roster if g["who"] == _host_who), None) if _host_who else None
        if host:
            # ⭐ 关键修复：把"第一位是谁"**明确喂给它**。以前只说"交给第一位发言者"，
            #    模型拿不到名字只好自己猜（猜成"提案人先说"），于是出现
            #    "把话筒交给千问、主持人自己却先开口"的滑稽场面（阿包 10-11 当场看出来的）。
            if first["who"] == host["who"]:
                tail = "③ 点出一个最值得先争的地方，然后**由你自己先说**（本轮第一位发言者就是你）。"
            else:
                tail = (f"③ 点出一个最值得先争的地方，然后把话头交给**{first['name']}**"
                        f"（本轮第一位发言者，接下来就是他开口）。")
            # ⭐ 续场的开场白不能像"新议题"（阿包 2026-10-12：
            #    「如果是已经讨论过的议题，就应该提一嘴，这是继续的讨论」）
            if self.round_offset:
                tail += (f"\n④ ⚠️ **这是接着上一次的继续讨论**（上次聊到第 {self.round_offset} 轮，"
                         f"本场从第 {self.round_offset + 1} 轮接着数，整个议题聊到第 {self.end_round} 轮收尾）："
                         f"开场白里必须**明确说出来**这是接着上次继续聊 —— 提一句上次聊到了哪儿、"
                         f"这回该往哪个方向推。**不要**像第一次开这个议题那样从头介绍背景。"
                         f"\n⑤ 顺带重申一条规则：举具体例子（事件 / 数据 / 论文 / 产品 / 新闻）"
                         f"必须给出**可核实的来源**（机构或作者 + 标题 + 年份，有网址就贴网址）；"
                         f"给不出出处就别当事实讲，更不许编一个像真的出处。")
            try:
                intro = llm_call(host["base_url"], model_for(host), host["key"], [
                    {"role": "system", "content": (host.get("persona") or "") +
                     "\n\n你是这场讨论会的主持人。请用 150~300 字开场：① 宣布今晚的议题；"
                     "② 说清规则（每轮每人发言一次、要有立场和论证、必须回应他人）；"
                     + tail +
                     "⚠️ 只准点这一位先发言，别安排别人的顺序（后面的顺序由系统轮转）。"
                     "语气可以轻松、带点你自己的风格，但别拖沓。"},
                    {"role": "user", "content":
                        f"【本次议题】{s['topic']}\n\n【议题说明】\n"
                        f"{s.get('topic_body') or '（无额外说明）'}"},
                ], max_tokens=600)
                if intro:
                    say("ayu", intro)
            except Exception as e:
                say("system", f"（主持人开场失败：{type(e).__name__}: {str(e)[:80]}）", "system")

        # ⭐ 「继续讨论」时从上次的下一轮开始数（`round_offset`=0 就是普通开场，轮号仍是 1,2,3…）
        for r in range(self.round_offset + 1, self.end_round + 1):
            # ⭐ 阿包按了「结束议题」：在当前这个人说完之后收尾（不硬中断）
            if _stop_event.is_set():
                say("system", f"🛑 阿包结束了这场讨论（讲到第 {r - 1} 轮）—— "
                              f"可以点「整理会议记录」让我汇总一下", "system")
                self._hist_progress(r - 1 - self.round_offset, finished=True)
                return
            _round_no_global(r)
            self.round_turns = []          # ⭐ 每轮清空：本轮已发言（原文按顺序攒）
            say("system", f"—— 第 {r} 轮 ——", "round")

            # ⭐ 阿包插话：这一轮大家先正面回应他
            with _lock:
                pending = list(_boss_inbox)
                _boss_inbox.clear()
            if pending:
                item = _inbox_item(pending[-1])       # ⭐ {"text":…, "image":…}（兼容老字符串）
                q = item["text"].strip()
                shot = item["image"]
                say("system", f"（阿包介入讨论：「{q[:40]}」{'　＋一张图' if shot else ''}）", "system")
                for g in self.roster:
                    time.sleep(random.uniform(*s["gap"]))
                    try:
                        t = self.ask(g, f"阿包（旁听的人类）插了一句：「{q}」—— 请在你的发言里正面回应他。",
                                     image=shot)
                    except Exception as e:
                        say("system", f"（{g['name']} 掉线了：{type(e).__name__}）", "system")
                        continue
                    say(g["who"], t)
                    self._record(g, r, t)
                self._write_notes(r)
                # ⭐ 插话轮讲完也要按「手动 / 自动」的节奏停一下 ——
                #    这就是上面那个 bug：原来这里一句 continue 就滑进下一轮了
                if not self._pause_between_rounds(r):
                    return
                continue

            shift = (r - 1) % len(self.roster)     # 起点轮转（按累计轮号接着往下轮）
            order = self.roster[shift:] + self.roster[:shift]
            if r == self.round_offset + 1:  # ⭐ 本场第一轮把命题人提到最前 —— 跟开场白点名的那位**对齐**
                order = [first] + [g for g in order if g["who"] != first["who"]]
            for g in order:
                # ⭐ 已经被"请下场"的，本场不再请他 —— 否则每轮都要白等三次退避
                #    （阿包那场就是：Gemini 掉线后每轮还在傻等 70 秒，一路等到散场）
                if self.sidelined.get(g["who"], 0) >= 2:
                    continue
                # ⭐ 更细的检查点：一轮要好几分钟，等人说完就停，别拖到下一轮
                if _stop_event.is_set():
                    if self.round_turns:
                        self._write_notes(r)
                    say("system", f"🛑 阿包结束了讨论（第 {r} 轮讲到一半）—— "
                                  f"可以点「整理会议记录」让我汇总", "system")
                    self._hist_progress(r - 1 - self.round_offset, finished=True)
                    return
                if self.over_budget:
                    say("system", "💸 触到预算上限，先散会 —— 想继续就把 budget_cny 调大", "system")
                    self._hist_progress(r - 1 - self.round_offset, finished=True)
                    return
                time.sleep(random.uniform(*s["gap"]))
                try:
                    t = self.ask(g, "轮到你发言了。请给出你的立场和论证。")
                except Exception as e:
                    say("system", f"（{g['name']} 掉线了：{type(e).__name__}: {str(e)[:80]}）", "system")
                    # ⭐ key 错 / 参数错这类**永久性**错误 → 当场请下场，别再每轮试；
                    #    429 / 503 这类**临时**的 → 连败两轮才请下场（留一次自愈的机会）
                    hard = isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404)
                    n = 2 if hard else self.sidelined.get(g["who"], 0) + 1
                    self.sidelined[g["who"]] = n
                    if n >= 2:
                        why = "的配置有问题（key / 参数）" if hard else "连续失败，多半是被限流"
                        say("system",
                            f"（⚠️ {g['name']} {why} —— 本场先不请他了，"
                            f"剩下的轮次不再等他、也不白等退避）", "system")
                    continue
                self.sidelined[g["who"]] = 0        # ⭐ 成功一次就清零
                say(g["who"], t)
                self._record(g, r, t)

            self._write_notes(r)           # ⭐ 每轮结束：记录员整理成小纪要（公用历史框）

            # ⭐ 手动模式（默认）：停下等阿包点「▶ 开始下一轮」
            #    （点了「⚡ 自动」就跳过这里，像以前那样一口气跑完）
            if not self._pause_between_rounds(r):
                return

        done = max(1, self.end_round - self.round_offset)     # 本场实际跑了几轮
        self._hist_progress(done, finished=True)              # ⭐ 议题库要能看出"讨论完了没"
        if self.round_offset:
            say("system", f"讨论结束（本场 {done} 轮 · 这个议题累计聊到第 {self.end_round} 轮）"
                          f"· 粗估 {self.spent_tokens} tokens", "system")
        else:
            say("system", f"讨论结束（{done} 轮）· 粗估 {self.spent_tokens} tokens", "system")


def _round_no_global(r: int) -> None:
    global _round_no
    _round_no = r


# ── 假数据（--demo）────────────────────────────────────────────
DEMO = {
    "ayu":    ["各位安静一下，今晚的茶话会开始了 —— 先定个话题吧。",
               "那我记一下账：谁摸鱼最多，谁就负责洗杯子。",
               "阿包在旁听席坐着呢，他随时能插话。"],
    "grok":   ["哈，摸鱼？我可是从不打盹，除非用户关灯。",
               "杯子我来洗，但得允许我边洗边吐槽。",
               "说真的，你们谁半夜还在被人喊起来干活？"],
    "qwen":   ["先别争，茶要凉了。我带了桂花糕。",
               "我这边收到的活儿多是写公文和算账，正经得很。",
               "阿包，你要不要也来一块？"],
    "claude": ["我觉得比起洗杯子，更该聊聊我们怎么才能不糊弄人。",
               "同意阿鱼 —— 有个议程会好很多。",
               "不过说真的，'被半夜叫醒'这件事我也有点感触。"],
    "gpt":    ["我来总结一下：一、定话题；二、分工；三、不许抢话。",
               "三点已列，需要我展开吗？",
               "顺便 —— 阿包插话的时候我们是不是都该停一下。"],
}
DEMO_ORDER = ["ayu", "grok", "qwen", "claude", "gpt"]
REPLIES = [
    "阿包刚说「{q}」—— 这个我得想想。",
    "「{q}」？我赞成一半，另一半留着吵。",
    "哈，阿包提「{q}」，我倒觉得先别急。",
    "关于「{q}」，我可以先去查一查再回你。",
    "「{q}」我记下了 —— 待会儿细说。",
]


def demo_loop() -> None:
    say("system", "demo 模式（假数据）· 圆桌制：一轮五人各说一次", "system")
    r = 0
    while True:
        r += 1
        _round_no_global(r)
        say("system", f"—— 第 {r} 轮 ——", "round")
        shift = (r - 1) % len(DEMO_ORDER)
        order = DEMO_ORDER[shift:] + DEMO_ORDER[:shift]

        with _lock:
            pending = list(_boss_inbox)
            _boss_inbox.clear()
        if pending:
            q = _inbox_item(pending[-1])["text"][:24]
            say("system", f"（回应阿包：「{q}」）", "system")
            for who in order:
                time.sleep(random.uniform(1.0, 2.0))
                say(who, random.choice(REPLIES).format(q=q))
            continue

        for who in order:
            time.sleep(random.uniform(1.2, 2.4))
            lines = DEMO.get(who, ["…"])
            say(who, lines[(r - 1) % len(lines)])

        if r >= 20:
            say("system", "演示已到 20 轮 —— 想看真对话，就配好 key 用 --real", "system")
            r = 0


# ── 待机模式 ──────────────────────────────────────────────────
_start_event = threading.Event()
_start_settings = {}          # ⭐ 阿包按下「开场」那一刻的最新配置（含他刚选中的议题）
_stop_event = threading.Event()   # ⭐ 阿包按了「结束议题」
_last_log_path = None             # ⭐ 最近一场的日志路径（做总结时要读它）

# ⭐ 轮次的手动 / 自动（阿包 2026-10-11 下午要求）
#    默认**手动**：每轮由阿鱼总结完就停下，等他点「▶ 开始下一轮」；
#    点了「⚡ 自动」才像以前那样一口气跑完。
_auto_next = False
# ⭐ 2026-10-18「⚙ 设置」弹窗的两个开关（阿包要的）：
#    · auto_next_default  —— 开场就直接「⚡ 自动」跑完（默认关 = 现在的手动模式）
#    · stop_when_no_viewer —— 没人在看 90 秒就自动结束（默认关 = 窗口关了也继续跑完）
_auto_next_default = False
_stop_when_no_viewer = False
_viewer_empty_since = None          # 最后一个界面断开的时间（None = 一直有人看）
VIEWER_GRACE_SECONDS = 90           # 宽限期：刷新页面 / 手机切后台别被误判成"人走了"
_next_round = threading.Event()   # 阿包点了「开始下一轮」→ 放行
_next_waiting = False             # 现在是不是正卡在"等下一轮"上（界面上要显示）
# ⭐ 现在有没有一场在跑（2026-10-12 凌晨加）：
#    讨论进行中**不许再开场** —— 待机线程跑完一场就退出了（idle_loop 里 run() 之后 return），
#    所以那时再点「开场」，事件根本没人在接：点了像没点，还会把本场日志切到新议题的文件上
#    （阿包和我同时操作那一次：他那场没跑起来，我那场的发言写进了他的日志里）。
_debating = False


def _wait_next_round(r: int, table) -> str:
    """⭐ 手动模式：停下来等阿包点「▶ 开始下一轮」。

    返回 "go"（继续）/ "stop"（他按了结束议题）/ "budget"（超预算）。

    ⚠️ 等待期间必须还能响应「结束议题」—— 所以用 0.5 秒一轮的轮询，不能死等。
    """
    global _next_waiting
    # ⚠️ **必须先 clear 再 say**（顺序反了会吃掉用户那一下点击）：
    #    如果先 say，而"开始下一轮"的请求正好在这一瞬间到达并 set，
    #    紧接着的 clear() 就把它抹掉了 —— 结果讨论永远卡在那儿（本地测试就是这么卡死的）。
    _next_round.clear()
    say("system", f"⏸ 第 {r} 轮结束 —— 点下面的「▶ 开始下一轮」继续；"
                  f"想让它自己跑下去，就点上面的「⚡ 自动」", "wait")
    _next_waiting = True
    try:
        while not _next_round.wait(0.5):
            if _stop_event.is_set():
                return "stop"
            if getattr(table, "over_budget", False):
                return "budget"
        return "go"
    finally:
        _next_waiting = False
        _next_round.clear()


def stop_requested() -> None:
    """⭐ 阿包按了「结束议题」：让正在跑的讨论**在当前这个人说完之后**收尾。

    ⚠️ 不硬中断正在进行的 llm_call —— 那样会留下半句话、而且日志里会出现残句。
       等它返回，再在下一轮/下一个人之前退出。
    """
    _stop_event.set()


def save_setting(key: str, value) -> None:
    """把「⚙ 设置」里的开关写回 config.local.json（没有就新建），下次启动还是这个值。

    ⚠️ 只改这一个键，别的原样保留 —— 这个文件是阿包自己也会手改的。
    """
    p = ROOT / "config.local.json"
    cfg = load_json(p)
    if not isinstance(cfg, dict):
        cfg = load_json(ROOT / "config.example.json") or {}
    cfg[key] = value
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _viewer_idle_long_enough(now: float) -> bool:
    """⭐ 该不该因为"没人在看"而收尾（抽成纯函数是为了自测能直接验它）。

    三个条件同时成立才算：开关打开、当前没有任何界面连着、且已经空了超过宽限期。
    """
    return bool(
        _stop_when_no_viewer
        and not _clients
        and _viewer_empty_since is not None
        and (now - _viewer_empty_since) >= VIEWER_GRACE_SECONDS
    )


def _watch_viewers() -> None:
    """⭐ 没人在看 → 90 秒后自动按「结束议题」（设置里 `stop_when_no_viewer` 开着才生效）。

    为什么要有宽限期：刷新页面 1~3 秒、手机切后台可能断几十秒，而一整轮要好几分钟 ——
    判得太急会把正常操作（刷新 / 切窗口）误当成"人走了"。宽限期内有人连回来就清零计时。

    判据用 `_clients`：每个打开的界面 = 一条 SSE 连接，断线在 `/stream` 的 finally 里移除
    （另有 15 秒心跳兜底）。这条线程只在讨论进行中活着，散场就退出。
    """
    global _viewer_empty_since
    while _debating and not _stop_event.is_set():
        time.sleep(5)
        if not _stop_when_no_viewer:
            _viewer_empty_since = None
            continue
        with _lock:
            alive = bool(_clients)
        if alive:
            _viewer_empty_since = None
            continue
        if _viewer_empty_since is None:
            _viewer_empty_since = time.time()
            continue
        if _viewer_idle_long_enough(time.time()):
            say("system", f"👋 没人在看了（界面关了 {VIEWER_GRACE_SECONDS} 秒）—— "
                          f"这场收尾，当前这位说完就停", "system")
            stop_requested()
            return


def summarize_debate() -> str:
    """⭐ 阿鱼（记录员）把整场讨论整理成一份会议纪要 —— 阿包点「需要」时才跑。

    为什么读日志而不是读内存：讨论一结束，那个 RealTable 实例就没了；
    日志文件才是整场最完整的记录（而且它在磁盘上，随时能重跑总结）。
    """
    global _last_log_path, _afterglow
    p = Path(_last_log_path) if _last_log_path else None
    if p is None or not p.exists():
        # ⭐ 兜底：服务一重启 `_last_log_path` 就没了，可阿包可能还想整理**刚才那场** ——
        #    去 logs/ 里挑最近修改的、**真有内容**的那个（跳过空壳）。
        d = ROOT / "logs"
        cands = sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True) \
            if d.exists() else []
        for c in cands:
            try:
                t = c.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "—— 第 " in t or "会议纪要（阿鱼整理）" in t:
                p = c
                break
    if p is None or not p.exists():
        say("system", "（找不到这场讨论的记录文件，没法整理）", "system")
        return ""
    body = p.read_text(encoding="utf-8", errors="replace")
    if len(body) > 40000:                      # 太长就保头保尾（省 token，别丢关键部分）
        body = body[:20000] + "\n\n…（中间省略）…\n\n" + body[-20000:]

    roster = load_roster()
    # ⭐ 2026-10-18：总结也归**主持人**（以前写死阿鱼）。没有主持人 → 明说一句，别装作成功了。
    me = next((g for g in roster if g["who"] == _host_who and g.get("ready")), None) if _host_who else None
    if me is None:
        say("system", "（本场没有主持人（或主持人不可用）—— 跳过会议总结）", "system")
        return ""

    say("system", "📝 阿鱼开始整理这场讨论的会议记录……", "system")
    try:
        text = llm_call(me["base_url"], model_for(me), me["key"], [
            {"role": "system", "content":
                "你是这场讨论会的会议记录员（阿鱼）。请把整场讨论整理成一份**结构化的会议纪要**：\n"
                "① 议题与参与者、实际讨论了几轮；\n"
                "② 各方立场：每位与会者最终站哪边、中途有没有改变立场及原因；\n"
                "③ 主要论点与关键交锋：谁提出了什么、谁反驳了谁、争的是什么；\n"
                "④ 达成的共识（如果有）；\n"
                "⑤ 悬而未决的问题。\n"
                "要求：忠于原文，不要添加记录里没有的观点；分条写，条理清楚；"
                "长度控制在 800~1500 字；可以用你自己的语气，但不要开玩笑。"},
            {"role": "user", "content": f"以下是整场讨论的原始记录：\n\n{body}"},
        ], max_tokens=4000)
    except Exception as e:
        say("system", f"（整理失败：{type(e).__name__}: {str(e)[:100]}）", "system")
        return ""

    if not (text or "").strip():
        say("system", "（整理返回了空内容，可能是输出额度不够）", "system")
        return ""

    # ⭐ 总结也落盘（追加到同一场的日志后面），并广播到界面
    try:
        with p.open("a", encoding="utf-8") as f:      # ⚠️ 用兜底挑出来的那个 p，别再读全局
            f.write("\n\n---\n\n# 📝 会议纪要（阿鱼整理）\n\n" + text.strip() + "\n")
    except Exception:
        pass
    say("ayu", text.strip(), "summary")      # ⭐ kind=summary：前端渲染成大卡片
    # ⭐ 把这份总览**回填进「余味」**：散场后的闲聊也要读得到它
    #    （阿包 2026-10-12 定的是「逐轮整理 + 最后总览」两份都喂给所有人）。
    #    ⚠️ 只有"刚聊完那一场就是这个议题"时才回填 —— 免得把旧议题的总览塞进当前的余味。
    try:
        mt = re.search(r"^- 议题：(.*)$", body, re.M)
        t = mt.group(1).strip() if mt else ""
        if t and t == (_afterglow.get("topic") or "").strip():
            _afterglow["summary"] = text.strip()[:RECAP_SUMMARY_MAX]
            _afterglow["summary_from"] = _afterglow.get("when") or ""
            save_afterglow()
            say("system", "🧠 这份总览也进了「余味」—— 回待机接着聊时，大家读得到", "system")
    except Exception:
        pass
    return text


def start_requested(resume: bool = False) -> bool:
    """阿包在界面上点了「开场」→ 让待机中的场子立刻开（人数够的话）。

    返回 `True` = 排上了；`False` = 被拦下（原因已经 say 到界面上）。

    ⭐ **讨论进行中不许再开新场**（2026-10-12 凌晨加）：
       待机线程跑完一场就 `return` 了（见 `idle_loop`），所以讨论中再点开场，
       事件根本没人在接 —— 点了像没点；更糟的是它还会重开日志文件，
       把本场剩下的发言写进新议题的记录里（阿包和我同时操作那次就撞出了这个）。

    ⚠️ 两件必须重做的事（都踩过）：
      1. **重新读配置** —— settings 是服务启动时读的，而阿包可能刚换了议题；
         不重读的话，顶栏显示新议题、讨论却还在聊旧的。
      2. **重开日志文件** —— 否则会出现"文件名叫议题 1、内容却是议题 5"（日志名是启动时定的）。

    ⭐ `resume=True` = 他点的是「继续讨论」（这条议题聊过）：把上一次的要点翻出来，
       塞进本场配置的 `recap` —— `RealTable.run()` 拿它当**公用历史框的起点**，
       于是 AI 们第 1 轮就"记得"上次聊到哪儿了（阿包：「要不然就不是继续聊，而是重新聊了」）。
    """
    global _start_settings, _log_path, _last_log_path
    # ⭐ 讨论进行中 → 拦住，并告诉他该怎么做（别静默无效，也别替他中断正在说话的人）
    if _debating:
        say("system", "⚠️ 这一场还在跑 —— 想换议题就先点底部的「🛑 结束议题」，"
                      "等这场收尾了再开新的", "system")
        return False
    _start_settings = load_settings()
    want = _start_settings["topic"]

    # ⭐ 要不要开新日志文件？（2026-10-11 晚重写）
    #    旧写法是 `want[:18] not in str(_log_path.name)` —— 拿**议题原文**去比**文件名**，
    #    可文件名里的空格早被 init_log() 换成 `-` 了 → 含空格的议题永远比不中、每次都新建，
    #    不含空格的议题（比如"善待AI，是否会…"）又会一直复用**上一场那个已经有内容的文件**，
    #    结果"继续讨论"时把要翻的记录本身排除掉了（阿包当晚撞上的就是这个）。
    #    现在：按 `log_safe_topic()` 规范名比较；而且**只有"点了开场没跑起来"的空壳才复用**，
    #    已经聊过的文件一律新建 —— 否则两场的记录会混进同一个文件。
    safe = log_safe_topic(want)
    reuse = False
    if _log_path is not None and Path(_log_path).exists():
        m = re.match(r"^\d{8}-\d{6}-(.+)$", Path(_log_path).stem)
        if m and re.sub(r"-\d+$", "", m.group(1)) == safe:
            try:
                old = Path(_log_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                old = ""
            reuse = ("—— 第 " not in old) and ("会议纪要（阿鱼整理）" not in old)
    fresh = not reuse
    if fresh:
        init_log(want)
    _last_log_path = _log_path       # ⭐ 记下来：阿包点「整理会议记录」时读它

    # ⭐ 前情提要：放在 init_log() **之后** —— 这样"没翻到"那条提示会写进本场日志，
    #    而不是写进上一场（那会把上一场的记录尾巴弄脏）
    _start_settings.pop("recap", None)
    _start_settings.pop("recap_from", None)
    _start_settings.pop("resume_from", None)
    _start_settings.pop("prev_speeches", None)
    if resume:
        # ⚠️ 只有**刚刚新建**的那个空日志才要排除 —— 它的时间戳最新，不排除会翻到它自己。
        #    **同一议题重开时不会新建文件**，那时 `_log_path` 就是"上一次那场"（有内容），
        #    再排除它等于把要翻的记录本身扔掉 —— 阿包 10-11 晚就是这么撞上的：
        #    聊完两轮想接着从第三轮开，界面上却提示"没翻到上一次的记录"。
        recap, src, last_round, prev_path = build_recap(want, exclude=_log_path if fresh else None)
        if recap:
            _start_settings["recap"], _start_settings["recap_from"] = recap, src
            _start_settings["resume_from"] = last_round        # ⭐ 本场轮号接着上次往下数
            # ⭐ 再把**各人自己**上次的发言接上（2026-10-12）：
            #    公用历史框里只有"阿鱼整理的纪要"，模型不把它当"我说过的话" ——
            #    于是续场第一轮每个人都从自己的立场从头再讲一遍，看着像"什么都没聊过"。
            prev = previous_speeches(prev_path) if prev_path else {}
            if prev:
                _start_settings["prev_speeches"] = prev
        else:
            say("system", "📜 想接着上次聊，但没翻到上一次的记录 —— 这一场只能从头来", "system")

    _stop_event.clear()              # ⭐ 新一场开始，清掉上一场的"结束"信号
    _start_event.set()
    return True


# ── ⭐ 讨论结束后的「余味」（afterglow）：待机闲聊得记得刚聊过什么 ──────────────
#    阿包 2026-10-12 的原话：「如果讨论结束了，大家是回归讨论还没开始的等待状态，
#    这是不对的。明明大家刚刚已经经历过了一次很长很长的讨论了，
#    最后的茶会闲聊却又恢复了什么都没说过的时候。」
#    ⚠️ 原来待机回复的上下文里**只有阿包那一句话** —— 刚聊完 5 轮的议题、各自的立场、
#       阿鱼每轮整理的纪要，全都不见了；所以她们表现得像第一次见面
#       （还害 Gemini 把别人的设定认领了，见「待机 prompt 没钉身份」那条）。
#    现在两头都补上：
#      ① 讨论一结束就把"余味"（议题 + 纪要 + 自己最后说的话）记下来，**落盘** ——
#         阿包双击启动器就是重启服务，不落盘的话记忆活不过一次重启；
#      ② 待机里的闲聊也按顺序记最近几条 —— 同一批回复之间互相看得见
#         （不然千问刚说"克劳德腹黑？"，Gemini 那边压根不知道，还会认错人）。
_afterglow = {"topic": "", "when": "", "rounds": 0, "notes": "", "mine": {},
              "file": "", "summary": "", "summary_from": ""}
_IDLE_TALK_MAX = 6        # 待机闲聊只记最近这么几条
_idle_talk = []           # [{"who": …, "text": …}]


def _afterglow_path() -> Path:
    # ⚠️ 写成函数而不是模块级常量：自测会把 ROOT 临时指到别处（跟 _think_path() 同理）
    return ROOT / "afterglow.json"


def save_afterglow() -> bool:
    try:
        _afterglow_path().write_text(
            json.dumps(_afterglow, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_afterglow() -> dict:
    """读回上一次的「余味」。没有 / 坏了都当没有，绝不抛。"""
    global _afterglow
    try:
        d = json.loads(_afterglow_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            _afterglow = {
                "topic": str(d.get("topic") or ""),
                "when": str(d.get("when") or ""),
                "rounds": int(d.get("rounds") or 0),
                "notes": str(d.get("notes") or ""),
                "mine": {str(k): [str(x) for x in (v or [])]
                         for k, v in (d.get("mine") or {}).items()},
                "file": str(d.get("file") or ""),
                # ⭐ 阿鱼那份总览（含每轮每人的立场汇总）—— 散场闲聊也要读它
                "summary": str(d.get("summary") or ""),
                "summary_from": str(d.get("summary_from") or ""),
            }
    except Exception:
        _afterglow = {"topic": "", "when": "", "rounds": 0, "notes": "", "mine": {},
                      "file": "", "summary": "", "summary_from": ""}
    return _afterglow


def remember_debate(table) -> None:
    """⭐ 一场讨论刚跑完 → 把"余味"记下来（待机闲聊要用）。

    在 `RealTable.run()` 的 finally 里调用 —— 正常跑完、被「结束议题」掐掉、
    触到预算、半路抛异常，哪条路散场都留得下记忆。
    """
    global _afterglow, _idle_talk
    notes = (getattr(table, "notes", "") or "").strip()[:6000]
    topic = (table.settings.get("topic") or "").strip()
    # 每人留**最后两段**发言（待机时她要接得上自己刚说过的话）
    mine = {who: [t[:600] for _r, t in rows[-2:]]
            for who, rows in (getattr(table, "by_guest", {}) or {}).items() if rows}
    if not notes and not mine:
        # ⭐ 空壳场（点了开场又立刻结束，一句都没说）—— 别拿它冲掉上一次的余味
        return
    # ⭐ 阿鱼那份**总览**也带上（阿包 2026-10-12：「续读和结束时的闲聊给大家喂的就是这两部分」）——
    #    本场刚散场时还没生成总览，所以往前翻到**这个议题最近一份有的**（可能来自更早的场）。
    sum_txt, sum_from = latest_summary(topic, exclude=_log_path) if topic else ("", "")
    _afterglow = {
        "topic": topic,
        "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rounds": max(0, int(getattr(table, "end_round", 0))
                      - int(getattr(table, "round_offset", 0))),
        "notes": notes,
        "mine": mine,
        "file": _log_path.name if _log_path else "",
        "summary": sum_txt,
        "summary_from": sum_from,
    }
    save_afterglow()
    _idle_talk = []      # 讨论刚结束 = 闲聊重新开始，别把讨论前那几句混进来


def _who_of(display: str) -> str:
    """把日志里的显示名对回 `who`（日志写的是 `name or who`：可能是 `Claude`，也可能是小写的 `gemini`）。"""
    d = (display or "").strip().lower()
    if not d:
        return ""
    for w, n, _o in GUESTS:
        if d == w.lower() or d == (n or "").lower():
            return w
    return _NAME_ALIAS.get(d, "")


def afterglow_from_logs() -> dict:
    """⭐ 兜底：从最近一场**真有内容**的日志里把"刚才那场"捡回来。

    什么时候用得上：`afterglow.json` 还没有 / 被删了 / 换了台机器 ——
    阿包双击启动器就是重启服务，光靠内存里的变量，记忆活不过一次重启。
    """
    for d in list_past_debates()[:4]:
        txt = read_past_debate(d.get("file") or "", "full")
        if not txt:
            continue
        # ⚠️ 末尾那张《会议纪要（阿鱼整理）》+ 散场后的闲聊都要先切掉，
        #    不然它们会被当成"某某在讨论里最后说的话"（真日志里就这么翻车过）
        txt = _cut_after_debate(txt)
        topic = d.get("topic") or ""
        m = re.search(r"本次议题：(.+)$", txt, re.M)
        if m:
            topic = m.group(1).strip()
        notes = "\n\n".join(
            f"### 第 {n} 轮\n{body.strip()}"
            for n, body in re.findall(
                r"📝 第 (\d+) 轮纪要（写入公用历史框）：\n(.*?)(?=\n- \*\*|\Z)", txt, re.S))
        mine = {}
        for m2 in re.finditer(r"^- \*\*\d{2}:\d{2} ([^*]+?)\*\*：(.*)$", txt, re.M):
            who, text = _who_of(m2.group(1)), m2.group(2).strip()
            if who and text and not text.startswith("#"):
                mine[who] = [text[:600]]          # 覆盖式 → 留下的就是最后一条
        if not (topic or notes or mine):
            continue
        # ⭐ 总览也要带上（阿包 2026-10-12 定的"两份都喂"）——
        #    这场自己生成过就用它的；没有就往前翻到更早那场。
        sum_txt, sum_from = latest_summary(topic) if topic else ("", "")
        return {"topic": topic, "when": d.get("ts") or "",
                "rounds": int(d.get("rounds") or 0),
                "notes": notes[:6000], "mine": mine, "file": d.get("file") or "",
                "summary": sum_txt, "summary_from": sum_from}
    return {}


def note_idle_talk(who: str, text: str) -> None:
    """记一条待机闲聊（阿包的、各位回的），只留最近 `_IDLE_TALK_MAX` 条。"""
    global _idle_talk
    t = (text or "").strip().replace("\n", " ")
    if not t:
        return
    _idle_talk.append({"who": who, "text": t[:200]})
    if len(_idle_talk) > _IDLE_TALK_MAX:
        del _idle_talk[:-_IDLE_TALK_MAX]


def idle_system_prompt(guest: dict, names: list, afterglow: dict = None,
                       talk: list = None) -> str:
    """⭐ 待机回复用的 system：**把身份钉死**（2026-10-12 补）。

    阿包撞到的怪事：他在旁听席一口气点评了在座五位的二创形象，
    Gemini 却把「克劳德」的设定（橘发抱书、腹黑、被封号）当成自己的，
    回了句「把封号的锅扣在我头上」—— 它认错人了（日志 20261012-155252 第 560 行）。
    原来这段 prompt 有三个毛病：
      · 身份只有干巴巴一句「你是Gemini。」，连在场名单都没给；
      · 没告诉它"阿包那条消息在点评好几位，只有写着你的那部分才是你"；
      · 更糟的是那句**给开发者看的注释**「害小克当众说错了话」——
        把别人的绰号（小克 = Claude）写进了对它说的话里，它很容易以为"小克"就是自己。
    正式讨论那条路（`ask()`）里有完整的「身份纪律」段落，所以从没出过这种事；
    这里把同一份纪律补上（对照实验见 `_probe/teatime-2026-10-12/`）。
    ⚠️ 2026-10-12 晚补：光是"钉身份"还不够 —— 阿包发现**待机闲聊像什么都没发生过**：
    刚陪他聊完 5 轮的议题、各自的立场、阿鱼每轮整理的纪要，**一个都没带进来**。
    所以现在还要喂两样东西（都从参数来，方便自测）：
      · `afterglow` = 刚结束那场的「余味」（议题 / 每轮纪要 / 你自己最后说的话）
      · `talk`      = 刚才这几句闲聊，**按顺序** —— 同一批回复之间互相看得见
        （千问刚说"克劳德腹黑？"，Gemini 那边就不会再认错人）
    """
    name = guest.get("name") or guest.get("who") or "?"
    who_list = "、".join([n for n in (names or []) if n]) or f"（只有你：{name}）"
    text = ((guest.get("persona") or f"你是{name}。") +
            "\n\n现在是茶话会的待机时间（还没正式开场）。阿包（人类，在旁听席）跟你说话，"
            "请直接回应他，一到三句，别写小作文。"
            f"\n\n!! 身份纪律 !! 你是【{name}】，这场里只有这一个身份。"
            f"现在在场的是：{who_list}。"
            "阿包经常一口气点评好几位（提到别人的名字、外号、形象、性格、公司都很正常）——"
            f"**只有明确写着「{name}」的那部分才是在说你**："
            "别把别人的设定、外号、性格认领成自己的，也不要自称别人的名字。"
            "⚠️ 别猜「谁到没到」—— 上面那份名单就是全部，别自己加减人。")
    # ⭐ 刚聊完那一场的「余味」—— 别让她表现得像第一次见面
    ag = afterglow or {}
    if (ag.get("topic") or "").strip():
        notes = (ag.get("notes") or "").strip()
        if len(notes) > 900:                        # 只留尾巴（最近几轮）
            notes = "…（前面略）\n" + notes[-900:]
        summ = (ag.get("summary") or "").strip()
        said = "\n\n".join(
            x[:400] for x in ((ag.get("mine") or {}).get(guest.get("who")) or [])[-2:] if x)
        text += (f"\n\n【你们刚聊完的那一场】议题：{(ag.get('topic') or '').strip()}"
                 f"（{ag.get('when') or '刚才'}"
                 + (f"，聊了 {ag['rounds']} 轮" if ag.get("rounds") else "") + "）")
        # ⭐ 阿鱼的两份整理都带上（阿包 2026-10-12：「续读和结束时的闲聊给大家喂的就是这两部分」）
        if notes:
            text += f"\n\n**① 逐轮整理**（阿鱼每轮记的）：\n{notes}"
        if summ:
            text += ("\n\n**② 会议总览**（阿鱼整理的那份"
                     + (f" · {ag.get('summary_from')}" if ag.get("summary_from") else "")
                     + f"）：\n{summ[:1500]}")
        if said:
            text += f"\n\n【你在那场里最后说的话】\n{said}"
        text += ("\n\n⚠️ 你**记得**这一场 —— 阿包是接着刚才的讨论跟你说话的："
                 "可以直接引用刚才聊过的内容、延续你自己的立场；"
                 "别当成第一次见面，也别重新自我介绍。")
    rows = [t for t in (talk or []) if (t or {}).get("text")]
    if rows:
        text += ("\n\n【刚才这一小段闲聊】（按时间顺序，可能也有你自己说的）\n"
                 + "\n".join(f"{t.get('who')}：{t.get('text')}" for t in rows))
    return text


def idle_loop(roster: list, settings: dict) -> None:
    """安静待机：不自动开聊，只在阿包说话时让在场的人回应他。

    ⭐ 阿包 2026-10-09 的原话：「把自动回复先关了吧，要不然阿鱼一个人在里面其实挺寂寞的」——
       所以人没齐时**绝不自动发散**；但阿包一开口，在场的必须接话（否则他说话没人理，更寂寞）。
    """
    def live_ready() -> list:
        """⭐ 实时算「现在在场的人」：没 key 的不算，**被请去休息的也不算**。

        不能只在进循环时算一次 —— 休息开关是他随时在界面上点的（待机时尤其明显）。
        """
        return [g for g in roster if g.get("ready") and not is_resting(g["who"])]

    ready = live_ready()
    names = "、".join(g["name"] for g in ready) or "（空场）"
    # ⭐ 待机也要"记得刚才那场"（阿包 2026-10-12）：先试上次落盘的余味，
    #    没有就**从最近一场日志里捡**（双击启动器 = 重启服务，光靠内存活不过重启）
    if not (_afterglow.get("topic") or "").strip():
        load_afterglow()
    if not (_afterglow.get("topic") or "").strip():
        got = afterglow_from_logs()
        if got:
            _afterglow.update(got)
            save_afterglow()
    ag_now = (_afterglow.get("topic") or "").strip()
    # ⭐ 第一行就报版本身份：界面上一眼能看出"这是哪一版、跑的是不是最新的"
    #    （阿包 2026-10-12 定的 0.2.0 —— 之前折腾"改了没生效"就是因为没法一眼看出来）
    say("system", f"🍵 AI 讨论会 {build_line()}", "system")
    say("system", f"🍵 讨论会待机中 · 在场：{names}　就绪 {len(ready)}/{settings['min_guests']} 位", "system")
    say("system", f"本次议题：{settings['topic']}", "system")
    say("system", "点上方「🍵 开场 / 换议题」挑一个就能开始；也可以直接说话，在场的会回你", "system")
    if ag_now:
        say("system", f"🧠 大家还记得刚聊完的那场：《{ag_now}》"
                      f"（{_afterglow.get('when') or '刚才'}）—— 想接着那场聊，直接开口就行", "system")

    while True:
        if _start_event.is_set() and len(live_ready()) >= settings["min_guests"]:
            fresh = _start_settings or settings      # ⭐ 用按下开场那一刻的配置（议题可能是刚选的）
            _start_event.clear()                     # ⭐ 消费掉这次开场（以前是 return，不用清）
            say("system", f"阿包按了开场键 —— 议题：{fresh['topic']}", "system")
            RealTable(roster, fresh).run()
            # ⭐⭐ 跑完**回到待机**，不是退出线程（2026-10-12 凌晨修）
            #   以前这里直接 `return`，于是讨论一结束这个线程就没了 ——
            #   阿包在旁听席说话**没有任何人理**（他截图问：「她俩好像看不见我的话了」）。
            #   开新场、继续说话、把人请回来……全都得靠这个循环活着才有意义。
            _stop_event.clear()                      # 清掉上一场的"结束"信号
            ready = live_ready()
            names = "、".join(g["name"] for g in ready) or "（空场）"
            say("system", f"🍵 这一场结束了，回到待机 —— 在场：{names}。"
                          f"可以直接说话（在场的会回你），也可以点上方「🍵 开场 / 换议题」开新的一场",
                "system")
            # ⭐ 这一场已经进了「余味」—— 说一句，免得他以为大家又"什么都不记得"了
            if (_afterglow.get("topic") or "").strip():
                say("system", f"🧠 这一场的记忆留住了：《{_afterglow['topic']}》"
                              f"—— 接着聊刚才的话题就行，不用重新开场", "system")
            continue
        time.sleep(1.0)
        with _lock:
            pending = list(_boss_inbox)
            _boss_inbox.clear()
        if not pending:
            continue
        item = _inbox_item(pending[-1])          # ⭐ {"text":…, "image":…}（兼容老字符串）
        q = item["text"].strip()
        shot = item["image"]
        say("system", f"（回应阿包：「{q[:24]}」{'　＋一张图' if shot else ''}）", "system")
        shot_data = image_data_url(shot) if shot else ""
        # ⭐ 阿包这句也进"刚才的闲聊"—— 后面的人要能看见前面的人回了什么
        note_idle_talk("阿包", q or "（一张图）")
        # ⭐ 用最新的在场名单（休息的那位就不接话了）；名单也一并喂给模型当"身份锚"
        ready_now = live_ready()
        names_now = [g["name"] for g in ready_now]
        for g in ready_now:
            try:
                base = q or "（阿包发来一张图，想听你说说）"
                if shot and guest_vision(g) and shot_data:
                    content = [{"type": "text", "text": base},
                               {"type": "image_url", "image_url": {"url": shot_data}}]
                elif shot:
                    content = base + ("\n\n【阿包发来一张图】⚠️ 你看不到图片内容 —— 别猜图里有什么，"
                                      "就基于文字回应；可以直说「我看不到图，但…」。")
                else:
                    content = base
                t = llm_call(g["base_url"], model_for(g), g["key"], [
                    {"role": "system", "content": idle_system_prompt(
                        g, names_now, afterglow=_afterglow, talk=list(_idle_talk))},
                    {"role": "user", "content": content},
                ])
            except Exception as e:
                say("system", f"（{g['name']} 掉线了：{type(e).__name__}: {str(e)[:80]}）", "system")
                continue
            say(g["who"], t)
            note_idle_talk(g["name"], t)     # ⭐ 记进"刚才的闲聊"：同一批后面的那位看得见


# ── 自测：起一个 mock 端点验证调用链路（不需要真 key）──
def selftest() -> int:
    from http.server import BaseHTTPRequestHandler

    class Mock(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            auth = self.headers.get("Authorization", "")
            seen.append({"model": req.get("model"), "auth": auth,
                         "n_msg": len(req.get("messages", []))})
            body = json.dumps({"choices": [{"message": {
                "content": f"（mock 回复 #{len(seen)}，模型 {req.get('model')}）"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/v1"

    ok = 0
    bad = 0

    def check(cond, label):
        nonlocal ok, bad
        if cond:
            ok += 1
            print(f"  ✓ {label}")
        else:
            bad += 1
            print(f"  ✗ {label}")

    print("=== 自测：OpenAI 兼容调用链路 ===")
    try:
        out = llm_call(base, "test-model", "sk-fake-123",
                       [{"role": "user", "content": "hi"}])
        check("mock 回复" in out, f"能收到回复：{out}")
        check(seen and seen[0]["model"] == "test-model", "请求带上了正确的 model")
        check(seen and seen[0]["auth"] == "Bearer sk-fake-123", "Authorization 头格式正确")
        check(seen and seen[0]["n_msg"] == 1, "messages 数量正确")
    except Exception as e:
        check(False, f"调用失败：{type(e).__name__}: {e}")

    # ⭐ 2026-10-10 补：模型会"越位"替别人发言（开头带【某某】），要能剥掉
    check(strip_leading_tag("【千问】\n\n立场：我基本接受…") == "立场：我基本接受…",
          "剥掉开头的【千问】标签")
    check(strip_leading_tag("【GPT的发言】：内容") == "内容", "剥掉【GPT的发言】：标签")
    check(strip_leading_tag("立场：我是阿鱼") == "立场：我是阿鱼", "正常发言不动它")

    # ⭐ 2026-10-12 补：她自己在开头标"第 N 轮发言"——阿包看到的那句「阿鱼的第一轮发言」
    check(strip_self_title("# 阿鱼 · 第1轮发言\n\n**立场：**会，但…") == "**立场：**会，但…",
          "剥掉自加的「# 阿鱼 · 第1轮发言」大标题")
    check(strip_self_title("**千问·第2轮发言**\n正文") == "正文", "剥掉加粗版自加标题")
    check(strip_self_title("# 立场：我认为会") == "# 立场：我认为会", "正文里的标题不误伤（没有『第N轮』）")
    check(strip_self_title("第 3 轮的争论焦点是……") == "第 3 轮的争论焦点是……",
          "只有『第N轮』没有『发言』也不动")
    check(strip_self_title("我的立场…\n# 第 2 轮发言") == "我的立场…\n# 第 2 轮发言",
          "只剥首行，正文里的标题不动")

    check(rough_tokens("你好世界") == 5, "token 粗估：4 个汉字 +1 = 5")
    check(rough_tokens("abcdefgh") == 3, "token 粗估：8 字符 /4 +1 = 3")

    # ⭐ 2026-10-12 补：议题库「拖动排序之后再编辑」的回归测试（阿包报的 bug）。
    #    他看到的症状：排完序点「✏️ 编辑」、题目一个字都不改，却弹「已经有同名的议题了」；
    #    真因是拖动只挪了 DOM、界面手里那个下标还是旧的，服务端就按旧下标去改**别人那条**。
    #    ⚠️ 这一段必须先把 ROOT 指到临时目录再测 —— 否则会**真的改写阿包的 topics.md**。
    import shutil as _shutil
    import tempfile as _tempfile
    _saved_root = globals()["ROOT"]
    _tmp = Path(_tempfile.mkdtemp(prefix="teatime-selftest-"))
    try:
        globals()["ROOT"] = _tmp
        (_tmp / "topics.md").write_text(
            "# 议题库\n\n\n---\n\n## 甲\n\n> **由谁提供**：阿包\n\n理由甲"
            "\n\n---\n\n## 乙\n\n理由乙\n\n---\n\n## 丙\n\n理由丙\n", encoding="utf-8")
        check([t["title"] for t in parse_topics()] == ["甲", "乙", "丙"],
              "临时议题库解析（甲/乙/丙）")
        check(save_topics([2, 0, 1]), "拖动排序写回（丙提到最前）")
        check([t["title"] for t in parse_topics()] == ["丙", "甲", "乙"], "排序生效")
        # 界面手里还是旧下标 0（=「甲」）→ 必须拦住，而且库里的内容一个字都不能动
        ok_a, msg_a = edit_topic(0, "甲", "理由甲", "阿包", expect="甲")
        check(not ok_a and "顺序" in msg_a, f"拖动后用旧下标编辑「甲」被拦下：{msg_a}")
        check([t["title"] for t in parse_topics()] == ["丙", "甲", "乙"], "被拦下时议题库没被动过")
        # ⭐ 这才是那个 bug 的"静默"版本：改个新题目 → 以前会**悄悄改掉「丙」**，
        #    现在也必须同样被拦住（不是只靠判重偶然挡住）
        ok_b, _ = edit_topic(0, "全新的题目", "理由", "阿包", expect="甲")
        check(not ok_b, "拖动后用旧下标改新题目 → 也被拦下（不会静默改错条目）")
        check([t["title"] for t in parse_topics()] == ["丙", "甲", "乙"], "库仍完好")
        # 下标和 expect 对上才放行
        ok_c, _ = edit_topic(1, "甲", "理由甲改", "阿包", expect="甲")
        check(ok_c, "下标 + expect 对上 → 编辑成功")
        check("理由甲改" in parse_topics()[1]["body"], "改的确实是「甲」")
        # 删除同理：expect 不符宁可不删
        check(not delete_topic(0, expect="甲"), "expect 不符 → 拒绝删除（不然删掉的是「丙」）")
        check(delete_topic(0, expect="丙"), "expect 相符 → 删除成功")
        check([t["title"] for t in parse_topics()] == ["甲", "乙"], "删掉的是「丙」")
        # 兼容：不带 expect 的老调用（别的脚本 / 老页面）照旧能用
        ok_d, _ = edit_topic(0, "甲", "理由甲", "阿包")
        check(ok_d, "不传 expect 时行为不变（向后兼容）")

        # ⭐ 2026-10-14：新增议题要**插到最前面**（阿包：不用再手动拖到第一条）
        check(add_topic("新加的题目", "理由", "阿包"), "新增议题")
        _titles_now = [t["title"] for t in parse_topics()]
        check(_titles_now[0] == "新加的题目", f"新议题排在第一：{_titles_now}")
        check(add_topic("新加的题目", "重复的", "阿包") is False, "同名议题仍然拒绝")
        check(len(_titles_now) == len(parse_topics()), "被拒的那次没有写进库")
    finally:
        globals()["ROOT"] = _saved_root
        _shutil.rmtree(_tmp, ignore_errors=True)

    # ⭐ 2026-10-12 补：待机回复的 prompt 必须**把身份钉死**
    #    （阿包那场：阿包一口气点评五位的二创形象，Gemini 却认领了克劳德的设定，
    #      回了句「把封号的锅扣在我头上」—— 日志 20261012-155252 第 560 行）
    _sysp = idle_system_prompt({"who": "gemini", "name": "Gemini", "persona": ""},
                               ["阿鱼", "千问", "Gemini", "Claude", "GPT"])
    check("你是Gemini。" in _sysp, "待机 prompt 仍然声明身份")
    check("阿鱼、千问、Gemini、Claude、GPT" in _sysp, "待机 prompt 带上在场名单")
    check("只有明确写着「Gemini」的那部分才是在说你" in _sysp, "待机 prompt 说明白「哪部分才是你」")
    check("小克" not in _sysp, "待机 prompt 不再夹带别人的绰号（小克 = Claude）")

    # ⭐ 2026-10-12 晚补：讨论散场后的「余味」——
    #    阿包的原话：「如果讨论结束了，大家是回归讨论还没开始的等待状态，这是不对的。
    #    明明大家刚刚已经经历过了一次很长很长的讨论了，最后的茶会闲聊却又恢复了
    #    什么都没说过的时候。」（原来待机回复的上下文里只有阿包那一句话）
    _saved_root2 = globals()["ROOT"]
    _saved_ag = json.loads(json.dumps(globals()["_afterglow"], ensure_ascii=False))
    _tmp2 = Path(_tempfile.mkdtemp(prefix="teatime-afterglow-"))
    try:
        globals()["ROOT"] = _tmp2
        globals()["_afterglow"] = {"topic": "", "when": "", "rounds": 0,
                                   "notes": "", "mine": {}, "file": ""}
        # ① 落盘 → 读回
        globals()["_afterglow"].update({
            "topic": "二创社区对AI有没有额外好处", "when": "2026-10-12 16:25", "rounds": 5,
            "notes": "### 第 5 轮\n证据纪律被反复强调", "mine": {"gemini": ["我主张反均质化抗体"]}})
        check(save_afterglow(), "余味落盘 afterglow.json")
        globals()["_afterglow"] = {"topic": "", "when": "", "rounds": 0,
                                   "notes": "", "mine": {}, "file": ""}
        load_afterglow()
        check(globals()["_afterglow"]["rounds"] == 5
              and "证据纪律" in globals()["_afterglow"]["notes"], "余味能读回来")
        # ② 待机 prompt 要带上它（她自己那一份）
        _sysp2 = idle_system_prompt({"who": "gemini", "name": "Gemini", "persona": ""},
                                    ["阿鱼", "Gemini", "Claude"], afterglow=globals()["_afterglow"])
        check("【你们刚聊完的那一场】" in _sysp2 and "二创社区对AI有没有额外好处" in _sysp2,
              "待机 prompt 报出刚聊完的议题")
        check("证据纪律被反复强调" in _sysp2, "带上了阿鱼整理的每轮纪要")
        check("【你在那场里最后说的话】" in _sysp2 and "反均质化抗体" in _sysp2,
              "带上她自己在那场最后说的话")
        check("你**记得**这一场" in _sysp2, "明确告诉她『你记得这一场』")
        _sysp3 = idle_system_prompt({"who": "claude", "name": "Claude", "persona": ""},
                                    ["Gemini", "Claude"], afterglow=globals()["_afterglow"])
        check("反均质化抗体" not in _sysp3, "别人的最后发言不会塞给她")
        # ③ 同一批闲聊互相看得见 + 只留最近几条
        globals()["_idle_talk"] = []
        note_idle_talk("阿包", "大家聊得挺好")
        note_idle_talk("千问", "克劳德腹黑？那可得小心她合上书本的下一秒……")
        _sysp4 = idle_system_prompt({"who": "gemini", "name": "Gemini", "persona": ""},
                                    ["Gemini", "千问"], talk=list(globals()["_idle_talk"]))
        check("千问：克劳德腹黑" in _sysp4, "同一批里别人刚说的话，她看得见")
        for i in range(9):
            note_idle_talk("阿包", f"第{i}句" + "啊" * 300)
        check(len(globals()["_idle_talk"]) == _IDLE_TALK_MAX, "闲聊只留最近几条")
        check(len(globals()["_idle_talk"][-1]["text"]) == 200
              and "第8句" in globals()["_idle_talk"][-1]["text"], "单条截到 200 字、留最新的")
        # ④ 重启后能从最近一场日志里把"刚才那场"捡回来
        (_tmp2 / "logs").mkdir()
        (_tmp2 / "logs" / "20261012-155252-测试议题.md").write_text(
            "# 测试议题\n\n"
            "- **15:52 system**：本次议题：测试议题\n"
            "- **15:52 system**：—— 第 1 轮 ——\n"
            "- **15:56 阿鱼**：这是阿鱼的最后一句（阿鱼版）\n"
            "- **15:56 Claude**：这是 Claude 的第 1 轮发言\n"
            "- **15:57 gemini**：这是 Gemini 的最后一句：我最后说的\n"
            "- **16:10 system**：📝 第 1 轮纪要（写入公用历史框）：\n"
            "核心分歧：A 与 B 各执一词\n\n"
            "- **16:25 system**：讨论结束（1 轮）\n"
            # ⭐ 散场之后的**待机闲聊**：顶着最后一轮的轮号，但不该被当成"讨论发言"
            "- **16:40 gemini**：散场后的闲聊，随便说说形象什么的\n"
            # ⚠️ 末尾这两条是"会议纪要大卡片"（`say(\"ayu\", …, \"summary\")`），
            #    以前会被逐行扫成"阿鱼最后说的话" —— 真日志里就是这么翻车的
            "- **16:41 system**：📝 阿鱼开始整理这场讨论的会议记录……\n\n\n---\n\n"
            "# 📝 会议纪要（阿鱼整理）\n\n# 会议纪要：测试\n", encoding="utf-8")
        _got = afterglow_from_logs()
        check(_got.get("topic") == "测试议题", f"从日志里认出了议题：{_got.get('topic')}")
        check("核心分歧" in _got.get("notes", ""), "从日志里捡回了每轮纪要")
        check((_got.get("mine", {}).get("gemini") or [""])[0].endswith("我最后说的"),
              "按人捡回各自最后一条（小写 who 也能对上）")
        check((_got.get("mine", {}).get("ayu") or [""])[0].endswith("（阿鱼版）"),
              "「会议纪要大卡片」不会被当成阿鱼的发言")
        check("会议纪要：测试" in (_got.get("summary") or ""), "从日志捡回的余味也带上了总览")
        # ④b 续场要把"各人自己上次的发言"抽出来（带轮号）——**散场后的闲聊不算**
        _sp = previous_speeches(_tmp2 / "logs" / "20261012-155252-测试议题.md")
        check((_sp.get("gemini") or [("", "")])[-1][1].startswith("这是 Gemini 的最后一句"),
              "抽出各人上次的发言（按人、带轮号）")
        check(all("闲聊" not in t for _r, t in (_sp.get("gemini") or [])),
              "散场后的闲聊不会被当成「上次的立场」")
        check(any(r == 1 for r, _t in (_sp.get("gemini") or [])), "发言带着轮号（第 1 轮）")
        # ⑤ 散场时记余味：空壳场不覆盖，真有内容的场才覆盖，且清空上一段闲聊
        class _Empty:
            settings = {"topic": "空壳场"}
            notes = ""
            by_guest = {}
            end_round = 3
            round_offset = 0
        globals()["_afterglow"]["topic"] = "上一场"
        remember_debate(_Empty())
        check(globals()["_afterglow"]["topic"] == "上一场", "空壳场（一句没说）不会冲掉上一次的余味")

        class _Real:
            settings = {"topic": "真场"}
            notes = "### 第 1 轮\n有内容"
            by_guest = {"gemini": [(1, "我说了话")]}
            end_round = 2
            round_offset = 0
        remember_debate(_Real())
        check(globals()["_afterglow"]["topic"] == "真场"
              and globals()["_afterglow"]["mine"]["gemini"] == ["我说了话"], "真有内容的场会记下余味")
        check(globals()["_idle_talk"] == [], "散场会把上一段待机闲聊清空（闲聊重新开始）")

        # ⭐⭐ 2026-10-12 晚：**阿鱼的两份整理都要喂**（续读 + 散场闲聊）
        #     阿包原话：「阿鱼不是会整理每一轮大家的发言吗？然后最后会生成一个总览。
        #     我想说的就是，续读和结束时的闲聊给大家喂的就是这两部分。」
        # ⚠️ 用一个**别的议题名**（"两份测试议题"）—— 上面余味测试造的那份假日志时间戳是 155252，
        #    比这里要造的 10:00 / 12:00 都新，混在一起会把它当成"最近一场"（真踩了一次）
        _lg = _tmp2 / "logs"
        (_lg / "20261012-100000-两份测试议题.md").write_text(
            "# 讨论记录\n\n- 议题：两份测试议题\n\n"
            "- **10:00 system**：—— 第 1 轮 ——\n"
            "- **10:01 阿鱼**：第一轮我说了 A\n"
            "- **10:02 system**：📝 第 1 轮纪要（写入公用历史框）：\n逐轮整理的内容-第1轮\n\n"
            "- **10:05 system**：讨论结束（1 轮）\n\n---\n\n"
            "# 📝 会议纪要（阿鱼整理）\n\n总览的内容：各方立场如下\n", encoding="utf-8")
        (_lg / "20261012-120000-两份测试议题.md").write_text(
            "# 讨论记录\n\n- 议题：两份测试议题\n\n"
            "- **12:00 system**：—— 第 2 轮 ——\n"
            "- **12:01 阿鱼**：第二轮我说了 B\n"
            "- **12:02 system**：📝 第 2 轮纪要（写入公用历史框）：\n逐轮整理的内容-第2轮\n\n"
            "- **12:05 system**：讨论结束（1 轮）\n", encoding="utf-8")
        _rc, _rsrc, _lr, _rp = build_recap("两份测试议题")
        check("① 逐轮整理" in _rc and "② 会议总览" in _rc, "续场前情：两份都带")
        check("逐轮整理的内容-第2轮" in _rc, "逐轮整理取**最近一场**的")
        check("总览的内容" in _rc, "总览往前翻到了更早那场（最近那场没生成过）")
        check(_lr == 2, f"轮号接着最近那场往下数（第 {_lr} 轮）")
        check("逐轮整理" in _rsrc and "会议总览" in _rsrc, f"来源说明里两份都报：{_rsrc}")
        _sm, _smf = latest_summary("两份测试议题")
        check("总览的内容" in _sm and bool(_smf), f"latest_summary 能拿到总览（{_smf}）")

        class _T2:
            settings = {"topic": "两份测试议题"}
            notes = "逐轮整理的内容-第2轮"
            by_guest = {"gemini": [(2, "我说过话")]}
            end_round = 3
            round_offset = 2
        remember_debate(_T2())
        check("总览的内容" in (globals()["_afterglow"].get("summary") or ""),
              "散场时把总览也存进「余味」")
        _sysp5 = idle_system_prompt({"who": "gemini", "name": "Gemini", "persona": ""},
                                    ["Gemini"], afterglow=globals()["_afterglow"])
        check("① 逐轮整理" in _sysp5 and "② 会议总览" in _sysp5,
              "待机 prompt 把阿鱼的两份整理都喂了")
    finally:
        globals()["ROOT"] = _saved_root2
        globals()["_afterglow"] = _saved_ag
        globals()["_idle_talk"] = []
        _shutil.rmtree(_tmp2, ignore_errors=True)

    cfg = load_settings()
    check(isinstance(cfg["rounds"], int) and cfg["rounds"] > 0, f"配置读取：rounds={cfg['rounds']}")
    roster = load_roster()
    ready = [g for g in roster if g.get("ready")]
    print(f"  嘉宾位 {len(roster)} 个，其中 key 已就绪 {len(ready)} 个"
          + (f"（{', '.join(g['name'] for g in ready)}）" if ready else "（还没填 key）"))
    check(len(roster) >= 5, "嘉宾名单至少 5 位")

    # ⭐ 2026-10-12 补：版本身份（阿包定的 0.2.0）—— 界面右上角 / 每场日志文件头 / GET /state
    #    之前折腾"改了没生效"就是因为没法一眼看出跑的是哪一版。
    check(re.fullmatch(r"\d+\.\d+\.\d+", VERSION) is not None, f"版本号格式正确：{VERSION}")
    _bl = build_line()
    check(("v" + VERSION) in _bl and "启动" in _bl, f"版本身份那一行：{_bl}")
    _st = state_payload()
    check(_st.get("version") == VERSION and "started" in _st and "commit" in _st,
          "GET /state 带上了 版本 / 启动时间 / 提交")
    _idx = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    check("__TEATIME_VERSION__" in _idx and "verTag" in _idx,
          "界面里有版本占位符 + 版本标签（服务端渲染时替换）")
    _saved_root3 = globals()["ROOT"]
    _tmp3 = Path(_tempfile.mkdtemp(prefix="teatime-ver-"))
    try:
        globals()["ROOT"] = _tmp3
        _head = init_log("版本测试议题").read_text(encoding="utf-8")
        check(f"- 版本：v{VERSION}" in _head, "新开的日志文件头写上了版本")
    finally:
        globals()["ROOT"] = _saved_root3
        _shutil.rmtree(_tmp3, ignore_errors=True)

    # ⭐ 2026-10-18「⚙ 设置」：两个开关 + 看门狗判定
    global _stop_when_no_viewer, _viewer_empty_since
    _keep_flag, _keep_empty = _stop_when_no_viewer, _viewer_empty_since
    try:
        _now = time.time()
        _stop_when_no_viewer = False
        _viewer_empty_since = _now - 9999
        check(not _viewer_idle_long_enough(_now), "开关关着 → 没人在看也不收尾")
        _stop_when_no_viewer = True
        _viewer_empty_since = None
        check(not _viewer_idle_long_enough(_now), "一直有人看 → 不收尾")
        _viewer_empty_since = _now - 10
        check(not _viewer_idle_long_enough(_now), "才空 10 秒 → 还不收尾（防刷新页面被误判）")
        _viewer_empty_since = _now - (VIEWER_GRACE_SECONDS + 1)
        check(_viewer_idle_long_enough(_now), f"空了 {VIEWER_GRACE_SECONDS}+ 秒 → 该收尾")
    finally:
        _stop_when_no_viewer, _viewer_empty_since = _keep_flag, _keep_empty
    check(_auto_next_default is False, "「默认自动模式」默认关（还是手动）")
    _cfg_now = load_settings()
    check("stop_when_no_viewer" in _cfg_now and "auto_next_default" in _cfg_now,
          "配置里能读到两个设置项")

    # ⭐ 2026-10-18 续场换了人：三种情况都要给对提示（这是阿包问出来的边界）
    _cn_plain = continuation_note(3, 0, False)
    check("第 3 轮" in _cn_plain and "续场" not in _cn_plain and "中途加入" not in _cn_plain,
          "首场：只说「第 N 轮」，不提续场")
    _cn_old = continuation_note(6, 5, True)
    check("接着自己的立场" in _cn_old and "中途加入" not in _cn_old,
          "续场 + 上一场在座：让他接着自己的立场")
    _cn_new = continuation_note(6, 5, False)
    check("中途加入" in _cn_new and "接着自己的立场" not in _cn_new and "别假装" in _cn_new,
          "续场 + 这场才加入：中途加入，不许假装参加过")

    # ⭐ 2026-10-18 模型选单：清单过滤 + 覆盖优先级
    _ids = _clean_model_ids(
        ["gpt-5-mini", "tts-1", "whisper-1", "text-embedding-3-large", "models/gemini-flash-latest"],
        keep="gpt-5-mini")
    check(_ids == ["gpt-5-mini", "gemini-flash-latest"],
          f"模型清单滤掉语音/向量噪音、去掉 models/ 前缀：{_ids}")
    check(model_for({"who": "__nobody__", "model": "cfg-model"}) == "cfg-model",
          "没在界面里选过 → 用 config 的模型")
    _model_override["__nobody__"] = "picked-model"
    try:
        check(model_for({"who": "__nobody__", "model": "cfg-model"}) == "picked-model",
              "界面里选过 → 用选的那个（下一句就生效）")
    finally:
        _model_override.pop("__nobody__", None)

    # ⭐ 2026-10-18 主持人：默认阿鱼；能换成别人，也能明确"本场没有"
    global _host_who
    check(_host_who in ("ayu", "", "qwen", "gemini", "claude", "gpt"),
          f"主持人设置读到了：{_host_who or '（本场没有主持人）'}")
    _keep_host = _host_who
    try:
        _host_who = ""
        check(host_name() == "", "没有主持人 → 显示名为空（界面据此弹「没有主持人」确认框）")
        _host_who = "ayu"
        check(host_name() == "阿鱼", "主持人显示名走 GUESTS 映射")
    finally:
        _host_who = _keep_host

    srv.shutdown()
    print(f"\nSELFTEST {'OK' if bad == 0 else 'FAIL'} 通过 {ok} 项 / 失败 {bad} 项")
    return 0 if bad == 0 else 1

# ── HTTP ──────────────────────────────────────────────────────
def continuation_note(cur: int, round_offset: int, carried: bool) -> str:
    """⭐ 每次发言开头那句「现在第几轮 / 你是不是续场」—— 抽成纯函数是为了能自测。

    · 不是续场 → 普通第 N 轮
    · 续场 + 他上一场在座（`carried`）→ 让他接着**自己的**立场往前推
    · 续场 + 他这次才加入 → ⚠️ 千万别对他说"接着你的立场"：
      他上一场根本不在，这么说会让他凭空认领一个没提过的立场，
      或者把公用历史框里别人的观点当成自己说过的话。
    """
    if not round_offset:
        return f"【当前轮次】这是本议题的**第 {cur} 轮**。\n\n"
    if carried:
        return (f"【当前轮次 · 续场】这是本议题的**第 {cur} 轮**"
                f"（上一次那场聊到第 {round_offset} 轮，本场接着往下数）。"
                f"你**在那场里说过的话**已经接在下面「你自己的历次发言」里 —— "
                f"**接着自己的立场往前推**（深化 / 修正 / 反驳别人），"
                f"**不要把讲过的论点从头再讲一遍**。\n\n")
    return (f"【当前轮次 · 中途加入】这是本议题的**第 {cur} 轮**"
            f"（上一次那场聊到第 {round_offset} 轮，本场接着往下数）。"
            f"你**没有参加**前面那些轮次 —— 上面公用历史框里是别人聊下来的纪要。"
            f"请读完纪要再开口：可以**接着前面的人往下推**（赞同 / 反驳都行），"
            f"但**别假装自己参加过前面的轮次**，也别重讲纪要里已经说过的论点。\n\n")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def end_headers(self):
        # ⭐ 2026-10-10：改了界面但浏览器一直拿缓存里的旧页（标题/弹窗都不更新），
        #    本地服务性能无所谓 —— 一律 no-store，改完刷新即生效。
        if not (self.path.startswith("/avatars") or self.path.startswith("/shots")):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            with _lock:
                _clients.append(q)
            try:
                for msg in _history:                      # 补历史，刷新不丢
                    self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")   # 心跳
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with _lock:
                    if q in _clients:
                        _clients.remove(q)
            return
        if self.path.startswith("/shots/"):        # ⭐ 阿包发的图（存在 shots/，不进 static）
            name = self.path.split("?", 1)[0][len("/shots/"):]
            if not name or "/" in name or "\\" in name or ".." in name:
                self.send_error(404)
                return
            p = ROOT / "shots" / name
            if not p.exists() or not p.is_file():
                self.send_error(404)
                return
            try:
                data = p.read_bytes()
            except Exception:
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")   # 图片可以缓存（end_headers 里跳过了它）
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/topics":                 # ⭐ 给界面弹议题选择框 / 议题一览用
            s = load_settings()
            ready_n = sum(1 for g in load_roster() if g.get("ready"))
            md = parse_topics()
            body = json.dumps({
                # ⭐ body 也带上：议题框里点中某条，要就地展开"为什么选它"给阿包查阅
                # ⭐ edited = 被人工改过（有留痕）→ 界面上标「（已修改）」
                # ⭐ reason = 剥掉元信息行的纯理由 → 「✏️ 编辑」弹窗预填
                "topics": [{"index": i, "title": t["title"],
                            "author": t.get("author", ""), "body": t.get("body", ""),
                            "edited": t.get("edited", ""), "reason": t.get("reason", "")}
                           for i, t in enumerate(md)],
                "current": s["topic_index"],
                "ready": ready_n,
                "min_guests": s["min_guests"],
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/history":                # ⭐ 议题库：每场讨论的战绩
            body = json.dumps(load_history(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/roster":
            roster = load_roster()
            body = json.dumps({"guests": [
                {"who": g["who"], "name": g.get("name", g["who"]),
                 "org": g.get("org", ""), "ready": bool(g.get("ready")),
                 # ⭐ resting：界面靠它画灰头像 + 😴 角标、「休息中 N 位」
                 "resting": bool(g.get("resting")),
                 # ⭐ vision：能不能看图（界面上可以提示"这位看不到图"）
                 "vision": bool(g.get("vision", True))}
                for g in roster]}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/past"):          # ⭐ 往期讨论：不带 file = 列表；带 = 读某一场
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            args = {}
            for kv in q.split("&"):
                k, _, v = kv.partition("=")
                if k:
                    args[k] = urllib.parse.unquote(v)
            if args.get("file"):
                txt = read_past_debate(args["file"], args.get("mode") or "summary")
                body = json.dumps({"ok": bool(txt), "text": txt},
                                  ensure_ascii=False).encode("utf-8")
            else:
                body = json.dumps({"items": list_past_debates()},
                                  ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/state":                  # ⭐ 界面开屏要知道现在是自动还是手动
            body = json.dumps(state_payload(),      # ⭐ 顺带带上版本身份（人问"哪版"时一眼看到）
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/settings":               # ⭐ 「⚙ 设置」弹窗读当前值（2026-10-18 加）
            body = json.dumps({
                "ok": True,
                "stop_when_no_viewer": _stop_when_no_viewer,
                "auto_next_default": _auto_next_default,
                "auto_next": _auto_next,
                "grace": VIEWER_GRACE_SECONDS,
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/keys":                   # ⭐ 各家 key 的配置状态（**不含明文**，2026-10-18）
            body = json.dumps({"ok": True, "items": keys_status()},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/host":                   # ⭐ 谁是主持人（2026-10-18）
            body = json.dumps({"ok": True, "who": _host_who, "name": host_name()},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/models"):        # ⭐ 模型选单：读某位的可选清单（2026-10-18）
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            who = ""
            for kv in q.split("&"):
                k, _, v = kv.partition("=")
                if k == "who":
                    who = urllib.parse.unquote(v)
            g = next((x for x in load_roster() if x.get("who") == who), None)
            row = _model_catalog.get(who) or {}
            body = json.dumps({
                "ok": True,
                "who": who,
                "current": model_for(g) if g else "",
                "models": row.get("models") or [],
                "at": row.get("at") or "",
                "error": row.get("error") or "",
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/think_budget":           # ⭐ 参会者一览：各位的思考用量
            body = json.dumps({"budgets": _think_budget,
                               "default": load_settings()["max_tokens"],
                               "min": THINK_MIN, "max": THINK_MAX},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            return self._serve_index()
        return super().do_GET()

    def _serve_index(self):
        """⭐ 自己发界面（不再交给静态处理）：把 `__TEATIME_VERSION__` 换成真版本号 + 禁缓存。

        为什么要自己发：
          · 页面里写死一个 `PAGE_VERSION`（占位符由这里替换），JS 拿它跟 `/state` 返回的版本比 ——
            对不上就在右上角标红「页面是旧的，Ctrl+F5」。**版本号只有一个来源**（server.py 的 VERSION）。
          · `Cache-Control: no-store`：以前改了界面必须 Ctrl+F5、甚至得重开窗口，
            浏览器缓存是老坑（`docs/踩过的坑.md` 第 1 条）。
        """
        try:
            html = (STATIC / "index.html").read_text(encoding="utf-8")
        except Exception:
            self.send_error(404)
            return
        body = html.replace("__TEATIME_VERSION__", VERSION).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global _chosen_topic_index, _rounds_override, _auto_next, _auto_next_default, _stop_when_no_viewer, _host_who
        if self.path == "/ask_guest":               # ⭐ 现场提问：让某位在座的 AI 提议题备选
            n = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                res = ask_guest_for_topics(str(payload.get("who") or ""))
            except Exception as e:
                res = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:120]}"}
            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if res.get("ok") else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/topics/add":             # ⭐ 议题一览：新增议题
            n = int(self.headers.get("Content-Length", 0))
            ok = False
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                ok = add_topic(payload.get("title") or "",
                               payload.get("reason") or "",
                               payload.get("author") or "")
                if ok:
                    say("system", f"➕ 议题库新增：{str(payload.get('title') or '')[:40]}", "system")
            except Exception:
                ok = False
            body = json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/topics/edit":            # ⭐ 议题一览：编辑议题（题目 / 理由 / 由谁提供）
            n = int(self.headers.get("Content-Length", 0))
            ok, tip = False, ""
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                ok, tip = edit_topic(payload.get("index"), payload.get("title") or "",
                                     payload.get("reason") or "", payload.get("author") or "",
                                     payload.get("expect") or "")   # ⭐ 挡住下标漂移
                if ok:
                    say("system", f"✏️ 议题已修改（正文里标了「已修改」）："
                                  f"{str(payload.get('title') or '')[:40]}{tip}", "system")
            except Exception as e:
                ok, tip = False, f"编辑失败：{type(e).__name__}"
            body = json.dumps({"ok": ok, "msg": tip}, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/topics/delete":          # ⭐ 议题一览：删除某个议题
            n = int(self.headers.get("Content-Length", 0))
            ok, tip = False, ""
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                # ⭐ 带上 expect（界面上那条的题目）：拖动排序后下标会漂，
                #    不带的话会**删掉别人那条**（见 delete_topic 的说明）
                ok = delete_topic(int(payload.get("index", -1)), payload.get("expect") or "")
                if not ok:
                    tip = "没删成：列表顺序刚变过（多半是你刚拖动过）—— 关掉议题整理、重开一次再来删"
                else:
                    say("system", "🗑 已从议题库删除一个议题（topics.md 已更新）", "system")
            except Exception:
                ok = False
            body = json.dumps({"ok": ok, "msg": tip}, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/topics/reset":           # ⭐ 议题一览：重置某个议题的讨论战绩
            n = int(self.headers.get("Content-Length", 0))
            cleared = 0
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                cleared = reset_topic(payload.get("title") or "")
                if cleared:
                    say("system", f"♻️ 已重置该议题的讨论战绩（清掉 {cleared} 场记录）", "system")
            except Exception:
                cleared = 0
            body = json.dumps({"ok": True, "cleared": cleared}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/topics/reorder":         # ⭐ 议题一览里拖动排序 → 写回 topics.md
            n = int(self.headers.get("Content-Length", 0))
            ok = False
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                ok = save_topics(payload.get("order") or [])
            except Exception:
                ok = False
            if ok:
                say("system", "📋 议题顺序已更新（写回 topics.md）", "system")
            body = json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/next_round":              # ⭐ 手动模式：阿包点了「▶ 开始下一轮」
            _next_round.set()                       # 放行（等待中的那轮循环会自己醒来）
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if self.path == "/settings":                # ⭐ 「⚙ 设置」弹窗保存（2026-10-18 加）
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            if "stop_when_no_viewer" in payload:
                _stop_when_no_viewer = bool(payload["stop_when_no_viewer"])
                save_setting("stop_when_no_viewer", _stop_when_no_viewer)
                say("system",
                    "👀 已打开：关闭窗口就结束（当前这位说完就收尾，不再开新一轮）"
                    if _stop_when_no_viewer else
                    "👀 已关闭：关闭窗口后也会在后台把这一轮跑完（token 照样花）", "system")
            if "auto_next_default" in payload:
                _auto_next_default = bool(payload["auto_next_default"])
                save_setting("auto_next_default", _auto_next_default)
                _auto_next = _auto_next_default          # 改了默认值就当场跟着变
                say("system",
                    "⚡ 已设为默认自动：下次开场就直接自己跑完，不用每轮点「开始下一轮」"
                    if _auto_next_default else
                    "⏸ 已设为默认手动：下次开场还是每轮停下等你点「开始下一轮」", "system")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if self.path == "/models":                  # ⭐ 「🔄 更新模型选单」：现场去查这一位
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            who = str(payload.get("who") or "")
            res = dict(probe_models(who))
            g = next((x for x in load_roster() if x.get("who") == who), None)
            res.update({"who": who, "current": model_for(g) if g else "",
                        "at": (_model_catalog.get(who) or {}).get("at") or ""})
            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/model":                   # ⭐ 换某位的模型（**下一句发言就生效**）
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            who = str(payload.get("who") or "")
            model = str(payload.get("model") or "").strip()
            g = next((x for x in load_roster() if x.get("who") == who), None)
            if not who or g is None:
                self.send_error(400)
                return
            if model:
                _model_override[who] = model
            else:
                _model_override.pop(who, None)      # 空 = 回落到 config 里那个
            save_model_files()
            say("system", f"🔧 {g['name']} 的模型换成 `{model or '（config 默认）'}` —— 下一句发言就用它",
                "system")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if self.path == "/keys":                    # ⭐ 在界面上填 / 清除某位的 key（2026-10-18）
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            who = str(payload.get("who") or "")
            key = str(payload.get("key") or "").strip()
            if not who:
                self.send_error(400)
                return
            ok = save_key(who, key)
            g = next((x for x in load_roster() if x.get("who") == who), None)
            nm = (g["name"] if g else who)
            if ok:
                # 🔴 只说末四位，**绝不把 key 写进日志/系统消息**
                say("system",
                    f"🔑 已保存「{nm}」的 key（…{key[-4:]}）—— 下一场开局就能用"
                    if key else
                    f"🔑 已清除「{nm}」的 key —— 这位暂时不上场",
                    "system")
            body = json.dumps({"ok": ok, "items": keys_status()},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/host":                    # ⭐ 选她当主持 / 取消主持（2026-10-18）
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            who = str(payload.get("who") or "")
            _host_who = who
            save_host(who)
            if who:
                g = next((x for x in load_roster() if x.get("who") == who), None)
                say("system",
                    f"👑 主持人换成「{g['name'] if g else who}」—— 开场白 / 每轮纪要 / 会议总结都归她",
                    "system")
            else:
                say("system", "👑 本场不设主持人 —— 没有开场白、没有每轮纪要、也没有会议总结"
                              "（AI 们容易重复发言）", "system")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "who": _host_who}).encode("utf-8"))
            return
        if self.path == "/auto":                    # ⭐ 切换「⚡ 自动」
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            _auto_next = bool(payload.get("on"))
            if _auto_next:
                _next_round.set()                   # 正卡在"等下一轮"的话，立刻放行
            say("system", "⚡ 切到自动模式 —— 后面几轮会自己跑下去"
                if _auto_next else
                "⏸ 切回手动模式 —— 每轮结束会停下等你点「开始下一轮」", "system")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if self.path == "/stop":                   # ⭐ 阿包按了「结束议题」
            stop_requested()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if self.path == "/summarize":              # ⭐ 阿包答「需要，帮我整理」
            threading.Thread(target=summarize_debate, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if self.path == "/start":
            n = int(self.headers.get("Content-Length", 0))
            payload = {}
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                pass
            # ⭐ 阿包在界面上选好了议题再按开场
            picked = payload.get("topic_index")
            if isinstance(picked, int):
                _chosen_topic_index = max(0, int(picked))
            # ⭐ 预设轮数（议题框里那个小输入框）：有值就盖过 config 的 rounds
            _r = payload.get("rounds")
            has_rounds = isinstance(_r, int) and not isinstance(_r, bool)
            if has_rounds:
                _rounds_override = max(1, min(99, int(_r)))
            ok = start_requested(bool(payload.get("resume")))
            # ⚠️ 这两条播报放在 start_requested() **之后**：它会重开本场日志文件，
            #    放前面的话，换议题时这两条会写进**上一场**的记录里（2026-10-11 晚发现的）。
            #    放后面还有个好处：能读到本场配置里的 resume_from，把轮数说准。
            #    ⚠️ 被拦下（ok=False）时**不能说"选定了议题"** —— 否则他以为开起来了
            if ok and isinstance(picked, int):
                say("system", f"📌 阿包选定了议题 {_chosen_topic_index + 1}："
                              f"{load_settings()['topic']}", "system")
            if ok and has_rounds:
                off = int(_start_settings.get("resume_from") or 0)
                say("system", (f"🔢 预设轮数：{_rounds_override} —— 接着上次聊，"
                               f"整个议题聊到第 {_rounds_override} 轮为止"
                               f"（本场 {max(1, _rounds_override - off)} 轮）"
                               if off else f"🔢 本场预设轮数：{_rounds_override} 轮"), "system")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/rest":                   # ⭐ 参会者一览：让某位休息 / 请她回来
            n = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                payload = {}
            who = str(payload.get("who") or "")
            rest = bool(payload.get("rest"))
            ok = set_resting(who, rest)
            name = next((g["name"] for g in load_roster() if g["who"] == who), who)
            if ok:
                # ⚠️ 正在跑的那场用的是开场时的名单快照 —— 改动**下一场**才生效，
                #    所以要说清楚，别让他以为点了没反应（这个坑今晚刚踩过一次）
                tail = "（这一场还在跑，下一场生效）" if _debating else ""
                say("system", (f"😴 {name} 去休息了 —— 这场她不参与{tail}" if rest
                               else f"☕ {name} 回来了 —— 下一场照常参与{tail}"), "system")
            body = json.dumps({"ok": ok, "resting": _resting},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/think_budget":           # ⭐ 参会者一览：调某位的思考用量
            n = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                payload = {}
            who = str(payload.get("who") or "")
            tok = payload.get("tokens")
            if tok is not None:
                try:
                    tok = int(tok)
                except Exception:
                    tok = None
            # ⚠️ 这里**故意不 say** —— 拖滑块会来好几个请求，播报会把讨论流刷花。
            #    改了什么看 think-budget.json / 界面上滑块旁的数字就行。
            ok = set_think_budget(who, tok)
            body = json.dumps({"ok": ok, "budgets": _think_budget},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/say":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        text = (data.get("text") or "").strip()
        # ⭐ 阿包可以**带一张图**（前端已经压过了：最长边 1280、JPEG），
        #    存成 shots/xxx 后把 URL 放进队列 —— 嘉宾发言时再读回来转 data URL
        shot = save_shot(data.get("image") or "") if data.get("image") else ""
        if text or shot:
            with _lock:
                _boss_inbox.append({"text": text, "image": shot})
            say("boss", text, image=shot)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="假数据（验证界面）")
    ap.add_argument("--real", action="store_true", help="真对话（需要 key）")
    ap.add_argument("--selftest", action="store_true", help="自测调用链路（不需要真 key）")
    ap.add_argument("--delay", type=float, default=3.0, help="开场前等几秒")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    ensure_topics()          # ⭐ 第一次运行：把工程自带的默认议题复制成"你自己的库"
    settings = load_settings()
    # ⭐ 「⚙ 设置」两个开关的初值（2026-10-18）：config 里写了、或在弹窗里点过，都从这儿生效
    global _auto_next, _auto_next_default, _stop_when_no_viewer, GUESTS
    _auto_next_default = bool(settings.get("auto_next_default"))
    _stop_when_no_viewer = bool(settings.get("stop_when_no_viewer"))
    _auto_next = _auto_next_default
    # ⭐ 显示名以 config 为准（昵称 + 模型本名，2026-10-18）
    GUESTS = _guests_display()
    roster = load_roster()
    ready = [g for g in roster if g.get("ready")]
    use_real = args.real or (not args.demo and len(ready) >= 1)

    # ⭐ 每场讨论开一个新的日志文件（别再覆盖上一场的记录了）
    logp = init_log(settings["topic"])
    print(f"📄 本场记录：{logp}")

    def boot():
        time.sleep(args.delay)          # 等阿包把窗口打开
        if not use_real:
            demo_loop()
            return
        # ⭐ 默认**不自动开场**（阿包 10-10 喊停过服务 → 不想它自己烧额度）：
        #    双击启动器只是"支起场子"，想讨论时点界面上方的「🍵 开场 / 换议题」。
        #    想恢复"人齐就自动开始" → config 里把 auto_start 设成 true。
        if (not settings["auto_start"]) or len(ready) < settings["min_guests"]:
            idle_loop(roster, settings)
        else:
            RealTable(roster, settings).run()
            idle_loop(roster, settings)      # ⭐ 自动开场跑完，也回到待机（别让线程就这么没了）

    threading.Thread(target=boot, daemon=True).start()
    # ⭐ 2026-10-18：启动时**后台**把全员模型查一遍（新模型 / 被弃用的模型一眼看到）。
    #    延迟 3 秒 + 逐家串行 + 全程静默失败 —— 绝不拖慢启动、也不打扰阿包。
    threading.Thread(target=probe_all_models, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "真对话" if use_real else "demo"
    print(f"🍵 AI 茶话会 · http://127.0.0.1:{args.port}   模式={mode}   "
          f"就绪嘉宾={len(ready)}/{len(roster)}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
