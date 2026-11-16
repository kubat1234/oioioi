"""End-to-end tests of what a camp is used for: two groups (two camps) on one
instance, participants submitting through the real views, rankings read over
HTTP. Judging itself is mocked; the rules are tested in ``tests.py``.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.urls import reverse

from oioioi.base.tests import TestCase, fake_time
from oioioi.contests.models import Contest, ContestPermission, ProblemInstance, Round, RoundTimeExtension, Submission
from oioioi.contests.scores import IntegerScore
from oioioi.czeciecamp.models import CampRanking, create_round_ranking, get_camp_config
from oioioi.participants.models import Participant
from oioioi.programs.models import Test, TestReport

CONTROLLER = "oioioi.czeciecamp.controllers.CzecieCampContestController"
T0 = datetime(2020, 8, 1, 9, 0, tzinfo=UTC)
JUDGE = "oioioi.programs.controllers.ProgrammingProblemController.judge"
# The score column of a list of submissions.
SCORE_CELL = r'-score">\s*%d\s*<'


def _date(day, hour=0, minute=0):
    return T0 + timedelta(days=day, hours=hour, minutes=minute)


class TwoCampsTestCase(TestCase):
    """Camp "c" (group A) with a real programming problem and camp "b"
    (group B) with an instance of the same problem.

    test_user is a participant of A, test_user2 of B, test_user3 of none.
    Round 1 of both camps runs on day 1 from 0:00 to 5:00.
    """

    fixtures = ["test_users", "test_contest", "test_full_package", "test_problem_instance"]

    def setUp(self):
        super().setUp()
        self.camp_a = self._make_camp(Contest.objects.get(id="c"))
        self.round_a = Round.objects.get(contest=self.camp_a)
        self._set_round_times(self.round_a)
        create_round_ranking(self.round_a)
        self.pi_a = ProblemInstance.objects.get(contest=self.camp_a)
        self.pi_a.submissions_limit = 0
        self.pi_a.save()

        self.camp_b = self._make_camp(Contest.objects.create(id="b", name="Group B", controller_name=CONTROLLER))
        self.round_b = Round.objects.create(contest=self.camp_b, name="Day 1", start_date=_date(1))
        self._set_round_times(self.round_b)
        self.pi_b = ProblemInstance.objects.create(problem=self.pi_a.problem, contest=self.camp_b, round=self.round_b, short_name="zad1")

        self.user_a = User.objects.get(username="test_user")
        self.user_b = User.objects.get(username="test_user2")
        self.outsider = User.objects.get(username="test_user3")
        Participant.objects.create(contest=self.camp_a, user=self.user_a)
        Participant.objects.create(contest=self.camp_b, user=self.user_b)
        CampRanking.objects.update(public_date=_date(0))

    def _make_camp(self, contest):
        contest.controller_name = CONTROLLER
        contest.save()
        return contest

    def _set_round_times(self, round):
        round.start_date = _date(1)
        round.end_date = _date(1, hour=5)
        round.results_date = _date(1, hour=5)
        round.public_results_date = _date(1, hour=6)
        round.save()

    def _submit(self, contest, pi, name="a.cpp"):
        url = reverse("submit", kwargs={"contest_id": contest.id})
        with patch(JUDGE) as judge:
            response = self.client.post(url, {"problem_instance_id": pi.id, "file": ContentFile(b"int main() {}", name=name)})
        return response, judge.call_count

    def _submissions_of_users(self, pi):
        # Adding a problem to a contest creates submissions of its model solutions, without a user.
        return Submission.objects.filter(problem_instance=pi, user__isnull=False)

    def _scored(self, user, pi, date, score):
        return Submission.objects.create(user=user, problem_instance=pi, date=date, kind="NORMAL", status="OK", score=IntegerScore(score))


class TestCampSubmitFlow(TwoCampsTestCase):
    def _count(self):
        return Submission.objects.filter(user=self.user_a, problem_instance=self.pi_a).count()

    def test_before_during_and_after_the_round(self):
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(0, hour=23)):
            self._submit(self.camp_a, self.pi_a)
        self.assertEqual(self._count(), 0)

        with fake_time(_date(1, hour=1)):
            response, judged = self._submit(self.camp_a, self.pi_a)
        self.assertRedirects(response, reverse("my_submissions", kwargs={"contest_id": "c"}), fetch_redirect_response=False)
        self.assertEqual((self._count(), judged), (1, 1))

        # Submitting stays open after the round.
        with fake_time(_date(3)):
            response, judged = self._submit(self.camp_a, self.pi_a)
        self.assertEqual((self._count(), judged), (2, 1))

    def test_outsider_and_anonymous_cannot_submit(self):
        with fake_time(_date(1, hour=1)):
            self._submit(self.camp_a, self.pi_a)
            self.assertTrue(self.client.login(username="test_user3"))
            response, judged = self._submit(self.camp_a, self.pi_a)
        self.assertEqual((response.status_code, judged), (403, 0))
        self.assertFalse(self._submissions_of_users(self.pi_a).exists())

    def test_round_limit_applies_only_during_the_round(self):
        config = get_camp_config(self.camp_a)
        config.default_round_submissions_limit = 2
        config.save()
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(1, hour=1)):
            for _i in range(3):
                self._submit(self.camp_a, self.pi_a)
        self.assertEqual(self._count(), 2)
        with fake_time(_date(1, hour=6)):
            self._submit(self.camp_a, self.pi_a)
        self.assertEqual(self._count(), 3)

    def test_submission_page_and_list(self):
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(1, hour=1)):
            self._submit(self.camp_a, self.pi_a)
        submission = self._submissions_of_users(self.pi_a).get()
        submission.status = "OK"
        submission.score = IntegerScore(100)
        submission.save()
        with fake_time(_date(1, hour=2)):
            response = self.client.get(reverse("my_submissions", kwargs={"contest_id": "c"}))
            self.assertEqual(response.status_code, 200)
            # Results are not shown before the results date.
            self.assertNotRegex(response.content.decode(), SCORE_CELL % 100)
        with fake_time(_date(1, hour=5, minute=1)):
            response = self.client.get(reverse("my_submissions", kwargs={"contest_id": "c"}))
            self.assertRegex(response.content.decode(), SCORE_CELL % 100)
            response = self.client.get(reverse("submission", kwargs={"contest_id": "c", "submission_id": submission.id}))
            self.assertEqual(response.status_code, 200)


class TestCampGroupsIsolation(TwoCampsTestCase):
    def test_participant_cannot_enter_the_other_group(self):
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(2)):
            for name in ("problems_list", "default_ranking", "my_submissions", "submit", "czeciecamp_panel"):
                response = self.client.get(reverse(name, kwargs={"contest_id": "b"}))
                self.assertEqual(response.status_code, 403, name)
            response, judged = self._submit(self.camp_b, self.pi_b)
            self.assertEqual((response.status_code, judged), (403, 0))
        self.assertFalse(self._submissions_of_users(self.pi_b).exists())

    def test_participant_cannot_submit_to_the_other_group_through_own_contest(self):
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(2)):
            _response, judged = self._submit(self.camp_a, self.pi_b)
        self.assertEqual(judged, 0)
        self.assertFalse(self._submissions_of_users(self.pi_b).exists())

    def test_participant_cannot_open_a_submission_of_the_other_group(self):
        submission = self._scored(self.user_b, self.pi_b, _date(1, hour=1), 100)
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(2)):
            for contest_id in ("b", "c"):
                for name in ("submission", "download_submission_source"):
                    response = self.client.get(reverse(name, kwargs={"contest_id": contest_id, "submission_id": submission.id}))
                    self.assertIn(response.status_code, (403, 404), (contest_id, name))

    def test_contest_list_shows_only_own_group(self):
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(2)):
            response = self.client.get(reverse("select_contest"), follow=True)
            self.assertNotContains(response, "Group B")
            contests = self.client.get(reverse("api_contest_list")).json()
        self.assertEqual([contest["id"] for contest in contests], ["c"])

    def test_admin_of_one_group_cannot_manage_the_other(self):
        ContestPermission.objects.create(user=self.user_a, contest=self.camp_a, permission="contests.contest_admin")
        ranking_b = CampRanking.objects.filter(contest=self.camp_b, round=None).first()
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(2)):
            self.assertEqual(self.client.get(reverse("czeciecamp_panel", kwargs={"contest_id": "c"})).status_code, 200)
            self.assertEqual(self.client.get(reverse("czeciecamp_panel", kwargs={"contest_id": "b"})).status_code, 403)

            # Objects of group B addressed through the panel of group A.
            urls = [
                reverse("czeciecamp_round_action", kwargs={"contest_id": "c", "round_id": self.round_b.id, "action": "hide_results"}),
                reverse("czeciecamp_delete_round", kwargs={"contest_id": "c", "round_id": self.round_b.id}),
                reverse("czeciecamp_ranking_action", kwargs={"contest_id": "c", "ranking_id": ranking_b.id, "action": "delete"}),
                reverse("czeciecamp_hide_participant", kwargs={"contest_id": "c", "user_id": self.user_b.id}),
            ]
            for url in urls:
                self.assertEqual(self.client.post(url).status_code, 404, url)
            url = reverse("czeciecamp_rejudge_report", kwargs={"contest_id": "c", "problem_instance_id": self.pi_b.id})
            self.assertEqual(self.client.get(url).status_code, 404)
            url = reverse("czeciecamp_edit_ranking", kwargs={"contest_id": "c", "ranking_id": ranking_b.id})
            self.assertEqual(self.client.get(url).status_code, 404)

        self.round_b.refresh_from_db()
        self.assertIsNotNone(self.round_b.results_date)
        self.assertTrue(CampRanking.objects.filter(id=ranking_b.id).exists())
        self.assertEqual(get_camp_config(self.camp_b).hidden_users, [])

    def test_api_respects_groups_and_round_start(self):
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(0, hour=12)):
            # Round 1 has not started: nothing to list.
            self.assertEqual(self.client.get(reverse("api_problem_list", args=("c",))).json(), [])
            self.assertEqual(self.client.get(reverse("api_round_list", args=("c",))).json(), [])
        with fake_time(_date(1, hour=1)):
            problems = self.client.get(reverse("api_problem_list", args=("c",))).json()
            self.assertEqual([p["short_name"] for p in problems], ["zad1"])
            for name in ("api_problem_list", "api_round_list"):
                self.assertEqual(self.client.get(reverse(name, args=("b",))).status_code, 403, name)


# A number alone in an element, as the points in a cell of a ranking.
CELL = r">\s*%d\s*<"


class TestCampRankingPages(TwoCampsTestCase):
    def _ranking_page(self, contest_id="c"):
        ranking = CampRanking.objects.get(contest_id=contest_id, round=None, name="Stałe")
        return self.client.get(reverse("ranking", kwargs={"contest_id": contest_id, "key": str(ranking.id)}))

    def test_round_appears_when_published_and_freezes_after_the_last_round(self):
        self._scored(self.user_a, self.pi_a, _date(1, hour=1), 40)
        self.assertTrue(self.client.login(username="test_user"))
        # Rankings are serialized outside of a request, at the real current time.
        with fake_time(_date(1, hour=5, minute=30)), patch("django.utils.timezone.now", return_value=_date(1, hour=5, minute=30)):
            # Personal results are out, the round is not in rankings yet.
            response = self._ranking_page()
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "zad1")

        with fake_time(_date(1, hour=7)), patch("django.utils.timezone.now", return_value=_date(1, hour=7)):
            response = self._ranking_page()
            self.assertContains(response, "zad1")
            self.assertRegex(response.content.decode(), CELL % 40)

        # Sent after the freeze (the end of the last round): only admins see its points.
        self._scored(self.user_a, self.pi_a, _date(1, hour=8), 100)
        with fake_time(_date(1, hour=9)), patch("django.utils.timezone.now", return_value=_date(1, hour=9)):
            response = self._ranking_page()
            self.assertRegex(response.content.decode(), CELL % 40)
            self.assertNotRegex(response.content.decode(), CELL % 100)
            self.assertTrue(self.client.login(username="test_admin"))
            self.assertRegex(self._ranking_page().content.decode(), CELL % 100)

    def test_rankings_of_groups_are_separate(self):
        self._scored(self.user_a, self.pi_a, _date(1, hour=1), 40)
        self._scored(self.user_b, self.pi_b, _date(1, hour=1), 70)
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(1, hour=7)), patch("django.utils.timezone.now", return_value=_date(1, hour=7)):
            response = self._ranking_page()
            self.assertContains(response, "Test User")
            self.assertNotContains(response, "Test User 2")
            self.assertNotRegex(response.content.decode(), CELL % 70)
            ranking_b = CampRanking.objects.get(contest=self.camp_b, round=None, name="Stałe")
            # A ranking of group B is not reachable through the URL of group A either.
            response = self.client.get(reverse("ranking", kwargs={"contest_id": "c", "key": str(ranking_b.id)}))
            self.assertEqual(response.status_code, 404)

    def test_csv_only_for_admins(self):
        ranking = CampRanking.objects.get(contest=self.camp_a, round=None, name="Stałe")
        url = reverse("ranking_csv", kwargs={"contest_id": "c", "key": str(ranking.id)})
        self._scored(self.user_a, self.pi_a, _date(1, hour=1), 40)
        with fake_time(_date(2)):
            self.assertTrue(self.client.login(username="test_user"))
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertTrue(self.client.login(username="test_admin"))
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("test_user", response.content.decode())


class TestCampEmailLogin(TwoCampsTestCase):
    def test_login_with_email_then_submit(self):
        self.user_a.set_password("secret-pass")
        self.user_a.email = "Uczestnik@Example.com"
        self.user_a.save()
        self.assertTrue(self.client.login(username="uczestnik@example.COM", password="secret-pass"))
        with fake_time(_date(1, hour=1)):
            _response, judged = self._submit(self.camp_a, self.pi_a)
        self.assertEqual(judged, 1)
        self.assertEqual(Submission.objects.get(problem_instance=self.pi_a).user, self.user_a)


class TestCampTimeExtension(TwoCampsTestCase):
    """A participant with extra time may submit "during the round" for longer."""

    def test_submission_in_extra_time_is_accepted(self):
        RoundTimeExtension.objects.create(user=self.user_a, round=self.round_a, extra_time=30)
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(1, hour=5, minute=20)):
            _response, judged = self._submit(self.camp_a, self.pi_a)
        self.assertEqual(judged, 1)


class TestCampPartialRejudge(TwoCampsTestCase):
    """A rejudge of a whole problem on new or selected tests, through the
    admin's form, with real judging: what it does to the submission, to the
    ranking and to the rejudge report.
    """

    CODE = b'#include <stdio.h>\nint main() { int a, b; scanf("%d %d", &a, &b); printf("%d\\n", a + b); return 0; }\n'

    def setUp(self):
        super().setUp()
        self.admin = User.objects.get(username="test_user3")
        ContestPermission.objects.create(user=self.admin, contest=self.camp_a, permission="contests.contest_admin")
        # A camp gives fewer points for slow runs; keep the timing of a loaded machine out of the test.
        Test.objects.filter(problem_instance=self.pi_a).update(time_limit=20000)
        self.ranking = CampRanking.objects.get(contest=self.camp_a, name="Stałe", round=None)

    def _points(self):
        data = self.camp_a.controller.ranking_controller().serialize_ranking(f"admin#{self.ranking.id}#test")
        return next(row["sum"] for row in data["rows"] if row["user"] == self.user_a)

    def _rejudge(self, **data):
        self.assertTrue(self.client.login(username="test_user3"))
        url = reverse("rejudge_all_submissions_for_problem", kwargs={"contest_id": "c", "problem_instance_id": self.pi_a.id})
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        report_url = reverse("czeciecamp_rejudge_report", kwargs={"contest_id": "c", "problem_instance_id": self.pi_a.id})
        return self.client.get(report_url)

    def _tests_of(self, submission):
        reports = TestReport.objects.filter(submission_report__submission=submission, submission_report__status="ACTIVE", submission_report__kind="NORMAL")
        return {report.test_name: report for report in reports}

    def test_new_tests_then_selected_tests(self):
        hidden = Test.objects.get(problem_instance=self.pi_a, name="2")
        hidden.is_active = False
        hidden.save()
        self.assertTrue(self.client.login(username="test_user"))
        with fake_time(_date(1, hour=1)):
            url = reverse("submit", kwargs={"contest_id": "c"})
            self.client.post(url, {"problem_instance_id": self.pi_a.id, "file": ContentFile(self.CODE, name="a.c")})
        submission = self._submissions_of_users(self.pi_a).get()
        self.assertEqual(submission.score, IntegerScore(67))
        self.assertEqual(self._points(), 67)
        before = self._tests_of(submission)
        self.assertNotIn("2", before)

        # The test comes back; only it is judged, the others keep their results.
        hidden.is_active = True
        hidden.save()
        response = self._rejudge(rejudge_type="NEW")
        submission.refresh_from_db()
        after = self._tests_of(submission)
        self.assertEqual(submission.score, IntegerScore(100))
        self.assertEqual(self._points(), 100)
        self.assertEqual(set(after), {*before, "2"})
        for name, report in before.items():
            self.assertEqual((after[name].status, after[name].score, after[name].time_used), (report.status, report.score, report.time_used))
        self.assertContains(response, "67 → 100")
        self.assertContains(response, "Stałe")

        # Selected tests of an unchanged submission: judged again, nothing changes.
        response = self._rejudge(rejudge_type="SELECTED", tests=["1a", "2"])
        submission.refresh_from_db()
        self.assertEqual(submission.score, IntegerScore(100))
        self.assertEqual(self._points(), 100)
        self.assertContains(response, "The rejudge has not changed anything.")
        self.assertEqual(set(self._tests_of(submission)), set(after))

    def test_selected_tests_must_be_chosen_and_belong_to_the_problem(self):
        self.assertTrue(self.client.login(username="test_user3"))
        url = reverse("rejudge_all_submissions_for_problem", kwargs={"contest_id": "c", "problem_instance_id": self.pi_a.id})
        with patch(JUDGE) as judge:
            for tests in ([], ["no-such-test"]):
                response = self.client.post(url, {"rejudge_type": "SELECTED", "tests": tests})
                self.assertEqual(response.status_code, 200)
            self.assertEqual(judge.call_count, 0)
