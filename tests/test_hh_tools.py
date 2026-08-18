"""Tests for the hh.ru pipeline tools: filter_automation, match_resume, check_letter.

Each case here is a bug the tools actually shipped with at some point, kept so a
future edit to the regexes cannot quietly reintroduce it.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_letter  # noqa: E402
import filter_automation as fa  # noqa: E402
import hh_common  # noqa: E402
import match_resume as mr  # noqa: E402


class SplitSentencesTests(unittest.TestCase):
    def test_splits_on_bullets_and_newlines(self):
        text = "Обязанности:\n• писать автотесты\n• вести документацию"
        parts = hh_common.split_sentences(text)
        self.assertIn("писать автотесты", parts)
        self.assertIn("вести документацию", parts)

    def test_ignores_empty_fragments(self):
        self.assertEqual(hh_common.split_sentences("\n\n  \n"), [])


class ModalityTests(unittest.TestCase):
    def test_optional_beats_mandatory(self):
        # A nice-to-have inside a requirements block is still a nice-to-have.
        line = "Требования: будет плюсом опыт автоматизации"
        self.assertEqual(hh_common.modality(line), "optional")

    def test_mandatory(self):
        self.assertEqual(hh_common.modality("Опыт обязателен"), "mandatory")

    def test_neutral(self):
        self.assertEqual(hh_common.modality("Работа с логами"), "neutral")


class AutomationFilterTests(unittest.TestCase):
    def test_automation_title_alone_excludes(self):
        verdict = fa.score("QA Automation Engineer (Java)", None)
        self.assertEqual(verdict["verdict"], "exclude")

    def test_plain_manual_role_kept(self):
        verdict = fa.score(
            "Тестировщик",
            "Обязанности: ручное функциональное тестирование, ведение тест-кейсов.",
        )
        self.assertEqual(verdict["verdict"], "keep")

    def test_single_automation_duty_is_not_decisive(self):
        # Deliberate calibration: one mention in the duties block scores 4 of the
        # 6 needed. "Анализировать регресс автотестами" is manual QA work, and
        # cutting on it alone dropped genuine manual roles.
        verdict = fa.score(
            "Senior Manual QA Engineer",
            "Обязанности\nРаботать с отчетами Allure, анализировать проведение регресса автотестами.",
        )
        self.assertEqual(verdict["verdict"], "keep")

    def test_repeated_automation_duties_exclude(self):
        verdict = fa.score(
            "QA Engineer (Middle)",
            "Чем предстоит заниматься\n"
            "Проектирование и реализация автотестов: unit / API / e2e;\n"
            "Обеспечение и отслеживание покрытия автотестами;",
        )
        self.assertEqual(verdict["verdict"], "exclude")

    def test_nice_to_have_automation_is_kept(self):
        verdict = fa.score(
            "Специалист по тестированию",
            "Обязанности: ручное тестирование.\nБудет плюсом: понимание и опыт работы с автотестами.",
        )
        self.assertEqual(verdict["verdict"], "keep")

    def test_handoff_to_another_team_is_not_a_duty(self):
        # A manual role whose test cases are automated by someone else.
        verdict = fa.score(
            "Senior Manual QA Engineer",
            "Обязанности: писать тест-кейсы в TestRail, далее они передаются в команду автоматизации.",
        )
        self.assertEqual(verdict["verdict"], "keep")

    def test_oversight_of_an_automation_team_excludes(self):
        verdict = fa.score(
            "QA Team Lead",
            "Требования: понимание принципов автоматизации тестирования, "
            "опыт взаимодействия с командами автоматизации.",
        )
        self.assertEqual(verdict["verdict"], "exclude")

    def test_english_duties_section_is_recognised(self):
        verdict = fa.score(
            "Senior QA engineer (full-stack)",
            "Responsibilities\nDesigning, building and maintaining automated test frameworks "
            "using Playwright and Selenium.",
        )
        self.assertEqual(verdict["verdict"], "exclude")

    def test_reasons_carry_evidence(self):
        verdict = fa.score("QA Automation Engineer", None)
        self.assertTrue(all(len(reason) == 3 for reason in verdict["reasons"]))


class QaWhitelistTests(unittest.TestCase):
    def _classify(self, title, skills=None):
        rows = [{"vacancy_id": "1", "title": title}]
        skill_map = {"1": skills or []}
        return fa.classify(rows, {}, skill_map)[0]

    def test_developer_title_is_not_qa(self):
        self.assertTrue(self._classify("PHP-программист Laravel")["not_qa"])

    def test_recruiter_title_is_not_qa(self):
        # The old blacklist let anything unlisted through, including this.
        self.assertTrue(self._classify("IT Рекрутер")["not_qa"])

    def test_tester_title_is_qa(self):
        self.assertFalse(self._classify("Мануальный тестировщик")["not_qa"])

    def test_qa_recognised_via_key_skills(self):
        row = self._classify("Специалист по внедрению", ["QA", "Postman"])
        self.assertFalse(row["not_qa"])


class OffProfileTests(unittest.TestCase):
    def _off(self, title):
        rows = [{"vacancy_id": "1", "title": title}]
        return fa.classify(rows, {}, {"1": []})[0]["off_profile"]

    def test_junior(self):
        self.assertIn("junior", self._off("Junior QA Engineer"))

    def test_load_testing(self):
        self.assertIn("нагрузочное", self._off("Инженер по нагрузочному тестированию"))

    def test_1c_cyrillic_and_latin(self):
        self.assertIn("1С", self._off("Тестировщик 1С"))
        self.assertIn("1С", self._off("QA Engineer 1C"))

    def test_plain_role_has_no_flags(self):
        self.assertEqual(self._off("Senior QA Engineer"), [])


class SalaryTests(unittest.TestCase):
    # Neutral round numbers: fixtures must not leak a real salary expectation.
    THRESHOLDS = {"RUR": 100000, "USD": 1000}

    def test_no_salary(self):
        points, note = mr.salary_note({"no_salary": True}, self.THRESHOLDS)
        self.assertEqual(points, 0)
        self.assertEqual(note, "не указана")

    def test_range_ceiling_above_expectation_is_accepted(self):
        # 800–1500 can still meet 1000; judging by the floor rejected a match.
        points, note = mr.salary_note(
            {"salary_from": 800, "salary_to": 1500, "salary_currency": "USD"},
            self.THRESHOLDS,
        )
        self.assertGreater(points, 0)
        self.assertIn("потолок выше", note)

    def test_range_entirely_below_expectation_is_penalised(self):
        points, _ = mr.salary_note(
            {"salary_from": 300, "salary_to": 600, "salary_currency": "USD"},
            self.THRESHOLDS,
        )
        self.assertLess(points, 0)

    def test_open_ended_floor_above_expectation(self):
        points, note = mr.salary_note(
            {"salary_from": 2000, "salary_to": None, "salary_currency": "USD"},
            self.THRESHOLDS,
        )
        self.assertEqual(points, 2)
        self.assertIn("✓", note)


class DomainGapTests(unittest.TestCase):
    PROFILE = {"strong_skills": ["postman", "sql"], "domains": ["b2b saas"]}

    def test_mandatory_named_system_is_a_gap(self):
        gaps = mr.domain_gaps("опыт тестирования ЦФТ-Банк обязателен", self.PROFILE)
        self.assertTrue(any("ЦФТ" in gap for gap in gaps))

    def test_optional_named_system_is_not_a_gap(self):
        gaps = mr.domain_gaps("Будет плюсом опыт 1С", self.PROFILE)
        self.assertEqual(gaps, [])

    def test_system_already_in_profile_is_not_a_gap(self):
        profile = {"strong_skills": ["sap"], "domains": []}
        self.assertEqual(mr.domain_gaps("Требуется опыт SAP", profile), [])


class LetterCheckTests(unittest.TestCase):
    def _letter(self, *paragraphs):
        return "\n\n".join(paragraphs)

    def test_valid_letter_passes(self):
        text = self._letter(
            "Здравствуйте! " + "Опыт ручного тестирования web-приложений и backend-сервисов. " * 4,
            "Тестирую REST API через Postman, проверяю данные в SQL, веду тест-кейсы. " * 5,
            "Готова обсудить задачи команды и продукт, отвечу на вопросы в удобное время. " * 4,
        )
        self.assertTrue(
            check_letter.MIN_CHARS <= len(text) <= check_letter.MAX_CHARS,
            f"фикстура вне диапазона: {len(text)}",
        )
        self.assertEqual(check_letter.check(text), [])

    def test_too_short_is_flagged(self):
        issues = check_letter.check("Здравствуйте!\n\nКоротко.\n\nСпасибо.")
        self.assertTrue(any("длина" in issue for issue in issues))

    def test_wrong_paragraph_count(self):
        body = "А" * 400
        issues = check_letter.check(self._letter(body, body))
        self.assertTrue(any("абзацев" in issue for issue in issues))

    def test_gendered_past_is_flagged(self):
        issues = check_letter.check("Я работала в компании")
        self.assertTrue(any("родовым окончанием" in issue for issue in issues))

    def test_adverbs_are_not_mistaken_for_verbs(self):
        # "сначала", "начала" and "была" are not first-person past verbs.
        for word in ("сначала", "начала", "была"):
            issues = check_letter.check(f"Работа {word} устроена так же")
            self.assertFalse(
                any("родовым окончанием" in issue for issue in issues),
                f"ложное срабатывание на «{word}»",
            )

    def test_parenthetical_gender_is_flagged(self):
        issues = check_letter.check("работал(а) в команде")
        self.assertTrue(any("скобках" in issue for issue in issues))

    def test_contacts_are_flagged(self):
        issues = check_letter.check("Пишите на me@example.com")
        self.assertTrue(any("контакты" in issue for issue in issues))

    def test_keyword_overlap_reports_reused_terms(self):
        used = check_letter.keyword_overlap(
            "Тестирую REST API через Postman", "Требуется REST API, Postman и Kibana"
        )
        self.assertIn("REST API", used)
        self.assertNotIn("Kibana", used)


if __name__ == "__main__":
    unittest.main()
