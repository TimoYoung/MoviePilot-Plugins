"""剧名、季号和豆瓣条目标题的对齐。"""

from __future__ import annotations

import re
from typing import Optional

from .models import Candidate, Subject

_CN = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_SEASON = re.compile(r"第\s*([0-9零一二三四五六七八九十]+)\s*季", re.IGNORECASE)
_SEASON_EN = re.compile(r"season\s*([0-9]+)", re.IGNORECASE)
_SPECIAL = re.compile(r"特别篇|specials?", re.IGNORECASE)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_DOUBAN_ID = re.compile(r"/(\d+)(?=/|$)")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[：:·\-—_()（）\[\]【】.。,，/]")


def parse_cn_number(text: str) -> Optional[int]:
    """解析中文季号。只接受不超过两位数的常见写法。"""
    if text.isdigit():
        return int(text)
    if text in _CN:
        return _CN[text]
    if "十" not in text:
        return None
    left, right = text.split("十", 1)
    tens = _CN.get(left, 1) if left else 1
    ones = _CN.get(right, 0) if right else 0
    if left and left not in _CN:
        return None
    if right and right not in _CN:
        return None
    return tens * 10 + ones


def parse_title(title: str) -> tuple[str, Optional[int], bool]:
    """从条目标题取出系列名和季号。

    返回 (系列名, 季号, 是否含混)。特别篇是第 0 季。
    同时出现两个不同季号时视为含混，调用方不能用它做唯一匹配。
    """
    found: list[int] = []
    cleaned = title or ""
    for pattern, special in ((_SEASON, False), (_SEASON_EN, False)):
        for match in pattern.finditer(cleaned):
            number = parse_cn_number(match.group(1))
            if number is not None:
                found.append(number)
        cleaned = pattern.sub(" ", cleaned)
    if _SPECIAL.search(title or ""):
        found.append(0)
        cleaned = _SPECIAL.sub(" ", cleaned)
    unique = list(dict.fromkeys(found))
    season = unique[0] if len(unique) == 1 else None
    return _SPACE.sub(" ", cleaned).strip(), season, len(unique) > 1


def chinese_key(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff]+", text or ""))


def latin_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def normalize_name(text: str) -> str:
    return _PUNCT.sub("", _SPACE.sub("", (text or "").lower()))


def names_match(left: str, right: str) -> bool:
    """中文主标题或英文主标题一致即视为同一部作品。"""
    left_name, _, _ = parse_title(left)
    right_name, _, _ = parse_title(right)
    if normalize_name(left_name) and normalize_name(left_name) == normalize_name(right_name):
        return True
    left_cn, right_cn = chinese_key(left_name), chinese_key(right_name)
    if left_cn and left_cn == right_cn:
        return True
    left_en, right_en = latin_key(left_name), latin_key(right_name)
    return len(left_en) >= 4 and left_en == right_en


def norm_imdb(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("tt"):
        return text
    if text.isdigit():
        return f"tt{text}"
    return text


def years_compatible(left: str, right: str, *, slack: int = 1) -> bool:
    """年份兼容：缺一即可；两边都有时允许 ±slack（常见上映年/公映年偏差）。"""
    if not left or not right:
        return True
    try:
        return abs(int(left) - int(right)) <= max(int(slack), 0)
    except (TypeError, ValueError):
        return str(left) == str(right)


def extract_douban_id(link: str) -> str:
    """按豆瓣榜单订阅的规则，从链接路径里取豆瓣数字 id。"""
    match = _DOUBAN_ID.search(link or "")
    if not match or not match.group(1).isdigit():
        return ""
    return match.group(1)


def extract_year(text: str) -> str:
    match = _YEAR.search(text or "")
    return match.group(1) if match else ""


def season_numbers(text: str) -> set[int]:
    """解析转移记录里的季范围。S01-S03 展开为 1、2、3。"""
    raw = str(text or "")
    numbers = [int(item) for item in re.findall(r"(\d+)", raw)]
    if not numbers:
        return set()
    if re.search(r"[-~到至]", raw) and len(numbers) >= 2:
        start, end = numbers[0], numbers[-1]
        if end < start:
            start, end = end, start
        return set(range(start, end + 1))
    return set(numbers)


def subjects_match(candidate: Candidate, subject: Subject, *, trust_douban_id: bool) -> bool:
    """判断媒体库候选和豆瓣条目是不是同一部电影或同一季。

    优先级：豆瓣 ID（含 IMDb 桥接回填）→ 双方 IMDb → 标题+年份（±1）。
    trust_douban_id 仍用于白名单直填：有豆瓣 ID 时不再要求年份相容。
    """
    if subject.ambiguous:
        return False
    same_douban = bool(candidate.douban and subject.douban_id and candidate.douban == subject.douban_id)
    imdb_left, imdb_right = norm_imdb(candidate.imdb), norm_imdb(subject.imdb)
    same_imdb = bool(imdb_left and imdb_right and imdb_left == imdb_right)
    if imdb_left and imdb_right and not same_imdb and not (trust_douban_id and same_douban):
        return False
    if candidate.kind == "movie":
        if subject.season not in (None,):
            return False
        # 豆瓣数字 ID / IMDb 是权威身份，不再用年份否决
        if same_douban:
            return True
        if same_imdb:
            return True
        return names_match(candidate.title, subject.title) and years_compatible(candidate.year, subject.year)
    if candidate.season is None:
        return False
    if subject.season is None:
        season_ok = candidate.season == 1
    else:
        season_ok = candidate.season == subject.season
    if not season_ok:
        return False
    if same_douban and (trust_douban_id or subject.season is not None or candidate.season == 1):
        return True
    if same_imdb:
        return True
    return names_match(candidate.title, subject.title)


def matching_subjects(candidate: Candidate, subjects: list[Subject], *, trust_douban_id: bool) -> list[Subject]:
    return [subject for subject in subjects if subjects_match(candidate, subject, trust_douban_id=trust_douban_id)]
