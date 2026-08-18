"""Score hh.ru vacancies by how hard their test-automation requirement is.

Built for a manual-QA job search: the goal is to drop roles that *require*
writing autotests while keeping roles where automation is merely a nice-to-have.

The scoring is deliberately transparent — every point added is reported with the
sentence that triggered it, so a borderline verdict can be argued with rather
than trusted blindly. Calibrated against vacancies the candidate rejected by
hand with the reason "автоматизация" (see --selftest).

Usage:
    python3 tools/filter_automation.py --input <export.json> [--descriptions <enriched.json>]
    python3 tools/filter_automation.py --selftest --descriptions <enriched.json> --labels <sqlite>
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hh_common import (  # noqa: E402
    OPTIONAL as SOFT,
    load_descriptions,
    load_rows,
    load_skills,
    split_sentences,
)

AUTO = re.compile(
    r"автоматизац|автотест|автоматизированн|автоматизир|\bAQA\b|\bSDET\b|"
    r"automation|automated|selenium|playwright|pytest|rest[\s-]?assured|"
    r"appium|cypress|junit|testng|webdriver|allure",
    re.I,
)

# Automation named in the job title is the strongest single signal.
TITLE_AUTO = re.compile(
    r"автоматизат|автоматизац|автоматизированн|автотест|\bAQA\b|\bSDET\b|"
    r"automation|automated|auto\s*qa|qa\s*auto|\bauto\b",
    re.I,
)

# Section headers. Automation inside the duties block is disqualifying; the same
# words inside a "nice to have" list are not, so the block matters more than the
# verb — chasing individual verbs missed "доработка фреймворка" and "внесение
# изменений в автотесты" until this replaced it.
DUTIES_HEADER = re.compile(
    r"чем предстоит заниматься|обязанност|задачи|что предстоит|что делать|"
    r"ваши задачи|функционал|зона ответственност|вы будете|предстоит|"
    r"responsibilities|what you.{0,3}ll do|your role|day.to.day|about the role",
    re.I,
)
REQS_HEADER = re.compile(
    r"требован|мы ожидаем|ожидания|наши пожелания|что мы ждем|что мы ждём|"
    r"необходим|квалификац|наш кандидат|вы подходите|"
    r"requirements|qualifications|what we expect|you have|must have|your profile",
    re.I,
)
PERKS_HEADER = re.compile(
    r"будет плюсом|плюсом будет|преимуществ|приветствуется|условия|мы предлагаем|"
    r"nice to have|bonus|we offer|benefits|preferred",
    re.I,
)

# Pure hand-off: the candidate writes test cases and someone else automates
# them. Nothing is required of the candidate, so these sentences count for
# nothing — this was the filter's main false-positive source ("Senior Manual QA
# Engineer" was being cut because its test cases go to an automation team).
HANDOFF = re.compile(
    r"передаются?\s+в\s+команд|передают\s+в\s+команд|"
    r"коллег\w*[-\s]автоматизатор|автоматизатор\w*\s+(?:пишут|разрабатыва)",
    re.I,
)

# Owning automation without writing it: leads and seniors who must understand,
# review, task or steer an automation team. Excluded by explicit choice —
# these roles still demand automation depth the candidate is not applying for.
OVERSIGHT = re.compile(
    r"понимание\s+принципов\s+автоматизац|опыт\s+взаимодействи\w*\s+с\s+команд\w*\s+автоматизац|"
    r"ставить\s+задачи\s+на\s+(?:разработку\s+)?автотест|принимать\s+автотест|"
    r"стратеги\w*\s+автоматизац|управлени\w*\s+автоматизац|"
    r"руководств\w*\s+(?:\w+\s+){0,2}?автоматизац|развити\w*\s+автоматизац|"
    r"планирован\w*\s+автоматизац|приоритизац\w*\s+автоматизац",
    re.I,
)

# Writing/owning autotests as a duty — the disqualifying pattern.
DUTY = re.compile(
    r"(?:писать|написание|разработ|создан|проектирован|реализац|поддерж|развива|"
    r"актуализац|внедрен|доработ|внесен|обеспечен|отслеживан|покрыт|интеграц|"
    r"проведен|выполнен|организац|миграц)\w*\s+(?:\w+\s+){0,4}?"
    r"(?:автотест|автоматизированн|автоматизац|тестов\w*\s+фреймворк|фреймворк)"
    r"|(?:writing|write|building|build|maintain\w*|design\w*|develop\w*|creating|create)\s+"
    r"(?:\w+\s+){0,4}?(?:automat\w+|autotest|test\s+framework|e2e\s+test)",
    re.I,
)

# Explicit required experience in automation.
REQ_EXP = re.compile(
    r"опыт\w*\s+(?:\w+\s+){0,4}?(?:автоматизац|автотест|автоматизированн)\w*"
    r"|(?:автоматизац|автотест)\w*\s+(?:\w+\s+){0,3}?от\s+\d+\s+(?:год|лет|мес)"
    r"|уверенн\w*\s+(?:знание|владение)\s+(?:python|java|c#|typescript|javascript|go)\b",
    re.I,
)

FRAMEWORK = re.compile(
    r"selenium|playwright|pytest|rest[\s-]?assured|appium|cypress|junit|testng|"
    r"webdriver|hp uft|testcomplete|k6|jmeter|specflow|nunit|xunit|cucumber|"
    r"robot framework|фр[еэ]ймворк|codecept|puppeteer|karate|gatling",
    re.I,
)

# A QA role must say so. This started as a blacklist of dev/analyst titles and
# leaked "IT Рекрутер", "ВНЕШТАТНЫЙ ПАРТНЁР" and "PHP-программист" into the
# results — anything absent from the list passed. A whitelist cannot leak that
# way: no QA word in the title or key skills means it is keyword-search noise.
IS_QA = re.compile(
    r"\bqa\b|\bsdet\b|тестировщ|тестирован|тестиров|\btest(?:er|ing)?\b|"
    r"quality\s+(?:assurance|engineer)|контрол\w*\s+качества",
    re.I,
)

# Profile mismatches — a separate axis from automation. These are not about how
# much autotesting a role demands; they are roles the candidate does not want at
# all, so they get their own reason label instead of being folded into the score.
OFF_PROFILE = {
    "нагрузочное": re.compile(
        r"нагрузочн|load\s*(?:qa|test)|performance\s*(?:qa|test)|перформанс|стресс-тест",
        re.I,
    ),
    "junior": re.compile(
        r"\bjunior\b|\bjun\b|младш|ученик|стаж[её]р|\bintern(?:ship)?\b|\btrainee\b|"
        r"начинающ|без опыта",
        re.I,
    ),
    # Cyrillic С and Latin C are both used in the wild, often in the same posting.
    "1С": re.compile(r"\b1[СC]\b|\b1[СC][-\s]?(?:битрикс|предприят|erp|унф|зуп)", re.I),
    # "Fullstack QA" on hh means manual plus automation in one seat — the
    # automation half is exactly what this search excludes, and the title says
    # so before the body does.
    "fullstack": re.compile(r"fullstack|full[-\s]stack|full stack", re.I),
    "мобильное": re.compile(r"мобильн|\bmobile\b|\bandroid\b|\bios\b", re.I),
}

THRESHOLD = 6


def label_sections(sentences: List[str]) -> List[Tuple[str, str]]:
    """Tag each sentence with the block it belongs to: duties, reqs, perks, other.

    Headers often share a line with their first item ("Чем предстоит заниматься
    Проектирование автотестов"), so a header switches the current section and the
    line still counts as content.
    """
    current = "other"
    tagged: List[Tuple[str, str]] = []
    for sentence in sentences:
        head = sentence[:70]
        if PERKS_HEADER.search(head):
            current = "perks"
        elif REQS_HEADER.search(head):
            current = "reqs"
        elif DUTIES_HEADER.search(head):
            current = "duties"
        tagged.append((current, sentence))
    return tagged


def score(title: str, description: Optional[str]) -> Dict[str, Any]:
    """Return a verdict dict: score, reasons, and the evidence for each."""
    reasons: List[Tuple[str, int, str]] = []
    points = 0

    # An automation title is decisive on its own: AQA/SDET/"Automation Engineer"
    # roles are automation jobs whatever the body text says, and hybrid
    # "Manual + Automation" titles were rejected by hand for the same reason.
    if TITLE_AUTO.search(title or ""):
        points += 6
        reasons.append(("автоматизация в заголовке", 6, title))

    if description:
        tagged = label_sections(split_sentences(description))
        auto = [
            (sec, s) for sec, s in tagged
            if AUTO.search(s) and not HANDOFF.search(s) and not OVERSIGHT.search(s)
        ]
        auto_sentences = [s for _, s in auto]

        handed_off = [s for _, s in tagged if AUTO.search(s) and HANDOFF.search(s)]
        if handed_off:
            reasons.append(("автоматизацию пишет другая команда", 0, handed_off[0][:160]))

        # Decisive on its own, by explicit choice: owning or steering
        # automation without writing it still means being measured on automation
        # depth (QA Team Lead, ведущий инженер who tasks an automation team).
        oversight = [s for _, s in tagged if AUTO.search(s) and OVERSIGHT.search(s)]
        if oversight:
            gain = min(6 * len(oversight), 8)
            points += gain
            reasons.append(("надзор за автоматизацией", gain, oversight[0][:160]))

        # Automation listed among the duties is the disqualifying signal: it is
        # what the person will spend their days doing, regardless of phrasing.
        in_duties = [s for sec, s in auto if sec == "duties" and not SOFT.search(s)]
        if in_duties:
            gain = min(4 + 2 * (len(in_duties) - 1), 8)
            points += gain
            reasons.append(("автоматизация в обязанностях", gain, in_duties[0][:160]))

        duty_hits = [s for s in auto_sentences if DUTY.search(s) and s not in in_duties]
        if duty_hits:
            gain = min(3 * len(duty_hits), 6)
            points += gain
            reasons.append(("работа с автотестами как задача", gain, duty_hits[0][:160]))

        req_hits = [
            s for sec, s in auto
            if REQ_EXP.search(s) and sec != "perks" and not SOFT.search(s)
        ]
        if req_hits:
            gain = min(4 * len(req_hits), 8)
            points += gain
            reasons.append(("требуется опыт автоматизации", gain, req_hits[0][:160]))

        fw_hits = [
            s for sec, s in auto
            if FRAMEWORK.search(s) and sec != "perks" and not SOFT.search(s)
        ]
        if fw_hits:
            gain = min(2 * len(fw_hits), 4)
            points += gain
            reasons.append(("названы инструменты автоматизации", gain, fw_hits[0][:160]))

        if len(auto_sentences) >= 5:
            points += 2
            reasons.append(("плотность упоминаний", 2, f"{len(auto_sentences)} предложений"))

        # A discount, never a veto: one "будет плюсом" line does not undo a duty list.
        soft_hits = [s for sec, s in auto if SOFT.search(s) or sec == "perks"]
        if soft_hits:
            loss = min(2 * len(soft_hits), 4)
            points -= loss
            reasons.append(("смягчающая формулировка", -loss, soft_hits[0][:160]))

        if not auto_sentences:
            reasons.append(("автоматизация не упоминается", 0, ""))

    return {
        "score": points,
        "verdict": "exclude" if points >= THRESHOLD else "keep",
        "confident": description is not None,
        "reasons": reasons,
    }


def classify(
    rows: List[Dict[str, Any]],
    descriptions: Dict[str, str],
    skills: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    skills = skills or {}
    out = []
    for row in rows:
        vid = str(row["vacancy_id"])
        title = row.get("title") or ""
        desc = descriptions.get(vid)
        verdict = score(title, desc)
        is_qa = bool(IS_QA.search(title)) or any(
            IS_QA.search(s) for s in skills.get(vid, [])
        )
        # Title and key skills only: a description that merely mentions load
        # testing among many duties is not a load-testing role.
        haystack = " ".join([title] + skills.get(vid, []))
        off_profile = [name for name, rx in OFF_PROFILE.items() if rx.search(haystack)]
        verdict.update(
            off_profile=off_profile,
            vacancy_id=vid,
            title=title,
            company=row.get("company"),
            url=row.get("url"),
            published_at=row.get("published_at"),
            created_at=row.get("created_at"),
            area=row.get("area"),
            # Carried through so downstream scoring can weigh pay without
            # re-joining against the raw export.
            no_salary=row.get("no_salary"),
            salary_from=row.get("salary_from"),
            salary_to=row.get("salary_to"),
            salary_currency=row.get("salary_currency"),
            experience=row.get("experience"),
            has_test=row.get("has_test"),
            not_qa=not is_qa,
            has_description=desc is not None,
        )
        out.append(verdict)
    return out


def selftest(descriptions: Dict[str, str], labels_db: str, enriched: str) -> int:
    """Check the scorer against the vacancies the user rejected for automation."""
    rows = load_rows(enriched)
    titles = {str(r["vacancy_id"]): r.get("title", "") for r in rows}
    db = sqlite3.connect(labels_db)
    labelled = [
        vid
        for vid, reason in db.execute(
            "select vacancy_id, json_extract(payload_json,'$.reason') "
            "from candidate_events where event_type='rejected'"
        )
        if reason and "втоматиз" in reason
    ]
    caught, missed = [], []
    for vid in labelled:
        if vid not in descriptions:
            continue
        verdict = score(titles.get(vid, ""), descriptions[vid])
        (caught if verdict["verdict"] == "exclude" else missed).append(
            (titles.get(vid, "")[:55], verdict["score"])
        )
    print(f"размеченных отказов с описанием: {len(caught) + len(missed)}")
    print(f"поймано: {len(caught)}   пропущено: {len(missed)}")
    for title, sc in missed:
        print(f"  ПРОПУСК  {title:57} score={sc}")
    return 0 if not missed else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input")
    ap.add_argument("--descriptions")
    ap.add_argument("--labels")
    ap.add_argument("--enriched")
    ap.add_argument("--output")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    descriptions = load_descriptions(args.descriptions)

    if args.selftest:
        return selftest(descriptions, args.labels, args.enriched)

    rows = load_rows(args.input)
    results = classify(rows, descriptions, load_skills(args.descriptions))

    not_qa = [r for r in results if r["not_qa"]]
    qa = [r for r in results if not r["not_qa"]]
    excluded = [r for r in qa if r["verdict"] == "exclude"]
    rest = [r for r in qa if r["verdict"] == "keep"]
    off_profile = [r for r in rest if r["off_profile"]]
    kept = [r for r in rest if not r["off_profile"]]
    unknown = [r for r in kept if not r["has_description"]]

    print(f"всего:                       {len(results)}")
    print(f"− не QA-роли:                {len(not_qa)}")
    print(f"= QA-вакансии:               {len(qa)}")
    print(f"   − автоматизация:          {len(excluded)}")
    print(f"   − не по профилю:          {len(off_profile)}")
    for name in OFF_PROFILE:
        hits = [r for r in off_profile if name in r["off_profile"]]
        if hits:
            print(f"        {name:14} {len(hits)}")
    print(f"   = ЧИСТЫЙ ОСТАТОК:         {len(kept)}")
    if unknown:
        print(f"        без описания:  {len(unknown)}  (вердикт ненадёжен)")

    if args.output:
        payload = {
            "kept": kept,
            "excluded": excluded,
            "off_profile": off_profile,
            "not_qa": not_qa,
        }
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print(f"\nзаписано: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
