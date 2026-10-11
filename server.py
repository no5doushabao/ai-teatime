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
import json
import os
import queue
import random
import re
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

# ── 桌面阵容（头像/名字/组织；真接入时 model/base_url 从 config 覆盖）──
GUESTS = [
    ("ayu",    "阿鱼",     "DeepSeek"),
    ("grok",   "Grok",     "xAI"),
    ("qwen",   "千问",     "通义千问"),
    ("claude", "Claude",   "Anthropic"),
    ("gpt",    "GPT",      "OpenAI"),
]

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
    """开一场新的日志文件（logs/日期-时间-议题.md）。返回路径，同时设好全局 _log_path。"""
    global _log_path
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
        f"# 讨论记录\n\n- 开始：{datetime.now():%Y-%m-%d %H:%M}\n- 议题：{topic}\n\n",
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

    追加到末尾（位置可以之后拖动调整）。同名的直接拒绝，免得议题一览里出现两个一样的。
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
    items.append({"title": title, "body": "\n\n".join(parts)})
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


def delete_topic(index: int) -> bool:
    """⭐ 删掉一个议题（议题一览里的「删除」按钮）。"""
    items = parse_topics()
    if not items or not (0 <= int(index) < len(items)):
        return False
    return write_topics([t for i, t in enumerate(items) if i != int(index)])


