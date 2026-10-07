"""相似意见归并所用的文本相似度。

面向中文短文本：归一化后取字符 bigram 集合，使用 min 归一化重合度
（containment），对“同一意见的不同措辞/截长补短”更稳健；
同时保留 Jaccard 供需要更严格口径的调用方使用。
"""
from __future__ import annotations

import re

_KEEP = re.compile(r"[0-9a-zA-Z一-鿿]+")


def normalize(text: str) -> str:
    """小写化并剔除空白与标点，仅保留字母、数字与 CJK 字符。"""
    return "".join(_KEEP.findall(text.lower()))


def ngrams(text: str, n: int = 2) -> set[str]:
    tokens = normalize(text)
    if not tokens:
        return set()
    if len(tokens) <= n:
        return {tokens}
    return {tokens[i : i + n] for i in range(len(tokens) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set[str], b: set[str]) -> float:
    """|A∩B| / min(|A|,|B|)：短文本被长文本包含时得分高。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def text_similarity(x: str, y: str) -> float:
    """归并主口径：bigram containment；完全一致归一化文本直接得 1。"""
    nx, ny = normalize(x), normalize(y)
    if not nx or not ny:
        return 0.0
    if nx == ny:
        return 1.0
    return containment(ngrams(x), ngrams(y))
