"""Validate an hh.ru cover letter against the agreed house rules.

On hh.ru a cover letter is plain text pasted into the response box, not a PDF,
so the checks are about the text itself: length, paragraph structure, no
gender-marked past tense, and no claim that is absent from the CV.

The keyword check is advisory — it reports which vacancy terms the letter
reuses, so a letter that quietly drifts away from the posting is visible.

Usage:
    python3 tools/check_letter.py cover_letters/*.txt
    python3 tools/check_letter.py letter.txt --vacancy <id> --descriptions <file>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

MIN_CHARS, MAX_CHARS = 700, 1100
PARAGRAPHS = 3
MIN_PARAGRAPH = 140

# "работал(а)", "привык(ла)" and "готов/а" read as a form the writer could not
# commit to; the agreed style rewrites the sentence instead.
PARENTHETICAL = re.compile(r"[А-Яа-яЁё-]+\([А-Яа-яЁё-]{1,3}\)")
SLASHED = re.compile(r"\b[А-Яа-яЁё-]+/(?:а|я|ла|на)\b", re.I)

# First-person past tense carries grammatical gender in Russian. The agreed
# style avoids it entirely rather than picking a form.
#
# Matching every -ла/-лась ending flagged "сначала", "начала" and "была" — an
# adverb, a noun and a copula agreeing with "работа". Only CV action verbs are
# listed, which is narrower but does not cry wolf.
GENDERED_PAST = re.compile(
    r"\b(?:работа|тестирова|занима|участвова|отвеча|разрабатыва|поддержива|"
    r"проводи|создава|внедря|писа|вела|выполня|обнаружива|взаимодействова|"
    r"анализирова|провери|составля|оформля|локализова|сопровожда|готови|"
    r"использова|применя|дела|реша|помога|стро|веду?ща)"
    r"(?:ла|лась|лись)\b",
    re.I,
)

FORBIDDEN = {
    "контакты": re.compile(r"@|\+7|\bтел\b|telegram|whatsapp", re.I),
    "зарплатные ожидания": re.compile(r"зарплат|оклад|вилк|рублей в месяц|ожидания по деньг", re.I),
    "тема письма": re.compile(r"^тема:", re.I | re.M),
}


def check(text: str, vacancy_text: Optional[str] = None) -> List[str]:
    issues: List[str] = []
    body = text.strip()

    length = len(body)
    if not MIN_CHARS <= length <= MAX_CHARS:
        issues.append(f"длина {length} вне диапазона {MIN_CHARS}–{MAX_CHARS}")

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if len(paragraphs) != PARAGRAPHS:
        issues.append(f"абзацев {len(paragraphs)}, нужно {PARAGRAPHS}")
    short = [i + 1 for i, p in enumerate(paragraphs) if len(p) < MIN_PARAGRAPH]
    if short:
        issues.append(f"слишком короткие абзацы: {short} (приветствие не должно стоять отдельно)")

    if PARENTHETICAL.search(body):
        issues.append("родовые окончания в скобках — запрещены")
    if SLASHED.search(body):
        issues.append("родовые окончания через слэш — запрещены")
    for match in GENDERED_PAST.finditer(body):
        issues.append(f"прошедшее время с родовым окончанием: «{match.group(0)}»")

    for label, pattern in FORBIDDEN.items():
        if pattern.search(body):
            issues.append(f"не должно быть: {label}")

    return issues


def keyword_overlap(text: str, vacancy_text: str) -> List[str]:
    """Vacancy terms the letter actually reuses — advisory, not pass/fail."""
    terms = re.findall(
        r"\b(?:REST API|Postman|Swagger|SoapUI|SQL|JSON|XML|Chrome DevTools|DevTools|"
        r"Jira|Confluence|TestRail|Kibana|BrowserStack|Git|SOAP|HTTP|HTTPS|"
        r"регрессионн\w+|интеграционн\w+|исследовательск\w+|exploratory|smoke|"
        r"тест-кейс\w*|чек-лист\w*|баг-репорт\w*|локализац\w+ дефект\w*)",
        vacancy_text,
        re.I,
    )
    seen, used = set(), []
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if re.search(re.escape(term), text, re.I):
            used.append(term)
    return used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("letters", nargs="+")
    ap.add_argument("--descriptions")
    args = ap.parse_args()

    descriptions = {}
    if args.descriptions:
        descriptions = {
            str(r["vacancy_id"]): r["description_text"]
            for r in json.load(open(args.descriptions, encoding="utf-8"))
        }

    failed = 0
    for path in args.letters:
        text = Path(path).read_text(encoding="utf-8")
        issues = check(text)
        name = Path(path).name
        status = "OK  " if not issues else "ОШИБ"
        print(f"[{status}] {name}  ({len(text.strip())} знаков)")
        for issue in issues:
            print(f"         • {issue}")
            failed = 1
        vacancy_id = re.search(r"(\d{6,})", name)
        if vacancy_id and vacancy_id.group(1) in descriptions:
            used = keyword_overlap(text, descriptions[vacancy_id.group(1)])
            print(f"         термины из вакансии: {', '.join(used[:10])}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