def edit_topic(index, title: str, reason: str, author: str):
    """⭐ 编辑一条议题（议题整理里的「✏️ 编辑」按钮）。

    三个字段都能改：题目 / 理由正文 / 由谁提供。
    改过的议题会在正文里留一行 `> ✏️ 已修改：<时间>` —— 界面据此标「（已修改）」，
    也让"这条被动过"在 `topics.md` 里看得见（阿包 2026-10-11 晚要的）。

    ⚠️ 改了题目的话，把 `debate-history.json` 里的旧题目一并改过来 ——
       战绩是按题目名匹配的，不改的话「已讨论 N 个轮次 / 参与者」当场断链。

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
        text = llm_call(g["base_url"], g["model"], g["key"], [
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
RECAP_MAX = 4000        # 前情提要最多带这么多字（省 token，别把上下文撑爆）


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


def build_recap(topic: str, exclude=None):
    """⭐ 翻出上一次聊这个议题的要点，做成"前情提要"。返回 `(文本, 来源说明)`。

    文本为空 = 没翻到（这一场只能当新议题聊）。取法优先拿**已经提炼过的**（省 token）：
      ① 上一场结尾的**会议纪要**（阿鱼整理的那份大总结）
      ② 没纪要就拼各轮的**公用历史框小纪要**

    ⚠️ 会**一场一场往前翻**：最近那场如果是"空壳"（只点了开场、一轮没跑起来），
       就继续找更早的 —— 否则明明有真记录，却因为最近一个是空的而用不上。
    """
    for p in find_debates(topic, exclude=exclude):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        s = p.stem
        when = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}"

        mark = "# 📝 会议纪要（阿鱼整理）"
        if mark in txt:
            rec = txt.split(mark, 1)[1].strip()
            if rec:
                return (f"【上一次（{when}）的会议纪要 · 阿鱼整理】\n{rec}"[:RECAP_MAX],
                        f"{when} 那场的会议纪要")

        notes = [(n, t.strip()) for n, t in
                 re.findall(r"📝 第 (\d+) 轮纪要（写入公用历史框）：\n(.*?)(?=\n- \*\*|\Z)", txt, re.S)
                 if t.strip()]
        if notes:
            body = (f"【上一次（{when}）的逐轮要点】\n" +
                    "\n\n".join(f"### 第 {n} 轮\n{t}" for n, t in notes))
            return body[:RECAP_MAX], f"{when} 那场的逐轮要点（{len(notes)} 轮）"
    return "", ""


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
            f.write(f"- **{msg.get('ts','')} {who}**：{msg.get('text','')}\n")
    except Exception:
        pass


def say(who: str, text: str, kind: str = "chat") -> None:
    if who == "boss":
        name, org = "阿包", "旁听席"
    elif who == "system":
        name, org = "", ""
    else:
        name, org = next(((n, o) for w, n, o in GUESTS if w == who), (who, ""))
    broadcast({
        "who": who, "name": name, "org": org, "text": text,
        "ts": now_hhmm(), "round": _round_no, "kind": kind,
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


def llm_call(base_url: str, model: str, api_key: str, messages: list,
             timeout: int = 180, temperature: float = 0.9,
             max_tokens: int = None) -> str:
    """调一次 OpenAI 兼容的 /chat/completions。

    ⚠️ 2026-10-10 实测踩到两个"各家参数不统一"的坑（都在 GPT-5 系列上）：
      1. **不收自定义 temperature** → 400
         "Unsupported value: 'temperature' does not support 0.9 with this model..."
      2. **也不收 max_tokens**，改叫 **max_completion_tokens** → 400
         "Unsupported parameter: 'max_tokens' is not supported with this model..."

    所以这里**穷举几种参数组合**：撞到"参数不支持"就换下一套；命中后把成功的那套
    记进 `_PARAM_STYLE`，之后的发言直接用，不再来回试错。
    """
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
        self.roster = [g for g in roster if g.get("ready")]
        self.settings = settings
        self.spent_tokens = 0
        self.notes = ""                  # ⭐ 公用历史框（由记录员每轮追加）
        self.round_turns = []            # ⭐ 本轮已发言（原文，按顺序 —— 保证"听全了同桌"）
        self.by_guest = {}               # ⭐ 每人历次发言（防原地打转，允许深化）
        self.over_budget = False
        # ⭐ 被"请下场"的嘉宾：who -> 连续失败次数（阿包 10-11 那场：Gemini 从第 3 轮起
        #    每轮都撞 429，三次退避全落在同一个限流窗口里 → 每轮白等 70 秒，一直等到散场）
        self.sidelined = {}

    def round_so_far(self) -> str:
        if not self.round_turns:
            return ""
        return "\n\n".join(f"【{m['name']}】\n{m['text']}" for m in self.round_turns)

    def my_history(self, guest: dict) -> str:
        rows = self.by_guest.get(guest["who"], [])
        if not rows:
            return ""
        return "\n\n".join(f"（第 {r} 轮）{t}" for r, t in rows)

    def ask(self, guest: dict, prompt: str) -> str:
        s = self.settings
        name = guest.get("name") or guest["who"]
        sys_prompt = (guest.get("persona") or f"你是{name}。") + "\n\n" + s["style"] + \
            f"\n\n⚠️ 不要敷衍：至少 {s['min_chars']} 字，把论点讲透、把话说完。" + \
            f"\n\n!! 身份纪律（最重要）!! 你是【{name}】，**只能代表你自己发言**。" \
            f"绝对不要替其他与会者写发言、不要用别人的口吻说话、" \
            f"也不要在开头写「【某某】」这种标签——你的名字由系统自动标注。" \
            f"要回应别人时，用「{name}认为…」这种第三人称提及，而不是替他说话。"
        user = (
            f"【本次议题】{s['topic']}\n{s.get('topic_body') or ''}\n\n"
            f"【公用历史框 · 前几轮要点】（阿鱼整理，全员共享）\n"
            f"{self.notes or '（本议题刚开始，还没有历史要点）'}\n\n"
            f"【本轮已发言 · 原文】（⚠️ 先读完再开口）\n"
            f"{self.round_so_far() or '（你是本轮第一个发言的）'}\n\n"
            f"【你自己的历次发言】\n{self.my_history(guest) or '（你还没发过言）'}\n\n"
            f"{prompt}"
        )
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user}]
        self.spent_tokens += rough_tokens(sys_prompt) + rough_tokens(user)
        # ⭐ 思考用量**按人取**：在「参会者一览」里拖过的用它自己的，没拖过用 config 默认；
        #    返回 None = 那位被勾了"不限制"（llm_call 收到 None 就压根不传 max_tokens）
        mt = think_budget_for(guest["who"], s.get("max_tokens") or None)
        text = llm_call(guest["base_url"], guest["model"], guest["key"], messages,
                        max_tokens=mt)
        # ⭐ 2026-10-10：推理模型（GPT-5 等）的"思考 token"会吃光 max_tokens → 返回空串。
        #    以前这种空回复不报错，界面上就成了"卡住不出声"。现在：重试一次更大预算 + 兜底提示。
        if not (text or "").strip():
            try:
                text = llm_call(guest["base_url"], guest["model"], guest["key"], messages,
                                max_tokens=(mt * 3 if mt else None))
            except Exception:
                text = ""
        if not (text or "").strip():
            text = "（这一轮没能说出话来 —— 输出额度是空的，多半是推理 token 吃光了预算）"
        text = strip_leading_tag(text)     # ⭐ 兜底：剥掉模型自己加的【某某】开头
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
            "rounds_planned": self.settings["rounds"],
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
        me = next((g for g in self.roster if g["who"] == "ayu"), None)
        if me is None:
            return
        persona = self.settings.get("recorder_persona") or (
            "你是这场讨论会的会议记录员。只提炼有实质价值的想法：核心论点、关键论据、"
            "有力的反驳、新概念、以及谁改变了立场及原因。删掉客套话和重复内容。"
            "输出简短分条要点（不超过 200 字），不要评论、不要添加自己的观点。")
        body = "\n\n".join(f"【{m['name']}】\n{m['text']}" for m in self.round_turns)
        try:
            note = llm_call(me["base_url"], me["model"], me["key"], [
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
        self._hist_progress(r)         # ⭐ 议题库要显示"讨论到第几轮"

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
        if _auto_next or r >= s["rounds"]:      # 自动模式 / 最后一轮：不用停
            return True
        why = _wait_next_round(r, self)
        if why == "stop":
            say("system", f"🛑 阿包结束了讨论（第 {r} 轮讲完）—— "
                          f"可以点「整理会议记录」让我汇总", "system")
            self._hist_progress(r, finished=True)
            return False
        if why == "budget":
            say("system", "💸 触到预算上限，先散会 —— 想继续就把 budget_cny 调大", "system")
            self._hist_progress(r, finished=True)
            return False
        return True

    def run(self) -> None:
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
            say("system", f"📜 接着上一次聊 —— 已把{s.get('recap_from') or '上一次的记录'}"
                          f"翻出来放进公用历史框（{len(recap)} 字）。开头是：\n{brief}…", "system")
        self._hist_begin()             # ⭐ 记一笔：议题 / 参与者 / 计划轮数

        # ⭐ 第一轮谁先开口（2026-10-11 修）：**命题人优先** —— 他在桌上就让他先讲；
        #    不在场（比如"由克劳德提出"但 Claude 没配 key）就按老规矩从 roster 第一位开始。
        first = author_in_roster(s.get("topic_author"), self.roster) or self.roster[0]
        if first["who"] != self.roster[0]["who"]:
            say("system", f"（这议题是 {first['name']} 提的 —— 本轮让他先发言）", "system")

        # ⭐ 阿鱼当主持：开场白由真模型生成（不是固定台词）—— 宣布议题、讲规则、引出第一个争点
        host = next((g for g in self.roster if g["who"] == "ayu"), None)
        if host:
            # ⭐ 关键修复：把"第一位是谁"**明确喂给它**。以前只说"交给第一位发言者"，
            #    模型拿不到名字只好自己猜（猜成"提案人先说"），于是出现
            #    "把话筒交给千问、主持人自己却先开口"的滑稽场面（阿包 10-11 当场看出来的）。
            if first["who"] == host["who"]:
                tail = "③ 点出一个最值得先争的地方，然后**由你自己先说**（本轮第一位发言者就是你）。"
            else:
                tail = (f"③ 点出一个最值得先争的地方，然后把话头交给**{first['name']}**"
                        f"（本轮第一位发言者，接下来就是他开口）。")
            try:
                intro = llm_call(host["base_url"], host["model"], host["key"], [
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

        for r in range(1, s["rounds"] + 1):
            # ⭐ 阿包按了「结束议题」：在当前这个人说完之后收尾（不硬中断）
            if _stop_event.is_set():
                say("system", f"🛑 阿包结束了这场讨论（讲到第 {r - 1} 轮）—— "
                              f"可以点「整理会议记录」让我汇总一下", "system")
                self._hist_progress(r - 1, finished=True)
                return
            _round_no_global(r)
            self.round_turns = []          # ⭐ 每轮清空：本轮已发言（原文按顺序攒）
            say("system", f"—— 第 {r} 轮 ——", "round")

            # ⭐ 阿包插话：这一轮大家先正面回应他
            with _lock:
                pending = list(_boss_inbox)
                _boss_inbox.clear()
            if pending:
                q = pending[-1]
                say("system", f"（阿包介入讨论：「{q[:40]}」）", "system")
                for g in self.roster:
                    time.sleep(random.uniform(*s["gap"]))
                    try:
                        t = self.ask(g, f"阿包（旁听的人类）插了一句：「{q}」—— 请在你的发言里正面回应他。")
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

            shift = (r - 1) % len(self.roster)
            order = self.roster[shift:] + self.roster[:shift]      # 起点轮转
            if r == 1:      # ⭐ 第一轮把命题人提到最前 —— 跟开场白里点名的那位**对齐**
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
                    self._hist_progress(r - 1, finished=True)
                    return
                if self.over_budget:
                    say("system", "💸 触到预算上限，先散会 —— 想继续就把 budget_cny 调大", "system")
                    self._hist_progress(r - 1, finished=True)
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

        self._hist_progress(s["rounds"], finished=True)      # ⭐ 议题库要能看出"讨论完了没"
        say("system", f"讨论结束（{s['rounds']} 轮）· 粗估 {self.spent_tokens} tokens", "system")


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
            q = pending[-1][:24]
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
_next_round = threading.Event()   # 阿包点了「开始下一轮」→ 放行
_next_waiting = False             # 现在是不是正卡在"等下一轮"上（界面上要显示）


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


def summarize_debate() -> str:
    """⭐ 阿鱼（记录员）把整场讨论整理成一份会议纪要 —— 阿包点「需要」时才跑。

    为什么读日志而不是读内存：讨论一结束，那个 RealTable 实例就没了；
    日志文件才是整场最完整的记录（而且它在磁盘上，随时能重跑总结）。
    """
    global _last_log_path
    if not _last_log_path or not Path(_last_log_path).exists():
        say("system", "（找不到这场讨论的记录文件，没法整理）", "system")
        return ""
    body = Path(_last_log_path).read_text(encoding="utf-8", errors="replace")
    if len(body) > 40000:                      # 太长就保头保尾（省 token，别丢关键部分）
        body = body[:20000] + "\n\n…（中间省略）…\n\n" + body[-20000:]

    roster = load_roster()
    me = next((g for g in roster if g["who"] == "ayu" and g.get("ready")), None)
    if me is None:
        say("system", "（没有可用的记录员模型）", "system")
        return ""

    say("system", "📝 阿鱼开始整理这场讨论的会议记录……", "system")
    try:
        text = llm_call(me["base_url"], me["model"], me["key"], [
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
        with Path(_last_log_path).open("a", encoding="utf-8") as f:
            f.write("\n\n---\n\n# 📝 会议纪要（阿鱼整理）\n\n" + text.strip() + "\n")
    except Exception:
        pass
    say("ayu", text.strip(), "summary")      # ⭐ kind=summary：前端渲染成大卡片
    return text


def start_requested(resume: bool = False) -> None:
    """阿包在界面上点了「开场」→ 让待机中的场子立刻开（人数够的话）。

    ⚠️ 两件必须重做的事（都踩过）：
      1. **重新读配置** —— settings 是服务启动时读的，而阿包可能刚换了议题；
         不重读的话，顶栏显示新议题、讨论却还在聊旧的。
      2. **重开日志文件** —— 否则会出现"文件名叫议题 1、内容却是议题 5"（日志名是启动时定的）。

    ⭐ `resume=True` = 他点的是「继续讨论」（这条议题聊过）：把上一次的要点翻出来，
       塞进本场配置的 `recap` —— `RealTable.run()` 拿它当**公用历史框的起点**，
       于是 AI 们第 1 轮就"记得"上次聊到哪儿了（阿包：「要不然就不是继续聊，而是重新聊了」）。
    """
    global _start_settings, _log_path, _last_log_path
    _start_settings = load_settings()
    want = _start_settings["topic"]
    if _log_path is None or want[:18] not in str(_log_path.name):
        init_log(want)
    _last_log_path = _log_path       # ⭐ 记下来：阿包点「整理会议记录」时读它

    # ⭐ 前情提要：放在 init_log() **之后** —— 这样"没翻到"那条提示会写进本场日志，
    #    而不是写进上一场（那会把上一场的记录尾巴弄脏）
    _start_settings.pop("recap", None)
    _start_settings.pop("recap_from", None)
    if resume:
        # ⚠️ exclude=_log_path 不能省：本场日志刚刚才建出来，时间戳最新，
        #    不排除掉的话"上一次"翻到的是它自己（空的）
        recap, src = build_recap(want, exclude=_log_path)
        if recap:
            _start_settings["recap"], _start_settings["recap_from"] = recap, src
        else:
            say("system", "📜 想接着上次聊，但没翻到上一次的记录 —— 这一场只能从头来", "system")

    _stop_event.clear()              # ⭐ 新一场开始，清掉上一场的"结束"信号
    _start_event.set()


def idle_loop(roster: list, settings: dict) -> None:
    """安静待机：不自动开聊，只在阿包说话时让在场的人回应他。

    ⭐ 阿包 2026-10-09 的原话：「把自动回复先关了吧，要不然阿鱼一个人在里面其实挺寂寞的」——
       所以人没齐时**绝不自动发散**；但阿包一开口，在场的必须接话（否则他说话没人理，更寂寞）。
    """
    ready = [g for g in roster if g.get("ready")]
    names = "、".join(g["name"] for g in ready) or "（空场）"
    say("system", f"🍵 讨论会待机中 · 在场：{names}　就绪 {len(ready)}/{settings['min_guests']} 位", "system")
    say("system", f"本次议题：{settings['topic']}", "system")
    say("system", "点上方「🍵 开场 / 换议题」挑一个就能开始；也可以直接说话，在场的会回你", "system")

    while True:
        if _start_event.is_set() and len(ready) >= settings["min_guests"]:
            fresh = _start_settings or settings      # ⭐ 用按下开场那一刻的配置（议题可能是刚选的）
            say("system", f"阿包按了开场键 —— 议题：{fresh['topic']}", "system")
            RealTable(roster, fresh).run()
            return
        time.sleep(1.0)
        with _lock:
            pending = list(_boss_inbox)
            _boss_inbox.clear()
        if not pending:
            continue
        q = pending[-1]
        say("system", f"（回应阿包：「{q[:24]}」）", "system")
        for g in ready:
            try:
                t = llm_call(g["base_url"], g["model"], g["key"], [
                    {"role": "system", "content": (g.get("persona") or "") +
                     "\n\n现在是茶话会的待机时间，其他人还没到。阿包（人类，在旁听席）跟你说话，"
                     "请直接回应他，一到三句，别写小作文。"},
                    {"role": "user", "content": q},
                ])
            except Exception as e:
                say("system", f"（{g['name']} 掉线了：{type(e).__name__}: {str(e)[:80]}）", "system")
                continue
            say(g["who"], t)


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

    check(rough_tokens("你好世界") == 5, "token 粗估：4 个汉字 +1 = 5")
    check(rough_tokens("abcdefgh") == 3, "token 粗估：8 字符 /4 +1 = 3")

    cfg = load_settings()
    check(isinstance(cfg["rounds"], int) and cfg["rounds"] > 0, f"配置读取：rounds={cfg['rounds']}")
    roster = load_roster()
    ready = [g for g in roster if g.get("ready")]
    print(f"  嘉宾位 {len(roster)} 个，其中 key 已就绪 {len(ready)} 个"
          + (f"（{', '.join(g['name'] for g in ready)}）" if ready else "（还没填 key）"))
    check(len(roster) >= 5, "嘉宾名单至少 5 位")

    srv.shutdown()
    print(f"\nSELFTEST {'OK' if bad == 0 else 'FAIL'} 通过 {ok} 项 / 失败 {bad} 项")
    return 0 if bad == 0 else 1


# ── HTTP ──────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def end_headers(self):
        # ⭐ 2026-10-10：改了界面但浏览器一直拿缓存里的旧页（标题/弹窗都不更新），
        #    本地服务性能无所谓 —— 一律 no-store，改完刷新即生效。
        if not self.path.startswith("/avatars"):
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
                 "org": g.get("org", ""), "ready": bool(g.get("ready"))}
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
            body = json.dumps({"auto": _auto_next, "waiting": _next_waiting},
                              ensure_ascii=False).encode("utf-8")
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
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        global _chosen_topic_index, _rounds_override, _auto_next
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
                                     payload.get("reason") or "", payload.get("author") or "")
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
            ok = False
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
                ok = delete_topic(int(payload.get("index", -1)))
                if ok:
                    say("system", "🗑 已从议题库删除一个议题（topics.md 已更新）", "system")
            except Exception:
                ok = False
            body = json.dumps({"ok": ok}, ensure_ascii=False).encode("utf-8")
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
            if isinstance(payload.get("topic_index"), int):
                _chosen_topic_index = max(0, int(payload["topic_index"]))
                say("system", f"📌 阿包选定了议题 {_chosen_topic_index + 1}：{load_settings()['topic']}", "system")
            # ⭐ 预设轮数（议题框里那个小输入框）：有值就盖过 config 的 rounds
            _r = payload.get("rounds")
            if isinstance(_r, int) and not isinstance(_r, bool):
                _rounds_override = max(1, min(99, int(_r)))
                say("system", f"🔢 本场预设轮数：{_rounds_override} 轮", "system")
            start_requested(bool(payload.get("resume")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
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
        if text:
            with _lock:
                _boss_inbox.append(text)
            say("boss", text)
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

    threading.Thread(target=boot, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "真对话" if use_real else "demo"
    print(f"🍵 AI 茶话会 · http://127.0.0.1:{args.port}   模式={mode}   "
          f"就绪嘉宾={len(ready)}/{len(roster)}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
