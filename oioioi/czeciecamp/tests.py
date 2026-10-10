from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from oioioi.base.tests import TestCase, fake_time
from oioioi.contests.models import Contest, ProblemInstance, Round, Submission
from oioioi.contests.scores import IntegerScore
from oioioi.czeciecamp.controllers import DAILY_KEY, MAIN_KEY, STALE_KEY
from oioioi.czeciecamp.forms import make_contest_id
from oioioi.czeciecamp.models import get_camp_config, ordered_rounds
from oioioi.czeciecamp.views import rounded_down, rounded_now
from oioioi.participants.models import Participant
from oioioi.problems.models import Problem

CONTROLLER = "oioioi.czeciecamp.controllers.CzecieCampContestController"
T0 = datetime(2020, 8, 1, 9, 0, tzinfo=UTC)


def _date(day, hour=0, minute=0):
    return T0 + timedelta(days=day, hours=hour, minutes=minute)


class CampTestCase(TestCase):
    fixtures = ["test_users"]

    def setUp(self):
        super().setUp()
        self.contest = Contest.objects.create(id="camp", name="Camp", controller_name=CONTROLLER)
        self.trial = Round.objects.create(
            contest=self.contest,
            name="Runda 0",
            start_date=_date(0),
            end_date=_date(0),
            results_date=_date(0),
            public_results_date=_date(0),
            is_trial=True,
        )
        self.round1 = self._round("Runda 1", 1)
        self.round2 = self._round("Runda 2", 2)
        self.pi_trial = self._problem(self.trial, "zero")
        self.pi1 = self._problem(self.round1, "abc")
        self.pi2 = self._problem(self.round2, "xyz")
        self.user1 = User.objects.get(username="test_user")
        self.user2 = User.objects.get(username="test_user2")
        self.outsider = User.objects.get(username="test_user3")
        for user in (self.user1, self.user2):
            Participant.objects.create(contest=self.contest, user=user)
        self.rcontroller = self.contest.controller.ranking_controller()

    def _round(self, name, day):
        return Round.objects.create(
            contest=self.contest,
            name=name,
            start_date=_date(day),
            end_date=_date(day, hour=5),
            results_date=_date(day, hour=5),
            public_results_date=_date(day, hour=6),
        )

    def _problem(self, round, short_name):
        problem = Problem.objects.create(
            legacy_name=short_name,
            short_name=short_name,
            controller_name="oioioi.problems.controllers.ProblemController",
        )
        return ProblemInstance.objects.create(problem=problem, round=round, contest=self.contest, short_name=short_name)

    def _submit(self, user, pi, date, score, kind="NORMAL"):
        return Submission.objects.create(user=user, problem_instance=pi, date=date, kind=kind, score=IntegerScore(score))

    def _ranking(self, partial_key, permission="regular"):
        return self.rcontroller.serialize_ranking(f"{permission}#{partial_key}#test")

    def _row(self, data, user):
        return next(row for row in data["rows"] if row["user"] == user)


