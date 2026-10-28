"""乒乓循环护栏：判定「对面也是机器人」，是就闭嘴。

为什么必须在网关侧拦：两个自动回复互相 @ 会形成无界对刷——本机实测某群与另一个
bot 连续互道「晚安」40+ 轮、每轮约 55 秒，直到人工叫停；核心每轮还要跑两版模型
择优，纯烧钱。这里在进核心之前用确定性信号掐断，并记住静默期（落盘），网关重启后依然生效。

判定信号（全部确定性规则，不赌模型判断）：
1. 道别复读：15 分钟内 ≥4 条收尾语（晚安/睡/下线/装死…）且 ≥3 条在回我们、平均间隔 <150s；
   没看到引用时阈值升到 6 条 / 平均间隔 <120s
2. 回声复读：连续 3 条与我们上一句高度重合（bigram 包含率 ≥0.35）
3. 问题连发：10 分钟内 ≥6 条疑问句且平均间隔 <30s，或 ≥10 条且平均间隔 <60s
   （群消息必然 @ 了我们，连续追问就是冲我们来的——用户明确要求把这种判成机器人）
4. 秒回连击：连续 ≥12 条在回我们、都在我们发言后 15s 内回，
   平均间隔 ≤8s、平均长度 ≤40 字（真人打字快也难连续 12 次引用式秒回）
5. 昵称即机器人：昵称命中 bot/机器人/自动回复 等，且已连发 3 条
6. 极短连击：连续 3 条去掉标点后只剩 ≤1 个字（「。」「……」「嗯」——
   对刷到词穷时就是靠单字符拖着不散场）

刻意不做「按总轮数一刀切」：真人也能连聊几十轮，按轮数误杀太伤。
秒回连击要求「正在回我们」（引用我们的 message_id）；问题连发不要求引用，
因为群消息进网关必然已经 @ 了我们。

命中后按 (渠道, 会话, 对方) 静默 30 分钟；同一对象 2 小时内再次触发则翻倍，最长 4 小时。
主人账号（AUTHORIZED_QQ）不进本护栏——主人在群里连发晚安也不会被静音。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

_CLOSING_RE = re.compile(
    r"晚安|安睡|好梦|睡吧|睡了|闭眼|装死|下线|不送|再见|拜拜|明早|明天见|早点睡|散了吧|梦里"
)
_QUESTION_RE = re.compile(r"[?？]\s*$|(吗|呢|么|啥|怎么|为什么|哪儿|谁|多少)\s*[?？]?\s*$")
_BOT_NAME_RE = re.compile(
    r"(bot|机器人|自动回复|智能助手|ai助手|chatgpt|gpt-?\d|deepseek|claude|kimi|豆包|文心|通义|小冰)",
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(r"\[引用:([0-9A-Za-z_-]+)\]")
_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")

DEFAULT_MUTE_S = 1800.0
MAX_MUTE_S = 4 * 3600.0
WINDOW_CLOSING = 900.0
WINDOW_QUESTION = 600.0
WINDOW_TURNS = 1200.0
WINDOW_RAPID = 12
MAX_TRACKED_CHATS = 500


@dataclass
class Verdict:
    muted: bool
    reason: str = ""
    until: float = 0.0


@dataclass
class _PeerState:
    closing: list[tuple[float, bool]] = field(default_factory=list)
    echo: list[float] = field(default_factory=list)
    questions: list[tuple[float, bool]] = field(default_factory=list)
    turns: list[tuple[float, bool, int]] = field(default_factory=list)
    rapid: list[tuple[float, int]] = field(default_factory=list)
    trivial: int = 0


def _bigrams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", _CQ_RE.sub("", text or ""))
    return {s[i:i + 2] for i in range(len(s) - 1)}


def containment(text: str, ref: str) -> float:
    """text 的二元组有多少落在 ref 里（“回声”程度）。ref 为空返回 0。"""
    a, b = _bigrams(text), _bigrams(ref)
    if not b:
        return 0.0
    return len(a & b) / len(b)


def is_closing(text: str) -> bool:
    return bool(_CLOSING_RE.search(text or ""))


def is_question(text: str) -> bool:
    return bool(_QUESTION_RE.search((text or "").strip()))


def is_trivial(text: str) -> bool:
    """纯标点/单字消息：「。」「……」「嗯」「哦」「哈」。

    单条毫无问题，但两个 bot 对刷到最后就是靠单字符拖着不散场
    （实测连环群以「。」「……」「。」三条收尾）。判定只在「连续多条」时才用。
    两个字的日常话（「你好」「晚安」「在吗」）不算——那是正常聊天。
    """
    s = re.sub(r"\[引用:[^\]]*\]|\[CQ:[^\]]*\]", "", text or "")
    s = re.sub(r"[\s。，、！？…~～.,!?;；:：'\"“”‘’()（）\[\]【】<>《》\-—_*#]+", "", s)
    return len(s) <= 1


def is_bot_name(name: str) -> bool:
    return bool(_BOT_NAME_RE.search(name or ""))


def _mean_gap(stamps: list[tuple]) -> float:
    if len(stamps) < 2:
        return float("inf")
    return statistics.mean(b[0] - a[0] for a, b in zip(stamps, stamps[1:]))


def _trim(stamps: list[tuple], now: float, window: float) -> None:
    while stamps and now - stamps[0][0] > window:
        stamps.pop(0)


class LoopGuard:
    """有状态的乒乓循环护栏。网关是单事件循环，无需加锁。"""

    def __init__(self, path: str | Path | None = None, *, mute_s: float = DEFAULT_MUTE_S,
                 now=time.time):
        self._path = Path(path) if path else None
        self._mute_s = max(60.0, float(mute_s))
        self._now = now
        self._state: dict[str, _PeerState] = {}
        self._mutes: dict[str, dict] = {}
        # 触发史与静默期分开存：静默到期后 _mutes 会被清掉，但「这是个惯犯」要记住，
        # 否则同一对象每半小时都能重新刷满一轮再被静音，等于没有升级。
        self._history: dict[str, dict] = {}
        self._last_reply: dict[str, str] = {}
        self._last_out_at: dict[str, float] = {}
        self._own_ids: dict[str, OrderedDict[str, bool]] = {}
        self._load()

    # ---------------- 对外 ----------------

    def inspect(self, *, channel: str, chat_id: str, peer_id: str,
                text: str, name: str = "", is_group: bool = True) -> Verdict:
        """看一条入站消息；返回 muted=True 表示这条不该回。

        群聊才做「对刷」类判定；私聊只保留「昵称就是机器人」这一条——
        私人秘书场景下主人连问十个问题很常见，不能因此静音。
        """
        now = self._now()
        chat_key = f"{channel}:{chat_id}"
        key = f"{chat_key}:{peer_id}"
        self._prune_mutes(now)

        mute = self._mutes.get(key)
        if mute and float(mute.get("until", 0)) > now:
            mute["drops"] = int(mute.get("drops", 0)) + 1
            return Verdict(True, str(mute.get("reason", "muted")), float(mute["until"]))
        if mute:
            self._mutes.pop(key, None)
            self._save()

        if len(self._state) > MAX_TRACKED_CHATS:
            self._state.pop(next(iter(self._state)))
        st = self._state.setdefault(key, _PeerState())

        body = (text or "").strip()
        quote = _QUOTE_RE.search(body)
        reply_self = bool(quote and quote.group(1) in self._own_ids.get(chat_key, {}))
        prev = self._last_reply.get(chat_key, "")
        echo = bool(prev) and len(body) >= 10 and containment(body, prev) >= 0.35
        bot_name = is_bot_name(name)
        latency = now - self._last_out_at[chat_key] if chat_key in self._last_out_at else None

        item = (now, reply_self)
        if is_closing(body):
            st.closing.append(item)
        else:
            st.closing.clear()
        _trim(st.closing, now, WINDOW_CLOSING)

        if echo:
            st.echo.append(now)
        else:
            st.echo.clear()
        while st.echo and now - st.echo[0] > WINDOW_CLOSING:
            st.echo.pop(0)

        if is_question(body):
            st.questions.append(item)
        else:
            st.questions.clear()
        _trim(st.questions, now, WINDOW_QUESTION)

        st.turns.append((now, reply_self, len(body)))
        _trim(st.turns, now, WINDOW_TURNS)

        if latency is not None and 0 <= latency <= 15 and reply_self:
            st.rapid.append((now, len(body)))
        else:
            st.rapid.clear()
        _trim(st.rapid, now, WINDOW_TURNS)

        # 极短消息连击：两个 bot 对刷到词穷时全靠「。」「……」拖着不散场。
        # 只数「连续」的：真人偶尔发个「嗯」不该被记。
        st.trivial = st.trivial + 1 if is_trivial(body) else 0

        closing_replied = sum(1 for _, r in st.closing if r)
        if is_group and len(st.closing) >= 4 and closing_replied >= 3 \
                and _mean_gap(st.closing) < 150:
            return self._mute(key, "closing_loop", now)
        if is_group and len(st.closing) >= 6 and _mean_gap(st.closing) < 120:
            return self._mute(key, "closing_loop", now)
        if is_group and len(st.echo) >= 3:
            return self._mute(key, "echo_loop", now)
        if is_group:
            q_gap = _mean_gap(st.questions)
            if len(st.questions) >= 6 and q_gap < 30:
                # 群消息进到网关必然已经 @ 了我们（to_me），连续追问就是冲我们来的。
                # 用户明确要求：对面连续问问题 → 判成机器人 → 不回复。
                return self._mute(key, "question_flood", now)
            if len(st.questions) >= 10 and q_gap < 60:
                return self._mute(key, "question_flood", now)
        if is_group and len(st.rapid) >= WINDOW_RAPID:
            gaps = [b[0] - a[0] for a, b in zip(st.rapid, st.rapid[1:])]
            lengths = [n for _, n in st.rapid]
            # 门槛压得很紧：真人打字快也能到 8 秒一条，但连续 12 条都「引用我们」
            # 且平均 40 字以内、间隔 8 秒内——人手点 12 次引用回复做不到。
            if gaps and statistics.mean(gaps) <= 8 \
                    and statistics.mean(lengths) <= 40:
                return self._mute(key, "rapid_pingpong", now)
        if is_group and st.trivial >= 3:
            return self._mute(key, "trivial_loop", now)
        if bot_name and len(st.turns) >= 3:
            return self._mute(key, "bot_name", now)
        return Verdict(False)

    def record_outgoing(self, chat_key: str, message_id: str | None, text: str) -> None:
        """记下我们自己发出的消息（供回复自 / 回声 / 秒回判定）。"""
        self._last_out_at[chat_key] = self._now()
        if message_id:
            ids = self._own_ids.setdefault(chat_key, OrderedDict())
            ids[str(message_id)] = True
            while len(ids) > 200:
                ids.popitem(last=False)
            if len(self._own_ids) > MAX_TRACKED_CHATS:
                self._own_ids.pop(next(iter(self._own_ids)))
        if text:
            self._last_reply[chat_key] = text

    def mutes(self) -> list[dict]:
        now = self._now()
        self._prune_mutes(now)
        out = []
        for k, v in sorted(self._mutes.items()):
            chat, peer = k.rsplit(":", 1)
            out.append({"chat": chat, "peer": peer, "reason": v.get("reason", ""),
                        "until": v.get("until", 0.0),
                        "remaining_s": int(max(0.0, float(v.get("until", 0)) - now)),
                        "drops": int(v.get("drops", 0))})
        return out

    def unmute(self, chat: str, peer: str) -> bool:
        """chat 形如 qq_group:123456789。"""
        key = f"{chat}:{peer}"
        found = self._mutes.pop(key, None) is not None
        self._state.pop(key, None)
        if found:
            self._save()
        return found

    def mute(self, chat: str, peer: str, reason: str = "manual",
             until: float | None = None) -> None:
        """外部直接下静默（止损/运维用）。"""
        now = self._now()
        self._mutes[f"{chat}:{peer}"] = {
            "until": until or (now + self._mute_s), "reason": reason,
            "hits": 1, "drops": 0, "at": now,
        }
        self._save()

    # ---------------- 内部 ----------------

    def _mute(self, key: str, reason: str, now: float) -> Verdict:
        prev = self._history.get(key) or {}
        hits = int(prev.get("hits", 0)) + 1 if now - float(prev.get("at", 0)) < 7200 else 1
        duration = min(MAX_MUTE_S, self._mute_s * (2 ** min(hits - 1, 3)))
        self._history[key] = {"hits": hits, "at": now}
        self._mutes[key] = {"until": now + duration, "reason": reason, "hits": hits,
                            "drops": int((self._mutes.get(key) or {}).get("drops", 0)), "at": now}
        self._state.pop(key, None)
        self._save()
        return Verdict(True, reason, now + duration)

    def _prune_mutes(self, now: float) -> None:
        expired = [k for k, v in self._mutes.items() if float(v.get("until", 0)) <= now]
        for k in expired:
            self._mutes.pop(k, None)
        if expired:
            self._save()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return
        mutes = data.get("mutes")
        if isinstance(mutes, dict):
            self._mutes = {k: v for k, v in mutes.items() if isinstance(v, dict)}
            self._prune_mutes(self._now())
        history = data.get("history")
        if isinstance(history, dict):
            self._history = {k: v for k, v in history.items() if isinstance(v, dict)}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps({"mutes": self._mutes, "history": self._history},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass
