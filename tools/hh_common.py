"""Shared helpers for the hh.ru pipeline tools.

filter_automation.py, match_resume.py and check_letter.py each grew their own
sentence splitter and JSON loader. They drifted: one split on newlines and one
did not, which changed whether a requirement written as a bullet was seen at
all. Keeping them here means a fix lands once.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

# Postings mix real punctuation with bullets and bare newlines; all three are
# item separators in practice.
_SPLIT = re.compile(r"(?<=[.;!?\n])\s+|•|—\s|\n")

# Requirement modality. A line is a hard requirement, an explicit nice-to-have,
# or neither — and "neither" is not the same as "optional", which is why the
# three-way split exists instead of a boolean.
MANDATORY = re.compile(
    r"обязательн|требуется|необходим|must have|требован|мы ожидаем|"
    r"без этого никак|строго|обязателен",
    re.I,
)
OPTIONAL = re.compile(
    r"будет плюсом|плюсом будет|как плюс|приветствуется|желательн|не обязательн|"
    r"будет преимуществом|как преимущество|готовность\s+(?:изучать|развиваться|расти)|"
    r"хотя бы базов|базовое понимание|начальн\w+ (?:опыт|знани)|по желанию|"
    r"nice to have|как бонус",
    re.I,
)


def split_sentences(text: str) -> List[str]:
    """Split a posting body into comparable fragments."""
    return [part.strip() for part in _SPLIT.split(text or "") if part.strip()]


def modality(sentence: str) -> str:
    """Classify one line: 'optional', 'mandatory' or 'neutral'.

    OPTIONAL wins over MANDATORY: "в требованиях будет плюсом опыт X" is a
    nice-to-have living inside a requirements block, not a hard requirement.
    """
    if OPTIONAL.search(sentence):
        return "optional"
    if MANDATORY.search(sentence):
        return "mandatory"
    return "neutral"


def load_rows(path: str) -> List[Dict[str, Any]]:
    """Load a JSON array, or the first array found in a JSON object."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    for value in data.values():
        if isinstance(value, list):
            return value
    raise ValueError(f"{path}: no array of records found")


def load_descriptions(path: str | None) -> Dict[str, str]:
    """Map vacancy_id -> description text, skipping records without one."""
    if not path:
        return {}
    return {
        str(row["vacancy_id"]): row["description_text"]
        for row in load_rows(path)
        if row.get("description_text")
    }


def load_skills(path: str | None) -> Dict[str, List[str]]:
    """Map vacancy_id -> key skills as listed by hh."""
    if not path:
        return {}
    return {str(row["vacancy_id"]): row.get("key_skills") or [] for row in load_rows(path)}