class TestCampRankings(CampTestCase):
    def setUp(self):
        super().setUp()
        config = get_camp_config(self.contest)
        config.rankings_unfrozen = True
        config.save()

    def test_daily_and_stale(self):
        self._submit(self.user1, self.pi1, _date(1, hour=1), 50)
        self._submit(self.user1, self.pi1, _date(1, hour=7), 100)
        self._submit(self.user1, self.pi_trial, _date(0, hour=1), 30)

        self.assertEqual(self._row(self._ranking(DAILY_KEY), self.user1)["sum"], 50)
        self.assertEqual(self._row(self._ranking(STALE_KEY), self.user1)["sum"], 130)
        self.assertEqual(self._row(self._ranking(MAIN_KEY), self.user1)["sum"], 180)
        # Per-round rankings show Dzienne, the trial round shows Stałe.
        self.assertEqual(self._row(self._ranking(str(self.round1.id)), self.user1)["sum"], 50)
        self.assertEqual(self._row(self._ranking(str(self.trial.id)), self.user1)["sum"], 30)

        config = get_camp_config(self.contest)
        config.daily_weight = 2
        config.save()
        self.assertEqual(self._row(self._ranking(MAIN_KEY), self.user1)["sum"], 230)

    def test_dzienne_has_no_trial_problems(self):
        pis = [pi for pi, _visible in self._ranking(DAILY_KEY)["problem_instances"]]
        self.assertNotIn(self.pi_trial, pis)
        pis = [pi for pi, _visible in self._ranking(STALE_KEY)["problem_instances"]]
        self.assertIn(self.pi_trial, pis)

    def test_counting_end(self):
        config = get_camp_config(self.contest)
        config.counting_end_date = _date(3)
        config.save()
        self._submit(self.user1, self.pi1, _date(2), 40)
        self._submit(self.user1, self.pi1, _date(4), 100)
        self.assertEqual(self._row(self._ranking(STALE_KEY), self.user1)["sum"], 40)

    def test_ignored_submissions_do_not_count(self):
        self._submit(self.user1, self.pi1, _date(1, hour=1), 100, kind="IGNORED")
        self.assertEqual(self._row(self._ranking(STALE_KEY), self.user1)["sum"], 0)

    def test_only_participants_and_shared_places(self):
        self._submit(self.user1, self.pi1, _date(1, hour=1), 70)
        self._submit(self.user2, self.pi1, _date(1, hour=2), 70)
        self._submit(self.outsider, self.pi1, _date(1, hour=2), 100)
        rows = self._ranking(MAIN_KEY)["rows"]
        self.assertEqual([row["user"] for row in rows], [self.user1, self.user2])
        self.assertEqual([row["place"] for row in rows], [1, 1])

    def test_combined_cells_diff_and_medals(self):
        config = get_camp_config(self.contest)
        config.daily_weight = 2
        config.save()
        self._submit(self.user1, self.pi1, _date(1, hour=1), 30)
        self._submit(self.user2, self.pi1, _date(1, hour=7), 50)
        data = self._ranking(MAIN_KEY)
        row1, row2 = self._row(data, self.user1), self._row(data, self.user2)
        cell = row1["cells"][[pi for pi, _visible in data["problem_instances"]].index(self.pi1)]
        self.assertEqual((cell["weighted_daily"], cell["stale"], cell["value"]), (60, 30, 90))
        self.assertEqual((row1["diff"], row1["medal"]), (0, 1))
        self.assertEqual((row2["diff"], row2["medal"]), (-40, 2))
        self.assertTrue(data["is_aggregate"])
        self.assertFalse(self._ranking(str(self.round1.id))["is_aggregate"])

    def test_unpublished_round_is_hidden(self):
        self.round2.public_results_date = None
        self.round2.save()
        self._submit(self.user1, self.pi2, _date(2, hour=1), 100)
        self.assertEqual(self._row(self._ranking(MAIN_KEY), self.user1)["sum"], 0)
        self.assertEqual(self._row(self._ranking(MAIN_KEY, "admin"), self.user1)["sum"], 200)

    def test_available_rankings_order(self):
        request = RequestFactory().get("/")
        request.user = self.user1
        request.contest = self.contest
        request.timestamp = _date(10)
        keys = [key for key, _name in self.rcontroller.available_rankings(request)]
        expected = [MAIN_KEY, STALE_KEY, DAILY_KEY, str(self.trial.id), str(self.round1.id), str(self.round2.id)]
        self.assertEqual(keys, expected)


class TestCampFreeze(CampTestCase):
    def test_freeze(self):
        self._submit(self.user1, self.pi1, _date(1, hour=1), 50)
        # After the last round ended, frozen for participants.
        self._submit(self.user1, self.pi1, _date(3), 100)

        self.assertEqual(self._row(self._ranking(STALE_KEY), self.user1)["sum"], 50)
        self.assertEqual(self._row(self._ranking(MAIN_KEY), self.user1)["sum"], 100)
        self.assertIsNotNone(self._ranking(STALE_KEY)["frozen_at"])
        self.assertEqual(self._row(self._ranking(STALE_KEY, "admin"), self.user1)["sum"], 100)

        config = get_camp_config(self.contest)
        config.rankings_unfrozen = True
        config.save()
        self.assertEqual(self._row(self._ranking(STALE_KEY), self.user1)["sum"], 100)
        self.assertIsNone(self._ranking(STALE_KEY)["frozen_at"])

    def test_full_key_changes_on_freeze(self):
        request = RequestFactory().get("/")
        request.user = self.user1
        request.contest = self.contest
        request.timestamp = _date(2, hour=7)
        frozen_key = self.rcontroller.get_full_key(request, MAIN_KEY)
        config = get_camp_config(self.contest)
        config.rankings_unfrozen = True
        config.save()
        self.assertNotEqual(frozen_key, self.rcontroller.get_full_key(request, MAIN_KEY))

    def test_custom_freeze_date(self):
        config = get_camp_config(self.contest)
        config.freeze_date = _date(1, hour=3)
        config.save()
        self._submit(self.user1, self.pi1, _date(1, hour=1), 50)
        self._submit(self.user1, self.pi1, _date(1, hour=4), 100)
        self.assertEqual(self._row(self._ranking(STALE_KEY), self.user1)["sum"], 50)
        self.assertEqual(self._row(self._ranking(STALE_KEY, "admin"), self.user1)["sum"], 100)


