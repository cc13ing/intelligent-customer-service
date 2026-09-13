"""饺子情绪识别：供后端写入 chat/WS 响应，前端切形象。"""

from __future__ import annotations

import re

# 与前端 JIAOZI_EMOTIONS 的 key 保持一致
EMOTION_KEYS = (
    "regret",
    "sorry",
    "happy",
    "thanks",
    "curious",
    "cheer",
    "listen",
)

_GREETING_RE = re.compile(
    r"^(?:"
    r"你好|您好|哈喽|嗨|嗨喽|"
    r"hello|hi|hey|howdy|"
    r"good\s+(?:morning|afternoon|evening)|"
    r"nihao|ni\s*hao|"
    r"你好啊|早上好|下午好|晚上好|"
    r"مرحبا|أهلا|السلام\s*عليكم"
    r")[\s!.?，,~！؟]*$",
    re.IGNORECASE,
)


def detect_jiaozi_emotion(
    question: str,
    *,
    tools_used: list[str] | None = None,
    answer: str | None = None,
) -> str:
    """根据用户问题、工具调用与回答内容推断饺子情绪。"""
    text = (question or "").strip()
    lower = text.lower()
    tools = tools_used or []

    # 工具优先：更贴近真实意图
    if "create_return_request" in tools:
        return "regret"
    if "record_user_complaint" in tools:
        return "sorry"
    if "query_my_orders" in tools or "query_order" in tools or "fetch_logistics_information" in tools:
        return "curious"

    if re.search(
        r"退货|退款|换货|退换|不想要|return|refund|exchange|money\s*back|"
        r"إرجاع|استرداد|استبدال",
        lower,
    ):
        return "regret"
    if re.search(
        r"投诉|态度差|欺诈|骗子|生气|愤怒|complaint|angry|terrible|awful|"
        r"شكوى|غاضب|سيء",
        lower,
    ):
        return "sorry"
    if re.search(r"谢谢|感谢|thanks|thank\s*you|xiexie|شكرا|مشكور", lower):
        return "thanks"
    if _GREETING_RE.match(text):
        return "happy"
    if re.search(
        r"订单|物流|运单|快递|tracking|order|shipment|到哪|"
        r"طلب|شحن|تتبع",
        lower,
    ):
        return "curious"
    if re.search(
        r"帮我|麻烦|可以吗|怎么办|怎么弄|please|help|"
        r"ساعد|مساعدة|من\s*فضلك",
        lower,
    ):
        return "cheer"

    # 回答侧弱信号（工具已跑完但问题本身中性）
    if answer:
        a = answer.lower()
        if re.search(r"遗憾|抱歉|对不起|sorry|unfortunately|آسف", a):
            return "sorry"
        if re.search(r"不客气|谢谢|thanks|pleasure|عفوا", a):
            return "thanks"

    return "listen"
