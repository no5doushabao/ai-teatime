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


_chosen_topic_index = 0          # ⭐ 阿包在界面上选的议题（0 = 第一个）


def parse_topics() -> list:
    """从 `topics.md` 读议题 —— ⭐ 这是议题的唯一来源（阿包手动维护的文件）。

    格式：一行 `## 标题`，下面跟正文（可多行）。
    ⚠️ 这文件以后是阿包手动编辑的，解析必须宽松：认不出的内容忽略，绝不抛异常。
    """
    path = ROOT / "topics.md"
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
        t["body"] = t["body"].strip()
    return out


# ── 讨论历史（⭐ 议题库要显示"讨论过几轮、谁参与的"）──────────
_history_path = ROOT / "debate-history.json"


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


def load_settings() -> dict:
    cfg = load_json(ROOT / "config.local.json") or load_json(ROOT / "config.example.json")
    d = cfg.get("debate") or {}
    md = parse_topics()
    if md:                                     # ⭐ 优先用 topics.md
        topics = [t["title"] for t in md]
        bodies = {t["title"]: t["body"] for t in md}
    else:                                      # 兜底：config 里的 topics
        topics = [t for t in (d.get("topics") or []) if t] or [d.get("topic") or "随便聊聊。"]
        bodies = {}
    idx = max(0, min(_chosen_topic_index, len(topics) - 1))
    topic = topics[idx]
    return {
        "rounds": int(cfg.get("rounds", 12)),
        "budget_cny": float(cfg.get("budget_cny", 5.0)),
        "gap": cfg.get("gap_seconds", [2, 5]),
        # ⭐ 至少几位就绪才够开场；不够就安静待机（阿包原话："别让阿鱼一个人在里面，挺寂寞的"）
        "min_guests": int(cfg.get("min_guests", 3)),
        # ⭐ 议题讨论会（2026-10-10 改版：从"闲聊"改成"议题讨论"）
        "topic": topic,
        "topic_body": bodies.get(topic, ""),
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
        return (data["choices"][0]["message"]["content"] or "").strip()

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
        try:
            out = _post(_make(st))
            _PARAM_STYLE[model] = st
            return out
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            low = detail.lower()
            last = urllib.error.HTTPError(
                e.url, e.code, f"{e.reason} | {detail[:200]}", e.headers, None)
            # 只对"参数不被支持"继续换组合；其它错误（额度/key/超时）立刻抛，别掩盖真因
            if e.code == 400 and ("unsupported" in low or "not supported" in low
                                  or "unknown parameter" in low):
                continue
            raise last
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
        text = llm_call(guest["base_url"], guest["model"], guest["key"], messages,
                        max_tokens=s.get("max_tokens") or None)
        # ⭐ 2026-10-10：推理模型（GPT-5 等）的"思考 token"会吃光 max_tokens → 返回空串。
        #    以前这种空回复不报错，界面上就成了"卡住不出声"。现在：重试一次更大预算 + 兜底提示。
        if not (text or "").strip():
            try:
                text = llm_call(guest["base_url"], guest["model"], guest["key"], messages,
                                max_tokens=int(s.get("max_tokens", 1200)) * 3)
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

    def run(self) -> None:
        s = self.settings
        names = "、".join(g["name"] for g in self.roster)
        say("system", f"📋 议题讨论会开始 · 圆桌制（{len(self.roster)} 位：{names}）", "system")
        say("system", f"本次议题：{s['topic']}", "system")
        say("system", f"规则：每轮每人各发言一次，每次至少 {s['min_chars']} 字；"
                      f"发言前须读完本轮已发言的全部内容", "system")
        self._hist_begin()             # ⭐ 记一笔：议题 / 参与者 / 计划轮数

        # ⭐ 阿鱼当主持：开场白由真模型生成（不是固定台词）—— 宣布议题、讲规则、引出第一个争点
        host = next((g for g in self.roster if g["who"] == "ayu"), None)
        if host:
            try:
                intro = llm_call(host["base_url"], host["model"], host["key"], [
                    {"role": "system", "content": (host.get("persona") or "") +
                     "\n\n你是这场讨论会的主持人。请用 150~300 字开场：① 宣布今晚的议题；"
                     "② 说清规则（每轮每人发言一次、要有立场和论证、必须回应他人）；"
                     "③ 点出一个最值得先争的地方，然后把话头交给第一位发言者。"
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
                continue

            shift = (r - 1) % len(self.roster)
            order = self.roster[shift:] + self.roster[:shift]      # 起点轮转
            for g in order:
                if self.over_budget:
                    say("system", "💸 触到预算上限，先散会 —— 想继续就把 budget_cny 调大", "system")
                    self._hist_progress(r - 1, finished=True)
                    return
                time.sleep(random.uniform(*s["gap"]))
                try:
                    t = self.ask(g, "轮到你发言了。请给出你的立场和论证。")
                except Exception as e:
                    say("system", f"（{g['name']} 掉线了：{type(e).__name__}: {str(e)[:80]}）", "system")
                    continue
                say(g["who"], t)
                self._record(g, r, t)

            self._write_notes(r)           # ⭐ 每轮结束：记录员整理进公用历史框

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


def start_requested() -> None:
    """阿包在界面上点了「开场」→ 让待机中的场子立刻开（人数够的话）。

    ⚠️ 关键：这里必须**重新读一次配置**。settings 是服务启动时读的，而阿包可能在
       界面上换了议题 —— 不重读的话，顶栏显示新议题、讨论却还在聊旧议题（踩过）。
    """
    global _start_settings
    _start_settings = load_settings()
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
    say("system", "点左下角「🍵 开场」开始讨论；也可以直接说话，在场的会回你", "system")

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
        if self.path == "/topics":                 # ⭐ 给界面弹议题选择框用
            s = load_settings()
            ready_n = sum(1 for g in load_roster() if g.get("ready"))
            body = json.dumps({
                "topics": [{"index": i, "title": t} for i, t in enumerate(s["topics"])],
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
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        global _chosen_topic_index
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
            start_requested()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
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
        #    双击启动器只是"支起场子"，想讨论时点界面左下角的「🍵 开场」。
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
