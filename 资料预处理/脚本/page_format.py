# -*- coding: utf-8 -*-
"""页码格式解析库（#27 M1）。提供多格式页码的确定性解析器。

支持格式：纯数字、装饰数字（- 8 -）、罗马数字、第N页、Page N、N/M、中文数字。
每个解析器输入字符串，输出解析后的整数页码或 None。
主入口 parse_page_number(content, format_type=None)：未指定格式时按优先级尝试所有格式。
"""

import re

FORMAT_PLAIN = "纯数字"
FORMAT_DECORATED = "装饰数字"
FORMAT_ROMAN = "罗马数字"
FORMAT_CN_PAGE = "第N页"
FORMAT_EN_PAGE = "PageN"
FORMAT_FRACTION = "N/M"
FORMAT_CN_NUM = "中文数字"
FORMAT_OTHER = "其它"

_ALL_FORMATS = [
    FORMAT_PLAIN,
    FORMAT_DECORATED,
    FORMAT_ROMAN,
    FORMAT_CN_PAGE,
    FORMAT_EN_PAGE,
    FORMAT_FRACTION,
    FORMAT_CN_NUM,
]


def _parse_plain(s: str):
    if not re.fullmatch(r"\d+", s):
        return None
    return int(s)


def _parse_decorated(s: str):
    m = re.fullmatch(r"[\s\-—_＝=·*]*(\d+)[\s\-—_＝=·*]*", s)
    if not m:
        return None
    return int(m.group(1))


def _parse_roman(s: str):
    roman_map = {
        'i': 1, 'v': 5, 'x': 10, 'l': 50,
        'c': 100, 'd': 500, 'm': 1000
    }
    s_lower = s.lower().strip()
    if not s_lower or not all(c in roman_map for c in s_lower):
        return None
    values = [roman_map[c] for c in s_lower]
    total = 0
    for i, v in enumerate(values):
        if i + 1 < len(values) and values[i + 1] > v:
            total -= v
        else:
            total += v
    return total if 1 <= total <= 2000 else None


def _parse_cn_page(s: str):
    m = re.fullmatch(r"第\s*(\d+)\s*页", s)
    if not m:
        return None
    return int(m.group(1))


def _parse_en_page(s: str):
    m = re.fullmatch(r"[Pp][Aa][Gg][Ee]\s*(\d+)", s)
    if not m:
        return None
    return int(m.group(1))


def _parse_fraction(s: str):
    m = re.fullmatch(r"(\d+)\s*/\s*\d+", s)
    if not m:
        return None
    return int(m.group(1))


def _parse_cn_num(s: str):
    cn_digits = {
        '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
        '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
        '十': 10, '百': 100, '千': 1000,
        '廿': 20, '卅': 30, '卌': 40,
    }
    s = s.strip()
    if not s:
        return None
    if s.startswith('廿'):
        if len(s) == 1:
            return 20
        rest = cn_digits.get(s[1])
        if rest is not None and rest < 10:
            return 20 + rest
        return None
    if s.startswith('卅'):
        if len(s) == 1:
            return 30
        rest = cn_digits.get(s[1])
        if rest is not None and rest < 10:
            return 30 + rest
        return None
    if s.startswith('卌'):
        if len(s) == 1:
            return 40
        rest = cn_digits.get(s[1])
        if rest is not None and rest < 10:
            return 40 + rest
        return None
    total = 0
    current = 0
    for c in s:
        v = cn_digits.get(c)
        if v is None:
            return None
        if v >= 10:
            if current == 0:
                current = 1
            total += current * v
            current = 0
        else:
            current = current * 10 + v
    total += current
    return total if 1 <= total <= 2000 else None


_PARSERS = {
    FORMAT_PLAIN: _parse_plain,
    FORMAT_DECORATED: _parse_decorated,
    FORMAT_ROMAN: _parse_roman,
    FORMAT_CN_PAGE: _parse_cn_page,
    FORMAT_EN_PAGE: _parse_en_page,
    FORMAT_FRACTION: _parse_fraction,
    FORMAT_CN_NUM: _parse_cn_num,
}


def parse_page_number(content: str, format_type: str = None):
    """解析页码：输入字符串，输出整数页码或 None。

    format_type 为 None 时按优先级尝试所有格式；指定时用对应解析器。
    解析结果须在 [1, 2000] 范围内，否则返回 None。
    """
    s = content.strip()
    if not s:
        return None

    formats_to_try = [format_type] if format_type else _ALL_FORMATS
    for fmt in formats_to_try:
        parser = _PARSERS.get(fmt)
        if not parser:
            continue
        result = parser(s)
        if result is not None and 1 <= result <= 2000:
            return result
    return None


def detect_format(content: str):
    """检测页码格式类型：返回格式枚举值或 None。"""
    s = content.strip()
    if not s:
        return None
    for fmt in _ALL_FORMATS:
        parser = _PARSERS[fmt]
        result = parser(s)
        if result is not None and 1 <= result <= 2000:
            return fmt
    return None


def _selfcheck():
    """page_format 自检：覆盖全部 7 种格式 + 边界值。"""
    print("page_format 自检")
    ok = True

    cases = [
        ("8", FORMAT_PLAIN, 8),
        ("- 8 -", FORMAT_DECORATED, 8),
        ("= 123 =", FORMAT_DECORATED, 123),
        ("iv", FORMAT_ROMAN, 4),
        ("XII", FORMAT_ROMAN, 12),
        ("第 8 页", FORMAT_CN_PAGE, 8),
        ("第8页", FORMAT_CN_PAGE, 8),
        ("Page 8", FORMAT_EN_PAGE, 8),
        ("page 42", FORMAT_EN_PAGE, 42),
        ("8 / 503", FORMAT_FRACTION, 8),
        ("8/503", FORMAT_FRACTION, 8),
        ("八", FORMAT_CN_NUM, 8),
        ("廿三", FORMAT_CN_NUM, 23),
        ("一百零五", FORMAT_CN_NUM, 105),
        ("2000", FORMAT_PLAIN, 2000),
        ("2024", None, None),
        ("2025", None, None),
        ("第 8 章", None, None),
        ("8 个方法", None, None),
        ("abc", None, None),
        ("", None, None),
    ]

    for text, expected_fmt, expected_val in cases:
        val = parse_page_number(text)
        fmt = detect_format(text)
        if expected_val is None:
            if val is not None:
                print(f"  ❌ 应拒收却解析成功：{text!r} → {val}")
                ok = False
        else:
            if val != expected_val:
                print(f"  ❌ 解析值不符：{text!r} → {val}，期望 {expected_val}")
                ok = False
            if expected_fmt and fmt != expected_fmt:
                print(f"  ❌ 格式检测不符：{text!r} → {fmt}，期望 {expected_fmt}")
                ok = False

    if ok:
        print("  page_format 自检：7 种格式 + 边界值全部通过 ✅")
    else:
        print("  ❌ 自检失败")
    return ok


if __name__ == "__main__":
    import sys
    if "--自检" in sys.argv:
        sys.exit(0 if _selfcheck() else 1)
    else:
        print("page_format.py 为格式解析库：请用 --自检 运行，或由 page_fit 调用")
        sys.exit(0)
