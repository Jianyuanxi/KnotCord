"""token 估算：汉字 1 token/字，其他字符 1 token/3.5 字，再加 10%。"""

from __future__ import annotations

import math
import re

_HAN_RE = re.compile("[㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f]")


def estimate(text: str) -> int:
    if not text:
        return 0
    han = len(_HAN_RE.findall(text))
    other = len(text) - han
    return math.ceil(round((han + other / 3.5) * 1.1, 6))  # round 去掉 110.00000000000001 这类浮点误差
