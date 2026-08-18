"""Push the hh.ru pipeline results into the QA Job Scout queue.

The fork produces JSON files; QA Job Scout owns the queue, the history and the
deduplication. This bridges the two through the sanctioned `ingest-browser` →
`import` contract, so no OpenAI key and no HH OAuth are involved.

It emits two files:

  1. a browser export for `qa-job-scout ingest-browser --file`
  2. a CodexRunResult for `qa-job-scout import --file`

IMPORTANT — the reviews it writes are rule-based, not two independent model
opinions. `scout` carries the automation/off-profile verdict from
filter_automation.py and `reviewer` the skills verdict from match_resume.py.
Both say so in their `recommendation`, so nothing downstream mistakes them for
an independent second opinion.

Usage:
    python3 tools/push_to_queue.py --stage export   # step 1, before ingest-browser
    python3 tools/push_to_queue.py --stage results --run-id N   # step 2, before import
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hh_common import load_rows  # noqa: E402

# Kept just under QA Job Scout's scout_threshold (80) on purpose: a clean
# vacancy still needs a human decision, and READY would demand a verified
# cover letter that does not exist yet.
CLEAN_SCORE = 70
REJECT_SCORE = 20
MANUAL_REVIEW_FLOOR = 50  # QA Job Scout's manual_review_threshold


def build_browser_export(
    vacancies: List[Dict[str, Any]],
    descriptions: Dict[str, str],
    applied: set,
) -> List[Dict[str, Any]]:
    """Shape the export the way ingest_browser_run expects."""
    rows = []
    for row in vacancies:
        vacancy_id = str(row["vacancy_id"])
        # Applied-to vacancies are ingested too, then closed as rejected below.
        # Skipping them here kept them out of the DB entirely, so every later
        # run rediscovered them as "new".
        rows.append(
            {
                "vacancy_id": vacancy_id,
                "title": row.get("title"),
                # A dict here survives into the stored vacancy as `employer`,
                # carrying the id the UI needs to link straight to the response
                # form instead of the vacancy page.
                "company": (
                    {"id": row["employer_id"], "name": row.get("company")}
                    if row.get("employer_id")
                    else row.get("company")
                ),
                "url": row.get("url"),
                "description_text": descriptions.get(vacancy_id, ""),
                "salary_text": _salary_text(row),
                "area": row.get("area"),
                "work_format": row.get("work_formats") or [],
                "experience": row.get("experience"),
                "has_test": bool(row.get("has_test")),
                "active": True,
                "published_at": row.get("published_at"),
                "created_at": row.get("created_at"),
                "source_phrases": row.get("source_phrases") or [],
            }
        )
    return rows


def _salary_text(row: Dict[str, Any]) -> str:
    if row.get("no_salary"):
        return ""
    low, high = row.get("salary_from"), row.get("salary_to")
    currency = row.get("salary_currency") or ""
    if low and high:
        return f"{low}–{high} {currency}".strip()
    if low:
        return f"от {low} {currency}".strip()
    if high:
        return f"до {high} {currency}".strip()
    return ""


def _review(
    *,
    score: int,
    matched: List[str],
    gaps: List[str],
    hard_stops: List[str],
    recommendation: str,
    manual: bool,
) -> Dict[str, Any]:
    return {
        "score": score,
        "matched": matched[:20],
        "gaps": gaps[:20],
        "warnings": [],
        "hard_stops": hard_stops[:10],
        "critical_unknowns": [],
        "recommendation": recommendation,
        "manual_review_recommended": manual,
    }


def build_results(
    run_id: int,
    resume_id: str,
    queued: List[str],
    filtered: Dict[str, Any],
    ranked: Dict[str, Dict[str, Any]],
    applied: set = frozenset(),
) -> Dict[str, Any]:
    """Map the rule-based verdicts onto the CodexRunResult schema."""
    verdicts = {}
    for bucket, label in (
        ("excluded", "автоматизация"),
        ("off_profile", "не по профилю"),
        ("not_qa", "не QA-роль"),
    ):
        for row in filtered.get(bucket, []):
            verdicts[row["vacancy_id"]] = (label, row)
    for row in filtered.get("kept", []):
        verdicts.setdefault(row["vacancy_id"], ("kept", row))

    items = []
    for vacancy_id in queued:
        label, row = verdicts.get(vacancy_id, ("kept", {}))
        rank = ranked.get(vacancy_id, {})
        if vacancy_id in applied:
            label = "отклик уже отправлен"
            row = {}

        if label == "kept":
            matched = rank.get("matched", [])
            gaps = rank.get("hard_gaps", []) + rank.get("soft_gaps", [])
            domain = rank.get("domain_gaps", [])
            scout = _review(
                score=CLEAN_SCORE,
                matched=matched,
                gaps=gaps,
                hard_stops=[],
                recommendation=(
                    "Правило filter_automation: жёстких требований к автоматизации нет. "
                    "Это не мнение модели, а детерминированная проверка."
                ),
                manual=True,
            )
            reviewer = _review(
                score=max(MANUAL_REVIEW_FLOOR, min(CLEAN_SCORE, 50 + 2 * (rank.get("fit") or 0))),
                matched=matched,
                gaps=gaps + [f"требуется опыт: {name}" for name in domain],
                hard_stops=[],
                recommendation=(
                    "Правило match_resume: совпадение по навыкам посчитано, пробелы перечислены. "
                    "Требуется решение человека."
                ),
                manual=True,
            )
        else:
            evidence = _evidence(row)
            stop = f"{label}: {evidence}" if evidence else label
            scout = _review(
                score=REJECT_SCORE,
                matched=[],
                gaps=[],
                hard_stops=[stop],
                recommendation="Отсеяно правилом filter_automation.",
                manual=False,
            )
            reviewer = dict(scout)

        items.append({"vacancy_id": vacancy_id, "scout": scout, "reviewer": reviewer})

    return {"run_id": run_id, "resume_id": resume_id, "items": items}


def _evidence(row: Dict[str, Any]) -> str:
    for name, _points, text in row.get("reasons", []):
        if text:
            return f"{name} — {text[:120]}"
    if row.get("off_profile"):
        return ", ".join(row["off_profile"])
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("export", "results"), required=True)
    ap.add_argument("--vacancies", default="job_scraper/hh-export-2026-08-15.json")
    ap.add_argument("--descriptions", default="job_scraper/hh-descriptions-2026-08-15.json")
    ap.add_argument("--filtered", default="job_scraper/hh-filtered-2026-08-15.json")
    ap.add_argument("--ranked", default="job_scraper/hh-ranked-open-2026-08-16.json")
    ap.add_argument("--status", default="job_scraper/hh-response-status-2026-08-16.json")
    ap.add_argument("--run-id", type=int)
    ap.add_argument("--resume-id")
    ap.add_argument("--queued", help="JSON list of vacancy ids actually queued by the run")
    ap.add_argument("--applied", help="JSON map of vacancy_id -> response status")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    if args.stage == "export":
        status = json.load(open(args.status, encoding="utf-8")) if Path(args.status).exists() else {}
        applied = {vid for vid, row in status.items() if row.get("applied")}
        descriptions = {
            str(r["vacancy_id"]): r["description_text"]
            for r in load_rows(args.descriptions)
            if r.get("description_text")
        }
        rows = build_browser_export(load_rows(args.vacancies), descriptions, applied)
        Path(args.output).write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"в экспорт: {len(rows)} вакансий (из них с уже отправленным откликом: {len(applied)})")
        print(f"записано: {args.output}")
        return 0

    if not args.run_id or not args.resume_id or not args.queued:
        print("для --stage results нужны --run-id, --resume-id и --queued", file=sys.stderr)
        return 2

    filtered = json.load(open(args.filtered, encoding="utf-8"))
    ranked = {row["vacancy_id"]: row for row in load_rows(args.ranked)}
    queued = json.loads(Path(args.queued).read_text(encoding="utf-8"))
    applied = set()
    if args.applied and Path(args.applied).exists():
        applied = {
            vid for vid, row in json.load(open(args.applied, encoding="utf-8")).items()
            if row.get("applied")
        }
    payload = build_results(args.run_id, args.resume_id, queued, filtered, ranked, applied)
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    kept = sum(1 for item in payload["items"] if not item["scout"]["hard_stops"])
    print(f"результатов: {len(payload['items'])} (на решение: {kept}, отсеяно: {len(payload['items']) - kept})")
    print(f"записано: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