class TestCampSubmitting(CampTestCase):
    def _can_submit(self, user, pi, timestamp):
        request = RequestFactory().get("/")
        request.user = user
        request.contest = self.contest
        request.timestamp = timestamp
        return self.contest.controller.can_submit(request, pi)

    def test_can_submit_after_round(self):
        self.assertFalse(self._can_submit(self.user1, self.pi2, _date(1, hour=1)))
        self.assertTrue(self._can_submit(self.user1, self.pi2, _date(2, hour=1)))
        self.assertTrue(self._can_submit(self.user1, self.pi2, _date(30)))
        self.assertTrue(self._can_submit(self.user1, self.pi_trial, _date(5)))

    def test_outsider_cannot_submit(self):
        self.assertFalse(self._can_submit(self.outsider, self.pi1, _date(1, hour=1)))


class TestCampPanel(CampTestCase):
    def _url(self, name, **kwargs):
        return reverse(name, kwargs=dict(contest_id=self.contest.id, **kwargs))

    def test_panel_permissions(self):
        self.assertTrue(self.client.login(username="test_user"))
        self.assertEqual(self.client.get(self._url("czeciecamp_panel")).status_code, 403)
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(self._url("czeciecamp_panel"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Runda 2")

    def test_round_buttons(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.round2.results_date = None
        self.round2.public_results_date = None
        self.round2.save()
        with fake_time(_date(2, hour=6, minute=4)):
            self.client.post(self._url("czeciecamp_round_action", round_id=self.round2.id, action="show_ranking"))
        self.round2.refresh_from_db()
        self.assertEqual(self.round2.results_date, _date(2, hour=6))
        self.assertEqual(self.round2.public_results_date, _date(2, hour=6))
        self.client.post(self._url("czeciecamp_round_action", round_id=self.round2.id, action="hide_ranking"))
        self.round2.refresh_from_db()
        self.assertIsNone(self.round2.public_results_date)

    def test_freeze_date_form(self):
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(self._url("czeciecamp_panel"))
        self.assertEqual(response.context["freeze_form"].initial["freeze_date"], self.round2.end_date)
        self.client.post(self._url("czeciecamp_panel"), {"action": "freeze_date", "freeze_date": "2020-08-03T20:00"})
        self.assertEqual(get_camp_config(self.contest).freeze_date, timezone.make_aware(datetime(2020, 8, 3, 20, 0)))
        last_end = timezone.localtime(self.round2.end_date).strftime("%Y-%m-%dT%H:%M")
        self.client.post(self._url("czeciecamp_panel"), {"action": "freeze_date", "freeze_date": last_end})
        self.assertIsNone(get_camp_config(self.contest).freeze_date)

    def test_freeze_buttons(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(self._url("czeciecamp_unfreeze"))
        self.assertTrue(get_camp_config(self.contest).rankings_unfrozen)
        self.client.post(self._url("czeciecamp_freeze"))
        self.assertFalse(get_camp_config(self.contest).rankings_unfrozen)

    def test_add_participants(self):
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.post(self._url("czeciecamp_panel"), {"action": "participants", "users": "test_user3\nnobody"})
        self.assertContains(response, "nobody")
        self.assertFalse(Participant.objects.filter(contest=self.contest, user=self.outsider).exists())
        self.client.post(self._url("czeciecamp_panel"), {"action": "participants", "users": "test_user3\n"})
        self.assertTrue(Participant.objects.filter(contest=self.contest, user=self.outsider).exists())

    def _round_data(self, prefix, name, start, end="", is_trial=False):
        data = {f"{prefix}-name": name, f"{prefix}-start_date": start, f"{prefix}-end_date": end}
        if is_trial:
            data[f"{prefix}-is_trial"] = "on"
        return data

    def test_panel_defaults_new_round_to_rounded_now(self):
        self.assertTrue(self.client.login(username="test_admin"))
        with fake_time(datetime(2020, 8, 10, 9, 41, 30, tzinfo=UTC)):
            response = self.client.get(self._url("czeciecamp_panel"))
        form = response.context["new_round_form"]
        self.assertEqual(form.initial["start_date"], datetime(2020, 8, 10, 9, 45, tzinfo=UTC))
        self.assertEqual(form.initial["name"], "Dzień 3")

    def test_add_round(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(
            self._url("czeciecamp_add_round"),
            self._round_data("new", "Próbna", "2020-08-04T09:00", "2020-08-04T14:00", is_trial=True),
        )
        new_round = Round.objects.get(contest=self.contest, name="Próbna")
        self.assertEqual(new_round.start_date, timezone.make_aware(datetime(2020, 8, 4, 9, 0)))
        self.assertEqual(new_round.end_date, timezone.make_aware(datetime(2020, 8, 4, 14, 0)))
        self.assertTrue(new_round.is_trial)
        self.assertEqual(new_round.results_date, new_round.start_date)

    def test_add_round_validation(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(self._url("czeciecamp_add_round"), self._round_data("new", "Runda 1", "2020-08-04T09:00"))
        self.client.post(self._url("czeciecamp_add_round"), self._round_data("new", "Zła", "2020-08-04T09:00", "2020-08-04T08:00"))
        self.assertEqual(Round.objects.filter(contest=self.contest).count(), 3)

    def _all_rounds_data(self, rounds):
        data = {}
        for round in rounds:
            start = timezone.localtime(round.start_date).strftime("%Y-%m-%dT%H:%M")
            end = timezone.localtime(round.end_date).strftime("%Y-%m-%dT%H:%M")
            data.update(self._round_data(f"r{round.id}", round.name, start, end, round.is_trial))
        data["order"] = [str(round.id) for round in rounds]
        return data

    def test_save_rounds(self):
        self.assertTrue(self.client.login(username="test_admin"))
        data = self._all_rounds_data([self.trial, self.round1, self.round2])
        data.update(self._round_data(f"r{self.round1.id}", "Dzień 1", "2020-08-02T10:00", "2020-08-02T16:00"))
        self.client.post(self._url("czeciecamp_save_rounds"), data)
        self.round1.refresh_from_db()
        self.assertEqual(self.round1.name, "Dzień 1")
        self.assertEqual(self.round1.end_date, timezone.make_aware(datetime(2020, 8, 2, 16, 0)))

    def test_save_rounds_is_all_or_nothing(self):
        self.assertTrue(self.client.login(username="test_admin"))
        data = self._all_rounds_data([self.trial, self.round1, self.round2])
        data.update(self._round_data(f"r{self.round1.id}", "Nowa nazwa", "2020-08-02T10:00", "2020-08-02T16:00"))
        data.update(self._round_data(f"r{self.round2.id}", "Zła", "2020-08-03T10:00", "2020-08-03T09:00"))
        self.client.post(self._url("czeciecamp_save_rounds"), data)
        self.round1.refresh_from_db()
        self.assertEqual(self.round1.name, "Runda 1")

    def test_delete_round(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(self._url("czeciecamp_delete_round", round_id=self.round2.id))
        self.assertFalse(Round.objects.filter(id=self.round2.id).exists())
        self.pi2.refresh_from_db()
        self.assertIsNone(self.pi2.round)

    def test_move_round(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(self._url("czeciecamp_save_rounds"), self._all_rounds_data([self.trial, self.round2, self.round1]))
        self.assertEqual(ordered_rounds(self.contest), [self.trial, self.round2, self.round1])
        request = RequestFactory().get("/")
        request.user = self.user1
        request.contest = self.contest
        request.timestamp = _date(10)
        keys = [key for key, _name in self.rcontroller.available_rankings(request)]
        self.assertEqual(keys[3:], [str(self.trial.id), str(self.round2.id), str(self.round1.id)])
        pis = [pi for pi, _visible in self.rcontroller.serialize_ranking("admin#main#test")["problem_instances"]]
        self.assertEqual(pis, [self.pi_trial, self.pi2, self.pi1])

    def test_ranking_view(self):
        self._submit(self.user1, self.pi1, _date(1, hour=1), 50)
        self.assertTrue(self.client.login(username="test_user"))
        response = self.client.get(self._url("default_ranking"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dzienne + Stałe")


class TestCreateCamp(TestCase):
    fixtures = ["test_users"]

    def test_create(self):
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.post(
            reverse("czeciecamp_create"),
            {
                "name": "Obóz Łódź 2026",
                "contest_id": "",
                "start_date": "2026-08-03T20:00",
                "days": 3,
                "results_visible_during_round": "on",
                "daily_weight": 2,
            },
        )
        contest = Contest.objects.get(id="oboz-lodz-2026")
        self.assertRedirects(response, reverse("czeciecamp_panel", kwargs={"contest_id": "oboz-lodz-2026"}))
        self.assertEqual(contest.controller_name, CONTROLLER)
        config = get_camp_config(contest)
        self.assertEqual(config.daily_weight, 2)
        self.assertTrue(config.results_visible_during_round)
        self.assertEqual(contest.default_submissions_limit, 25)
        rounds = list(contest.round_set.order_by("start_date"))
        self.assertEqual([r.name for r in rounds], ["Dzień 0", "Dzień 1", "Dzień 2", "Dzień 3"])
        self.assertTrue(rounds[0].is_trial)
        self.assertEqual(rounds[0].start_date, timezone.make_aware(datetime(2026, 8, 3, 20, 0)))
        self.assertEqual(rounds[3].start_date, timezone.make_aware(datetime(2026, 8, 6, 20, 0)))

    def test_create_limits_days(self):
        self.assertTrue(self.client.login(username="test_admin"))
        data = {"name": "Duży", "start_date": "2026-08-03T20:00", "days": 1000, "daily_weight": 1}
        self.client.post(reverse("czeciecamp_create"), data)
        self.assertFalse(Contest.objects.filter(id="duzy").exists())

    def test_create_default_start(self):
        self.assertTrue(self.client.login(username="test_admin"))
        with fake_time(datetime(2026, 8, 3, 10, 17, tzinfo=UTC)):
            response = self.client.get(reverse("czeciecamp_create"))
        start = timezone.localtime(response.context["form"].initial["start_date"])
        self.assertEqual((start.hour, start.minute), (20, 0))
        self.assertEqual(response.context["form"].fields["name"].initial, None)

    def test_only_superuser(self):
        self.assertTrue(self.client.login(username="test_user"))
        self.assertEqual(self.client.get(reverse("czeciecamp_create")).status_code, 403)


class TestRoundedNow(TestCase):
    def test_rounded_down(self):
        self.assertEqual(rounded_down(datetime(2020, 1, 1, 9, 44, 59, tzinfo=UTC)), datetime(2020, 1, 1, 9, 40, tzinfo=UTC))
        self.assertEqual(rounded_down(datetime(2020, 1, 1, 9, 45, tzinfo=UTC)), datetime(2020, 1, 1, 9, 45, tzinfo=UTC))

    def test_rounded_now(self):
        self.assertEqual(rounded_now(datetime(2020, 1, 1, 9, 41, 30, tzinfo=UTC)), datetime(2020, 1, 1, 9, 45, tzinfo=UTC))
        self.assertEqual(rounded_now(datetime(2020, 1, 1, 9, 45, tzinfo=UTC)), datetime(2020, 1, 1, 9, 45, tzinfo=UTC))
        self.assertEqual(rounded_now(datetime(2020, 1, 1, 9, 45, 1, tzinfo=UTC)), datetime(2020, 1, 1, 9, 50, tzinfo=UTC))
        self.assertEqual(rounded_now(datetime(2020, 1, 1, 23, 58, tzinfo=UTC)), datetime(2020, 1, 2, 0, 0, tzinfo=UTC))


class TestMakeContestId(TestCase):
    def test_make_contest_id(self):
        self.assertEqual(make_contest_id("Obóz OI – grupa starsza"), "oboz-oi-grupa-starsza")
        self.assertEqual(make_contest_id("Łódź 2026"), "lodz-2026")


class TestResultsVisibleDuringRound(CampTestCase):
    def test_results_visible(self):
        submission = self._submit(self.user1, self.pi1, _date(1, hour=1), 50)
        request = RequestFactory().get("/")
        request.user = self.user1
        request.contest = self.contest
        request.timestamp = _date(1, hour=2)
        controller = self.contest.controller
        self.assertFalse(controller.results_visible(request, submission))
        config = get_camp_config(self.contest)
        config.results_visible_during_round = True
        config.save()
        self.assertTrue(controller.results_visible(request, submission))
