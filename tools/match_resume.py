"""Score filtered hh.ru vacancies against the candidate profile.

This does the mechanical half of fit evaluation — which required skills the
resume covers, which it does not, seniority, salary, and blockers — so that
human (or model) judgement is spent only on the shortlist it produces, and on
the gaps it names explicitly.

It deliberately does NOT decide whether to apply. A high score means "worth
reading", not "good fit".

Usage:
    python3 tools/match_resume.py --vacancies job_scraper/hh-clean-2026-08-15.json \
        --descriptions job_scraper/hh-descriptions-2026-08-15.json \
        --profile job_scraper/profile.json --output job_scraper/hh-ranked.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hh_common import (  # noqa: E402
    MANDATORY,
    OPTIONAL,
    load_descriptions,
    load_rows,
    modality,
    split_sentences as sentences,
)

# Skills the resume genuinely covers, matched against vacancy text. The keys are
# what a posting is likely to write; the values are what to credit it as.
SKILL_PATTERNS: Dict[str, str] = {
    r"postman": "Postman",
    r"\brest\b|rest\s*api|api[-\s]тестирован": "REST API",
    r"\bsql\b|postgres|mysql|запрос\w* к бд|базам? данных": "SQL",
    r"dbeaver": "DBeaver",
    r"devtools|chrome dev|сетев\w* запрос|http[-\s]запрос": "DevTools / HTTP",
    r"\bjira\b": "Jira",
    r"confluence": "Confluence",
    r"testrail|test\s*rail|qase|allure\s*testops": "TestRail",
    r"\bgit\b": "Git",
    r"browserstack": "BrowserStack",
    r"figma": "Figma",
    r"sentry|kibana|opensearch|elastic|graylog|логи": "Логи / мониторинг",
    r"тест[-\s]?кейс|test\s*case": "Тест-кейсы",
    r"чек[-\s]?лист|checklist": "Чек-листы",
    r"регрессион": "Регрессионное",
    r"исследовательск|exploratory": "Исследовательское",
    r"smoke": "Smoke",
    r"интеграционн": "Интеграционное",
    r"баг[-\s]?репорт|дефект": "Баг-репорты",
    r"\bb2b\b|\bsaas\b": "B2B SaaS",
    r"биллинг|платеж|billing|payment": "Платежи / биллинг",
    r"права доступа|роли и права|персональн\w* данн": "Права доступа / ПДн",
}

# Requirements the resume does not cover. Presence is reported as a gap, and a
# gap stated as mandatory costs points.
GAP_PATTERNS: Dict[str, str] = {
    r"\bpython\b": "Python",
    r"\bc#\b|\.net\b": "C#/.NET",
    r"\bgolang\b|\bgo\b(?!\s*ogle)": "Go",
    r"\bkotlin\b": "Kotlin",
    r"typescript|javascript|\bjs\b": "JS/TS",
    r"\bkafka\b": "Kafka",
    r"kubernetes|k8s|docker": "Docker/K8s",
    r"\blinux\b|unix|консол\w* утилит": "Linux/консоль",
    r"ci/cd|jenkins|gitlab ci|github actions": "CI/CD",
    r"grafana|prometheus": "Grafana/Prometheus",
    r"\bgrpc\b|graphql": "gRPC/GraphQL",
    r"\bbash\b|\bshell\b": "Bash",
}

# Named systems a posting can demand hands-on experience with. A skills-overlap
# score cannot see these: "опыт тестирования ЦФТ-Банк (обязателен)" scored 12
# because every generic QA skill matched, while the one disqualifying line —
# a core banking system absent from the CV — carried no weight at all.
DOMAIN_SYSTEMS = re.compile(
    r"\bЦФТ(?:[-\s]?Банк)?\b|\bАБС\b|\b1[СC]\b|\bSAP\b|\bSiebel\b|\bDiasoft\b|"
    r"\bБитрикс\w*|\bBitrix\w*|\bamoCRM\b|\bSalesforce\b|\bMS\s*Dynamics\b|"
    r"\bOracle\s*(?:EBS|Siebel|Forms)\b|\bR-?Keeper\b|\bГИС\s*\w+|\bЕГАИС\b|"
    r"\bSharePoint\b|\bMagento\b|\bSAP\s*\w+|\bTemenos\b|\bFlexcube\b|"
    r"\bUnreal\s*Engine\b|\bUE[45]\b|\bUnity\b|\bSCADA\b|\bМИС\b|\bЭДО\b",
    re.I,
)

DOMAIN_PENALTY = 5

SENIORITY = {
    "senior": re.compile(r"\bsenior\b|\bсеньор|ведущий|старший", re.I),
    "middle": re.compile(r"\bmiddle\b|\bмидл|средний", re.I),
    "lead": re.compile(r"\blead\b|\bлид\b|руководител|тимлид|team\s*lead", re.I),
}

ENGLISH_REQ = re.compile(r"английск|english", re.I)
TEST_TASK = re.compile(r"тестово\w* задани|test\s*task|выполнить задание", re.I)
OFFICE = re.compile(r"офис|onsite|on-site|гибрид|hybrid|релокац", re.I)


def domain_gaps(description: str, profile: Dict[str, Any]) -> List[str]:
    """Named systems a posting requires experience with that the CV does not have.

    Only mandatory lines count: "будет плюсом опыт 1С" is not a gap.
    """
    covered = " ".join(
        profile.get("strong_skills", [])
        + profile.get("domains", [])
        + profile.get("light_automation", {}).get("tools", [])
    ).lower()
    found: List[str] = []
    for line in sentences(description):
        if not MANDATORY.search(line):
            continue
        for match in DOMAIN_SYSTEMS.finditer(line):
            name = match.group(0).strip()
            if name.lower() not in covered and name not in found:
                found.append(name)
    return found


def analyse(description: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    matched: List[str] = []
    gaps: List[Tuple[str, str]] = []
    lines = sentences(description)
    for pattern, label in SKILL_PATTERNS.items():
        if any(re.search(pattern, line, re.I) for line in lines) and label not in matched:
            matched.append(label)
    for pattern, label in GAP_PATTERNS.items():
        hits = [line for line in lines if re.search(pattern, line, re.I)]
        if not hits:
            continue
        strength = "optional"
        for line in hits:
            mode = modality(line)
            if mode == "mandatory":
                strength = "mandatory"
                break
            if mode == "neutral" and strength == "optional":
                strength = "neutral"
        gaps.append((label, strength))
    return matched, gaps


def salary_note(row: Dict[str, Any], thresholds: Dict[str, int]) -> Tuple[int, str]:
    if row.get("no_salary"):
        return 0, "не указана"
    currency = (row.get("salary_currency") or "RUR").upper()
    floor = thresholds.get(currency)
    low, high = row.get("salary_from"), row.get("salary_to")
    shown = f"{low or ''}–{high or ''} {currency}".strip("–  ")
    if floor is None:
        return 0, f"{shown} (порог для валюты не задан)"
    # Compare against the top of the range: "1500–2500 USD" can still meet a
    # 1800 expectation, and judging it by the floor rejected a strong match.
    ceiling = high or low
    if ceiling is None:
        return 0, shown
    if ceiling < floor:
        return -3, f"{shown} — потолок ниже ожидания {floor}"
    if low is not None and low < floor:
        return 1, f"{shown} (нижняя граница ниже {floor}, потолок выше)"
    return 2, f"{shown} ✓"


def score_vacancy(
    row: Dict[str, Any], description: Optional[str], profile: Dict[str, Any]
) -> Dict[str, Any]:
    if not description:
        return {**row, "fit": None, "note": "нет описания"}

    matched, gaps = analyse(description)
    points = 0
    notes: List[str] = []

    points += min(len(matched), 12)
    notes.append(f"совпало навыков: {len(matched)}")

    hard_gaps = [g for g, s in gaps if s == "mandatory"]
    soft_gaps = [g for g, s in gaps if s != "mandatory"]
    points -= 3 * len(hard_gaps)
    points -= 1 * len(soft_gaps)

    # Mandatory experience with a named system the CV does not have. Weighted
    # heavier than a tool gap: a posting that demands ЦФТ-Банк or SAP hands-on
    # will not accept transferable QA skill in its place.
    domain = domain_gaps(description, profile)
    points -= DOMAIN_PENALTY * len(domain)

    title = row.get("title") or ""
    text = f"{title}\n{description}"
    if SENIORITY["senior"].search(text):
        points += 3
        notes.append("уровень senior")
    elif SENIORITY["lead"].search(title):
        points += 1
    elif SENIORITY["middle"].search(text):
        points += 1
        notes.append("уровень middle")

    sal_points, sal_note = salary_note(row, profile["salary_thresholds"])
    points += sal_points

    if ENGLISH_REQ.search(description):
        points += 1
        notes.append("нужен английский (C1 ✓)")
    if TEST_TASK.search(description):
        notes.append("есть тестовое задание")
    if OFFICE.search(description):
        notes.append("упоминается офис/гибрид/релокация")

    return {
        **row,
        "fit": points,
        "matched": matched,
        "hard_gaps": hard_gaps,
        "soft_gaps": soft_gaps,
        "domain_gaps": domain,
        "salary": sal_note,
        "notes": notes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vacancies", required=True)
    ap.add_argument("--descriptions", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--output")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    profile = json.load(open(args.profile, encoding="utf-8"))
    rows = load_rows(args.vacancies)
    descriptions = load_descriptions(args.descriptions)

    scored = [score_vacancy(r, descriptions.get(str(r["vacancy_id"])), profile) for r in rows]
    ranked = sorted(
        [s for s in scored if s["fit"] is not None], key=lambda s: -s["fit"]
    )

    print(f"оценено вакансий: {len(ranked)}\n")
    for row in ranked[: args.top]:
        print(f"[{row['fit']:>2}] {row['title'][:52]:54} {(row['company'] or '')[:22]}")
        print(f"      навыки: {', '.join(row['matched'][:9])}")
        if row["domain_gaps"]:
            print(f"      ТРЕБУЕТ ОПЫТА (нет в резюме): {', '.join(row['domain_gaps'])}")
        if row["hard_gaps"]:
            print(f"      ПРОБЕЛЫ (обязательные): {', '.join(row['hard_gaps'])}")
        if row["soft_gaps"]:
            print(f"      пробелы (желательные):  {', '.join(row['soft_gaps'])}")
        print(f"      зарплата: {row['salary']}   {'; '.join(row['notes'][1:])}")
        print()

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(ranked, fh, ensure_ascii=False, indent=1)
        print(f"записано: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
