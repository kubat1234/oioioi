"""Property tests of the ranking math of a camp.

Instead of chosen examples, each test states a rule that must hold for every
camp and checks it on many random ones: a few rounds (some "All-time", some
without an end, some not published), problems, participants and submissions
placed on and around every boundary (round start and end, counting end,
freeze), with hidden and disqualified participants and rankings of random
weights.

Two kinds of rules:

* agreement with a reference written straight from the rules of a camp, as
  slow and plain as possible (``expected_best``, ``expected_ranking``);
* relations between rankings which need no reference: weights are linear,
  rounds add up, what a viewer may not see does not change what they see.

The cases are random but repeatable: they come from ``CAMP_PROPERTY_SEED``.
A failure names the seed of its case; to look at that case alone run with
``CAMP_PROPERTY_SEED=<seed> CAMP_PROPERTY_CASES=1``.
"""

import math
import os
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.db import transaction

from oioioi.base.tests import TestCase, fake_timezone_now
from oioioi.contests.models import Contest, ProblemInstance, Round, Submission, UserResultForProblem
from oioioi.contests.scores import IntegerScore
from oioioi.czeciecamp.controllers import after_freeze_text, best_submissions, is_daily_submission
from oioioi.czeciecamp.models import (
    MEDALS_IOI,
    MEDALS_NONE,
    MEDALS_PODIUM,
    SCORING_BEST,
    SCORING_LAST,
    CampRanking,
    get_camp_config,
    medal_places,
)
from oioioi.disqualification.models import Disqualification
from oioioi.participants.models import Participant
from oioioi.problems.models import Problem
from oioioi.programs.utils import UnequalMaxScores, min_group_scorer, sum_group_scorer, sum_score_aggregator, threshold_linear_test_scorer
from oioioi.rankings.controllers import DefaultRankingController

CONTROLLER = "oioioi.czeciecamp.controllers.CzecieCampContestController"
SEED = int(os.environ.get("CAMP_PROPERTY_SEED", "20261116"))
# Camps built in the database, per test.
CASES = int(os.environ.get("CAMP_PROPERTY_CASES", "25"))
# Cases of the tests which do not touch the database.
PURE_CASES = int(os.environ.get("CAMP_PROPERTY_PURE_CASES", "400"))
T0 = datetime(2020, 8, 1, 9, 0, tzinfo=UTC)
DAY = 24 * 60
SCORES = (0, 0, 1, 10, 50, 99, 100, 100)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def _earliest_best(submissions):
    """(points, id) of the first of the best ``submissions``, given in the order they were sent."""
    top = max(submission.score.to_int() for submission in submissions)
    return next((top, submission.id) for submission in submissions if submission.score.to_int() == top)


def _points(result):
    return result[0] if result else 0


# ---------------------------------------------------------------------------
# Which submission counts: best_submissions, without a database.
# ---------------------------------------------------------------------------


class BestCase:
    """Arguments of ``best_submissions`` for a few rounds and users."""

    def __init__(self, rng):
        self.rounds = []
        moments = set()
        for i in range(rng.randint(1, 3)):
            start = i * DAY + rng.choice((0, 30))
            is_trial = rng.random() < 0.25
            end = start if is_trial else rng.choice((start + 300, start + 300, None))
            self.rounds.append(SimpleNamespace(id=i + 1, start_date=_at(start), end_date=None if end is None else _at(end), is_trial=is_trial))
            for boundary in (start, start + 300):
                moments.update((boundary - 1, boundary, boundary + 1))
            moments.update((start + 100, start + 200, start + 700))
        self.moments = sorted(moments)
        self.pi_by_id = {10 + i: SimpleNamespace(id=10 + i, round=rng.choice(self.rounds)) for i in range(rng.randint(1, 4))}
        self.counting_end = rng.choice((None, None, _at(rng.choice(self.moments))))
        # A freeze only ever moves the end of Stałe earlier.
        earlier = [m for m in self.moments if self.counting_end is None or _at(m) <= self.counting_end]
        self.stale_end = rng.choice((self.counting_end, self.counting_end, _at(rng.choice(earlier))))
        self.submissions = []
        for i in rng.sample(range(1, 41), rng.randint(0, 30)):
            self.submissions.append(self.submission(rng, i))
        self.sort()

    def submission(self, rng, id, user_id=None, scored=False):
        score = None if not scored and rng.random() < 0.15 else IntegerScore(rng.choice(SCORES))
        return SimpleNamespace(
            id=id,
            user_id=user_id or rng.randint(1, 3),
            problem_instance_id=rng.choice(sorted(self.pi_by_id)),
            date=_at(rng.choice(self.moments)),
            score=score,
        )

    def sort(self):
        # The order rankings read submissions in.
        self.submissions.sort(key=lambda submission: (submission.date, submission.id))

    def best(self, last_daily=False, submissions=None, counting_end="same", stale_end="same"):
        return best_submissions(
            self.submissions if submissions is None else submissions,
            self.pi_by_id,
            self.counting_end if counting_end == "same" else counting_end,
            self.stale_end if stale_end == "same" else stale_end,
            last_daily,
            lambda submission: submission.score,
        )

    def __repr__(self):
        lines = [f"counting_end={self.counting_end} stale_end={self.stale_end}"]
        lines += [f"round {r.id}: {r.start_date} - {r.end_date} trial={r.is_trial}" for r in self.rounds]
        lines += [f"problem {pi.id} in round {pi.round.id}" for pi in self.pi_by_id.values()]
        lines += [f"submission {s.id}: user {s.user_id} problem {s.problem_instance_id} {s.date} {s.score!r}" for s in self.submissions]
        return "\n".join(lines)


def expected_best(case, last_daily):
    """The rules of Dzienne and Stałe, cell by cell."""
    daily, stale = {}, {}
    for key in {(s.user_id, s.problem_instance_id) for s in case.submissions}:
        round = case.pi_by_id[key[1]].round
        counted = [
            s
            for s in case.submissions
            if (s.user_id, s.problem_instance_id) == key
            and s.score is not None
            and s.date >= round.start_date
            and (case.counting_end is None or s.date <= case.counting_end)
        ]
        in_stale = [s for s in counted if case.stale_end is None or s.date <= case.stale_end]
        in_daily = [s for s in counted if not round.is_trial and (round.end_date is None or s.date <= round.end_date)]
        if in_stale:
            stale[key] = _earliest_best(in_stale)
        if in_daily:
            daily[key] = (in_daily[-1].score.to_int(), in_daily[-1].id) if last_daily else _earliest_best(in_daily)
    return daily, stale


class TestBestSubmissionsProperties(TestCase):
    def _cases(self):
        for seed in range(SEED, SEED + PURE_CASES):
            rng = random.Random(seed)
            case = BestCase(rng)
            with self.subTest(seed=seed, case=case):
                yield rng, case

    def test_agrees_with_the_rules(self):
        for _rng, case in self._cases():
            for last_daily in (False, True):
                self.assertEqual(case.best(last_daily), expected_best(case, last_daily))

    def test_all_time_is_at_least_daily(self):
        """Without a freeze, whatever counts in Dzienne counts in Stałe too."""
        for _rng, case in self._cases():
            daily, stale = case.best(stale_end=case.counting_end)
            for key, (points, _id) in daily.items():
                self.assertGreaterEqual(stale[key][0], points)

    def test_another_submission_never_takes_points_away(self):
        """Counting the best submission, sending one more cannot lower any cell."""
        for rng, case in self._cases():
            before_daily, before_stale = case.best()
            case.submissions.append(case.submission(rng, 100))
            case.sort()
            after_daily, after_stale = case.best()
            for before, after in ((before_daily, after_daily), (before_stale, after_stale)):
                for key, (points, _id) in before.items():
                    self.assertGreaterEqual(after[key][0], points)

    def test_better_scores_never_take_points_away(self):
        """Counting the best submission, a rejudge which only raises scores cannot lower any cell."""
        for rng, case in self._cases():
            before_daily, before_stale = case.best()
            for submission in case.submissions:
                if submission.score is not None and rng.random() < 0.5:
                    submission.score = IntegerScore(submission.score.to_int() + rng.randint(0, 20))
            after_daily, after_stale = case.best()
            for before, after in ((before_daily, after_daily), (before_stale, after_stale)):
                for key, (points, _id) in before.items():
                    self.assertGreaterEqual(after[key][0], points)

    def test_users_do_not_affect_each_other(self):
        for _rng, case in self._cases():
            for last_daily in (False, True):
                daily, stale = case.best(last_daily)
                for user_id in (1, 2, 3):
                    own = [s for s in case.submissions if s.user_id == user_id]
                    own_daily, own_stale = case.best(last_daily, submissions=own)
                    self.assertEqual(own_daily, {key: value for key, value in daily.items() if key[0] == user_id})
                    self.assertEqual(own_stale, {key: value for key, value in stale.items() if key[0] == user_id})

    def test_only_the_counted_submissions_matter(self):
        """Dropping every submission which is not shown in any cell changes nothing (best scoring)."""
        for _rng, case in self._cases():
            daily, stale = case.best()
            shown = {id for _points, id in [*daily.values(), *stale.values()]}
            self.assertEqual(case.best(submissions=[s for s in case.submissions if s.id in shown]), (daily, stale))

    def test_nothing_after_the_counting_end_matters(self):
        for rng, case in self._cases():
            if case.counting_end is None:
                continue
            for last_daily in (False, True):
                before = case.best(last_daily)
                late = [case.submission(rng, 200 + i, scored=True) for i in range(5)]
                for submission in late:
                    submission.date = case.counting_end + timedelta(minutes=rng.choice((1, 5, 3000)))
                    submission.score = IntegerScore(1000)
                submissions = sorted([*case.submissions, *late], key=lambda s: (s.date, s.id))
                self.assertEqual(case.best(last_daily, submissions=submissions), before)

    def test_a_cell_never_shows_more_than_was_scored(self):
        for _rng, case in self._cases():
            by_id = {s.id: s for s in case.submissions}
            for last_daily in (False, True):
                for cells in case.best(last_daily):
                    for (user_id, pi_id), (points, submission_id) in cells.items():
                        submission = by_id[submission_id]
                        self.assertEqual((submission.user_id, submission.problem_instance_id), (user_id, pi_id))
                        self.assertEqual(submission.score.to_int(), points)

    def test_daily_window(self):
        """Both ends of a round belong to it; an "All-time" round has no Dzienne."""
        for _rng, case in self._cases():
            for round in case.rounds:
                self.assertFalse(is_daily_submission(round, round.start_date - timedelta(seconds=1)))
                self.assertEqual(is_daily_submission(round, round.start_date), not round.is_trial)
                if round.end_date is None:
                    self.assertTrue(is_daily_submission(round, round.start_date + timedelta(days=1000)))
                else:
                    self.assertEqual(is_daily_submission(round, round.end_date), not round.is_trial)
                    self.assertFalse(is_daily_submission(round, round.end_date + timedelta(seconds=1)))


# ---------------------------------------------------------------------------
# Places, medals and the scores of tests.
# ---------------------------------------------------------------------------


class TestPlacesAndMedalsProperties(TestCase):
    def test_place_is_one_more_than_the_number_of_better_rows(self):
        controller = DefaultRankingController(None)
        for seed in range(SEED, SEED + PURE_CASES):
            rng = random.Random(seed)
            rows = [{"id": i, "sum": rng.choice((-5, 0, 0, 10, 10, 50, 100, rng.randint(0, 300)))} for i in range(rng.randint(0, 12))]
            sums = [row["sum"] for row in rows]
            controller._assign_places(rows, lambda row: row["sum"])
            with self.subTest(seed=seed, sums=sums):
                self.assertEqual(sorted(sums, reverse=True), [row["sum"] for row in rows])
                for row in rows:
                    self.assertEqual(row["place"], 1 + sum(1 for other in sums if other > row["sum"]))

    def test_medal_places(self):
        previous = (0, 0, 0)
        for participants in range(0, 400):
            gold, silver, bronze = medal_places(MEDALS_IOI, participants)
            with self.subTest(participants=participants):
                self.assertTrue(0 <= gold <= silver <= bronze)
                # Never fewer medals for more participants.
                self.assertTrue(all(now >= before for now, before in zip((gold, silver, bronze), previous, strict=True)))
                previous = (gold, silver, bronze)
                # 1/12, 1/6 and 1/4 of the participants, each rounded up.
                self.assertTrue(participants / 12 <= gold < participants / 12 + 1)
                self.assertTrue(participants / 6 <= silver - gold < participants / 6 + 1)
                self.assertTrue(participants / 4 <= bronze - silver < participants / 4 + 1)
                if participants % 12 == 0:
                    self.assertEqual(bronze, participants // 2)
                self.assertEqual(medal_places(MEDALS_PODIUM, participants), (1, 2, 3))
                self.assertEqual(medal_places(MEDALS_NONE, participants), (0, 0, 0))

    def test_after_freeze_text(self):
        """Polish plural forms: 1 zgłoszenie, 2-4 zgłoszenia (but 12-14 zgłoszeń), 5+ zgłoszeń."""
        known = {1: "zgłoszenie", 2: "zgłoszenia", 4: "zgłoszenia", 5: "zgłoszeń", 11: "zgłoszeń", 12: "zgłoszeń", 14: "zgłoszeń"}
        known.update({21: "zgłoszeń", 22: "zgłoszenia", 25: "zgłoszeń", 100: "zgłoszeń", 102: "zgłoszenia", 112: "zgłoszeń", 1001: "zgłoszeń"})
        for count in range(1, 2000):
            text = after_freeze_text(count)
            self.assertTrue(text.startswith(f"Po zamrożeniu: {count} zgłosze"))
            noun = text.split()[-1]
            if count in known:
                self.assertEqual(noun, known[count])
            # The form depends only on the last two digits (above 1).
            if count >= 100 and count % 100 != 1:
                self.assertEqual(noun, after_freeze_text(count % 100 + 100).split()[-1])


class TestTestScorerProperties(TestCase):
    """The scorer of a camp: full points up to half of the time limit, then linearly down to 1."""

    def _score(self, max_score, limit, used, status="OK", percentage=None):
        result = {"result_code": status, "time_used": used}
        if percentage is not None:
            result["result_percentage"] = (percentage, 1)
        score, test_max, status = threshold_linear_test_scorer({"max_score": max_score, "exec_time_limit": limit}, result)
        self.assertEqual(test_max.to_int(), max_score)
        return score.to_int(), status

    def test_threshold_linear_scorer(self):
        for seed in range(SEED, SEED + PURE_CASES):
            rng = random.Random(seed)
            max_score = rng.choice((0, 1, 2, 10, 100, rng.randint(1, 1000)))
            limit = rng.choice((1, 2, 100, 1000, 1001, rng.randint(1, 20000)))
            times = sorted({0, 1, limit // 2, limit // 2 + 1, limit - 1, limit, limit + 1, *(rng.randint(0, 2 * limit) for _ in range(20))})
            times = [used for used in times if used >= 0]
            with self.subTest(seed=seed, max_score=max_score, limit=limit):
                previous = None
                for used in times:
                    score, status = self._score(max_score, limit, used)
                    self.assertTrue(0 <= score <= max_score)
                    if used > limit:
                        self.assertEqual((score, status), (0, "TLE"))
                    else:
                        self.assertEqual(status, "OK")
                        # Within the limit a correct answer is always worth something.
                        self.assertGreaterEqual(score, min(1, max_score))
                    if 2 * used <= limit:
                        self.assertEqual(score, max_score)
                    # Slower is never better.
                    if previous is not None:
                        self.assertLessEqual(score, previous)
                    previous = score
                    # A wrong answer is worth nothing, however fast.
                    for failed in ("WA", "RE", "TLE", "MLE"):
                        self.assertEqual(self._score(max_score, limit, used, failed), (0, failed))
                    # A checker's percentage never gives more than the whole test.
                    partial, _status = self._score(max_score, limit, used, percentage=rng.randint(0, 100))
                    self.assertTrue(0 <= partial <= score)
                # Without a time limit only the answer counts.
                self.assertEqual(self._score(max_score, 0, rng.randint(0, 10**6)), (max_score, "OK"))

    def test_group_scorers(self):
        for seed in range(SEED, SEED + PURE_CASES):
            rng = random.Random(seed)
            max_score = rng.randint(0, 100)
            scores = [rng.randint(0, max_score) for _ in range(rng.randint(1, 8))]
            results = {
                str(i): {"score": IntegerScore(score).serialize(), "max_score": IntegerScore(max_score).serialize(), "status": "OK", "order": i}
                for i, score in enumerate(scores)
            }
            with self.subTest(seed=seed, scores=scores, max_score=max_score):
                score, group_max, _status = min_group_scorer(results)
                self.assertEqual((score.to_int(), group_max.to_int()), (min(scores), max_score))
                score, group_max, _status = sum_group_scorer(results)
                self.assertEqual((score.to_int(), group_max.to_int()), (sum(scores), max_score * len(scores)))
                score, group_max, _status = sum_score_aggregator(results)
                self.assertEqual((score.to_int(), group_max.to_int()), (sum(scores), max_score * len(scores)))
                # The order of tests does not matter.
                shuffled = list(results.items())
                rng.shuffle(shuffled)
                self.assertEqual(min_group_scorer(dict(shuffled))[0].to_int(), min(scores))
                # Tests of one group must be worth the same.
                if len(scores) > 1:
                    results["0"]["max_score"] = IntegerScore(max_score + 1).serialize()
                    with self.assertRaises(UnequalMaxScores):
                        min_group_scorer(results)


# ---------------------------------------------------------------------------
# Whole rankings, on camps built in the database.
# ---------------------------------------------------------------------------


class Camp:
    """A random camp. ``now`` is the moment its rankings are looked at."""

    def __init__(self, rng, seed, weights=(-1, 0, 1, 1, 2, 3), freeze=True, disqualify=True, published=None):
        self.rng = rng
        self.contest = Contest.objects.create(id=f"prop{seed}", name=f"Camp {seed}", controller_name=CONTROLLER)
        self.rounds = []
        self.pis = []
        moments = set()
        for i in range(rng.randint(1, 4)):
            start = i * DAY
            is_trial = rng.random() < 0.2
            end = start if is_trial else rng.choice((start + 300, start + 300, start + 300, None))
            public = rng.choice((start + 360, start + 360, start + 360, None, 100 * DAY)) if published is None else start + 360
            round = Round.objects.create(
                contest=self.contest,
                name=f"Runda {i}",
                start_date=_at(start),
                end_date=None if end is None else _at(end),
                results_date=_at(start + 300),
                public_results_date=None if public is None else _at(public),
                is_trial=is_trial,
            )
            self.rounds.append(round)
            for boundary in (start, start + 300, start + 360):
                moments.update((boundary - 1, boundary, boundary + 1))
            moments.update((start + 100, start + 200, start + 800))
            for j in range(rng.randint(1, 2)):
                short_name = f"r{i}p{j}"
                problem = Problem.objects.create(legacy_name=short_name, short_name=short_name, controller_name="oioioi.problems.controllers.ProblemController")
                self.pis.append(ProblemInstance.objects.create(problem=problem, round=round, contest=self.contest, short_name=short_name))
        self.moments = sorted(moments)

        self.users = []
        for i in range(rng.randint(2, 6)):
            user = User.objects.create(username=f"prop{seed}u{i}", first_name=f"F{i}", last_name=f"L{rng.randint(0, 3)}")
            Participant.objects.create(contest=self.contest, user=user)
            self.users.append(user)
        self.outsider = User.objects.create(username=f"prop{seed}out")

        self.config = get_camp_config(self.contest)
        self.config.counting_end_date = rng.choice((None, None, self.moment()))
        if freeze:
            self.config.freeze_date = rng.choice((None, None, self.moment()))
            self.config.rankings_unfrozen = rng.random() < 0.25
        else:
            self.config.rankings_unfrozen = True
        self.config.hidden_users = [user.id for user in self.users if rng.random() < 0.15]
        self.config.save()

        self.next_date_offset = 0
        for _ in range(rng.randint(0, 30)):
            self.submit()
        if disqualify:
            if rng.random() < 0.3:
                Disqualification.objects.create(contest=self.contest, user=rng.choice(self.users), title="t", content="c", guilty=rng.random() < 0.8)
            for submission in Submission.objects.filter(problem_instance__contest=self.contest, user__in=self.users):
                if rng.random() < 0.04:
                    Disqualification.objects.create(
                        contest=self.contest, user=submission.user, submission=submission, title="t", content="c", guilty=rng.random() < 0.8
                    )

        custom = CampRanking.objects.create(
            contest=self.contest,
            name="Custom",
            order=10,
            daily_scoring=rng.choice((SCORING_BEST, SCORING_LAST)),
            medals=rng.choice((MEDALS_NONE, MEDALS_PODIUM, MEDALS_IOI)),
        )
        self.set_weights(custom, weights)
        custom.rounds.set(rng.sample(self.rounds, rng.randint(0, len(self.rounds))))
        self.custom = custom
        CampRanking.objects.filter(contest=self.contest, name="Dzienne").update(medals=MEDALS_IOI, daily_scoring=rng.choice((SCORING_BEST, SCORING_LAST)))
        self.now = _at(rng.choice((*self.moments, *self.moments, 50 * DAY, 50 * DAY, 200 * DAY)))

    def moment(self):
        return _at(self.rng.choice(self.moments))

    def set_weights(self, ranking, choices):
        while True:
            ranking.daily_weight, ranking.stale_weight = self.rng.choice(choices), self.rng.choice(choices)
            if ranking.daily_weight or ranking.stale_weight:
                break
        ranking.save()

    def submit(self, user=None, pi=None, date=None, score="random", kind=None):
        rng = self.rng
        if score == "random":
            score = None if rng.random() < 0.1 else rng.choice(SCORES)
        return Submission.objects.create(
            user=user or rng.choice((*self.users, *self.users, *self.users, self.outsider)),
            problem_instance=pi or rng.choice(self.pis),
            date=date or self.moment(),
            kind=kind or rng.choice(("NORMAL",) * 9 + ("IGNORED",)),
            score=None if score is None else IntegerScore(score),
        )

    def rankings(self):
        return list(CampRanking.objects.filter(contest=self.contest).order_by("id"))

    def serialize(self, ranking, permission="regular"):
        """The ranking as rankingsd computes it, at ``now``."""
        controller = Contest.objects.get(id=self.contest.id).controller.ranking_controller()
        with fake_timezone_now(self.now):
            return controller.serialize_ranking(f"{permission}#{ranking.id}#test")

    def frozen_at(self):
        """When the rankings froze for participants, if they are frozen at ``now``."""
        config = get_camp_config(self.contest)
        if config.rankings_unfrozen:
            return None
        ends = [round.end_date for round in self.rounds if not round.is_trial and round.end_date is not None]
        freeze_date = config.freeze_date or max(ends, default=None)
        return freeze_date if freeze_date is not None and self.now >= freeze_date else None

    def __repr__(self):
        config = get_camp_config(self.contest)
        lines = [
            f"now={self.now} counting_end={config.counting_end_date} freeze={config.freeze_date}",
            f"unfrozen={config.rankings_unfrozen} hidden={config.hidden_users}",
        ]
        lines += [f"round {r.id}: {r.start_date} - {r.end_date} public={r.public_results_date} trial={r.is_trial}" for r in self.rounds]
        lines += [f"problem {pi.id} {pi.short_name} in round {pi.round_id}" for pi in self.pis]
        lines += [
            f"ranking {r.id} {r.name}: {r.daily_weight} x Dzienne ({r.daily_scoring}) + {r.stale_weight} x Stałe, medals {r.medals}, "
            f"round {r.round_id}, rounds {[x.id for x in r.rounds.all()]}"
            for r in self.rankings()
        ]
        lines += [f"participants {[user.id for user in self.users]}, outsider {self.outsider.id}"]
        lines += [
            f"submission {s.id}: user {s.user_id} problem {s.problem_instance_id} {s.date} {s.kind} {s.score!r}"
            for s in Submission.objects.filter(problem_instance__contest=self.contest).order_by("date", "id")
        ]
        lines += [f"disqualified: user {d.user_id} submission {d.submission_id} {d.guilty}" for d in Disqualification.objects.filter(contest=self.contest)]
        return "\n".join(lines)


def expected_ranking(camp, ranking, privileged):
    """The rules of a camp ranking, written down as plainly as possible.

    Returns the ids of the problems in the order of the columns and, for each
    user in the ranking, the numbers of their row.
    """
    config = get_camp_config(camp.contest)
    now = camp.now
    if ranking.round_id is not None:
        rounds = [ranking.round]
    else:
        rounds = list(ranking.rounds.all()) or list(camp.rounds)
    if not privileged:
        rounds = [round for round in rounds if round.public_results_date is not None and round.public_results_date <= now]
    rounds.sort(key=lambda round: (round.start_date, round.id))
    pis = []
    for round in rounds:
        if round.is_trial and not ranking.stale_weight:
            continue
        pis += sorted((pi for pi in camp.pis if pi.round_id == round.id), key=lambda pi: pi.short_name)

    counting_end = config.counting_end_date
    frozen_at = None if privileged else camp.frozen_at()
    stale_end = min((date for date in (counting_end, frozen_at) if date is not None), default=None)

    guilty = Disqualification.objects.filter(contest=camp.contest, guilty=True)
    disqualified_users = {d.user_id for d in guilty if d.submission_id is None}
    disqualified_cells = {(d.user_id, d.submission.problem_instance_id) for d in guilty if d.submission_id is not None}
    users = [user for user in camp.users if privileged or (user.id not in config.hidden_users and user.id not in disqualified_users)]

    rows = {}
    for user in users:
        cells = []
        for pi in pis:
            round = pi.round
            sent = list(Submission.objects.filter(user=user, problem_instance=pi, kind="NORMAL").order_by("date", "id"))
            disqualified = (user.id, pi.id) in disqualified_cells
            counted = [
                s for s in sent if not disqualified and s.score is not None and s.date >= round.start_date and (counting_end is None or s.date <= counting_end)
            ]
            in_stale = [s for s in counted if stale_end is None or s.date <= stale_end]
            in_daily = [s for s in counted if not round.is_trial and (round.end_date is None or s.date <= round.end_date)]
            stale = _earliest_best(in_stale)[0] if in_stale else None
            if not in_daily:
                daily = None
            elif ranking.daily_scoring == SCORING_LAST:
                daily = in_daily[-1].score.to_int()
            else:
                daily = _earliest_best(in_daily)[0]
            if ranking.daily_weight and ranking.stale_weight:
                value = None if daily is None and stale is None else ranking.daily_weight * (daily or 0) + ranking.stale_weight * (stale or 0)
            elif ranking.stale_weight:
                value = None if stale is None else ranking.stale_weight * stale
            else:
                value = None if daily is None else ranking.daily_weight * daily
            after_freeze = 0
            if frozen_at is not None and ranking.stale_weight and not disqualified:
                after_freeze = sum(1 for s in sent if s.date > frozen_at and (counting_end is None or s.date <= counting_end))
            cells.append({"daily": daily, "stale": stale, "value": value, "disqualified": disqualified, "after_freeze": after_freeze})
        daily_sum = sum(cell["daily"] or 0 for cell in cells)
        stale_sum = sum(cell["stale"] or 0 for cell in cells)
        rows[user.id] = {
            "cells": cells,
            "daily_sum": daily_sum,
            "stale_sum": stale_sum,
            "sum": ranking.daily_weight * daily_sum + ranking.stale_weight * stale_sum,
            "hidden": user.id in config.hidden_users,
            "disqualified": user.id in disqualified_users,
        }

    placed = [row for row in rows.values() if not row["hidden"] and not row["disqualified"]]
    leader = max((row["sum"] for row in placed), default=0)
    if ranking.medals == MEDALS_PODIUM:
        last_places = (1, 2, 3)
    elif ranking.medals == MEDALS_IOI:
        gold = math.ceil(len(placed) / 12)
        silver = gold + math.ceil(len(placed) / 6)
        last_places = (gold, silver, silver + math.ceil(len(placed) / 4))
    else:
        last_places = (0, 0, 0)
    for row in rows.values():
        row["place"] = None
        row["medal"] = None
        if row in placed:
            row["place"] = 1 + sum(1 for other in placed if other["sum"] > row["sum"])
            if row["sum"] > 0:
                row["medal"] = next((medal for medal, last in enumerate(last_places, start=1) if row["place"] <= last), None)
        row["diff"] = row["sum"] - leader
        row["percentage"] = 100 * row["sum"] / leader if leader else 0
    return [pi.id for pi in pis], rows


ROW_FIELDS = ("sum", "daily_sum", "stale_sum", "place", "medal", "diff", "percentage", "hidden", "disqualified")
CELL_FIELDS = ("daily", "stale", "value", "disqualified", "after_freeze")


def _numbers(data):
    """The numbers of a serialized ranking, in the shape ``expected_ranking`` returns."""
    rows = {}
    for row in data["rows"]:
        numbers = {field: row[field] for field in ROW_FIELDS}
        numbers["cells"] = [{field: cell[field] for field in CELL_FIELDS} for cell in row["cells"]]
        rows[row["user"].id] = numbers
    return [pi.id for pi, _visible in data["problem_instances"]], rows


def _points_of(data):
    """What a viewer learns about points: every number of every row, but not the count of submissions after the freeze."""
    pis, rows = _numbers(data)
    for row in rows.values():
        for cell in row["cells"]:
            del cell["after_freeze"]
    return pis, rows


def _all_time_points_of(data):
    """The same for a ranking of Stałe alone, which shows no Dzienne."""
    pis, rows = _points_of(data)
    for row in rows.values():
        del row["daily_sum"]
        for cell in row["cells"]:
            del cell["daily"]
    return pis, rows


def _sums(data):
    return {row["user"].id: row["sum"] for row in data["rows"]}


class CampPropertiesTestCase(TestCase):
    def _camps(self, subtests=True, **options):
        """Yields random camps; whatever a test does to one is rolled back."""
        for seed in range(SEED, SEED + CASES):
            with transaction.atomic():
                camp = Camp(random.Random(seed), seed, **options)
                if subtests:
                    with self.subTest(seed=seed, camp=camp):
                        yield camp
                else:
                    yield camp
                transaction.set_rollback(True)


class TestCampRankingProperties(CampPropertiesTestCase):
    def test_agrees_with_the_rules(self):
        """Every number of every ranking, for participants and for admins."""
        for camp in self._camps():
            for ranking in camp.rankings():
                for permission in ("regular", "admin", "observer"):
                    with self.subTest(ranking=ranking.name, permission=permission):
                        expected = expected_ranking(camp, ranking, privileged=permission != "regular")
                        self.assertEqual(_numbers(camp.serialize(ranking, permission)), expected)

    def test_rows_are_consistent(self):
        """What a ranking page shows adds up, whatever the rules are."""
        for camp in self._camps():
            for ranking in camp.rankings():
                for permission in ("regular", "admin"):
                    with self.subTest(ranking=ranking.name, permission=permission):
                        data = camp.serialize(ranking, permission)
                        rows = data["rows"]
                        self.assertEqual([row["sum"] for row in rows], sorted((row["sum"] for row in rows), reverse=True))
                        placed = [row for row in rows if row["place"] is not None]
                        for row in rows:
                            self.assertEqual(len(row["cells"]), len(data["problem_instances"]))
                            self.assertEqual(row["sum"], ranking.daily_weight * row["daily_sum"] + ranking.stale_weight * row["stale_sum"])
                            self.assertEqual(row["sum"], sum(cell["value"] or 0 for cell in row["cells"]))
                            self.assertEqual(row["daily_sum"], sum(cell["daily"] or 0 for cell in row["cells"]))
                            self.assertEqual(row["stale_sum"], sum(cell["stale"] or 0 for cell in row["cells"]))
                            for cell in row["cells"]:
                                if cell["value"] is None:
                                    self.assertIsNone(cell["url"])
                                elif permission == "admin":
                                    # A number links to the submission it comes from.
                                    self.assertIsNotNone(cell["url"])
                                if cell["disqualified"]:
                                    self.assertEqual((cell["daily"], cell["stale"], cell["value"], cell["after_freeze"]), (None, None, None, 0))
                            self.assertEqual(row["place"] is None, row["hidden"] or row["disqualified"])
                            if row["place"] is None:
                                self.assertIsNone(row["medal"])
                        for row in placed:
                            self.assertEqual(row["place"], 1 + sum(1 for other in placed if other["sum"] > row["sum"]))
                            self.assertLessEqual(row["diff"], 0)
                            self.assertLessEqual(row["percentage"], 100)
                            if row["medal"] is not None:
                                self.assertGreater(row["sum"], 0)
                            for other in placed:
                                # Equal points: equal place and medal. More points: never a worse medal.
                                if other["sum"] == row["sum"]:
                                    self.assertEqual((other["place"], other["medal"]), (row["place"], row["medal"]))
                                elif other["sum"] > row["sum"] > 0:
                                    self.assertIsNotNone(other["medal"] if row["medal"] is not None else 0)
                                    if row["medal"] is not None:
                                        self.assertLessEqual(other["medal"], row["medal"])
                        if placed:
                            self.assertEqual(min(row["place"] for row in placed), 1)
                            self.assertEqual(max(row["diff"] for row in placed), 0)
                        if not data["is_main"]:
                            self.assertTrue(not ranking.daily_weight or not ranking.stale_weight)

    def test_csv_shows_the_same_numbers(self):
        for camp in self._camps():
            controller = camp.contest.controller.ranking_controller()
            for ranking in camp.rankings():
                key = f"admin#{ranking.id}#test"
                data = camp.serialize(ranking, "admin")
                header = controller._get_csv_header(key, data)
                for row in data["rows"]:
                    line = controller._get_csv_row(key, row)
                    with self.subTest(ranking=ranking.name, user=row["user"].id):
                        self.assertEqual(len(line), len(header))
                        self.assertEqual(line[0], "" if row["place"] is None else row["place"])
                        self.assertEqual(line[1], row["user"].username)
                        self.assertEqual(line[4], row["sum"])
                        cells = line[7:] if row["combined"] else line[5:]
                        per_cell = 2 if row["combined"] else 1
                        self.assertEqual(len(cells), per_cell * len(row["cells"]))
                        for i, cell in enumerate(row["cells"]):
                            shown = cells[per_cell * i : per_cell * (i + 1)]
                            if cell["disqualified"]:
                                self.assertEqual(set(shown), {"DQ"})
                            elif row["combined"]:
                                self.assertEqual(shown, ["" if cell["daily"] is None else cell["daily"], "" if cell["stale"] is None else cell["stale"]])
                            else:
                                self.assertEqual(shown, ["" if cell["value"] is None else cell["value"]])

    def test_weights_are_linear(self):
        """a × Dzienne + b × Stałe is a times the Dzienne ranking plus b times the Stałe one, for every participant."""
        for camp in self._camps():
            ranking = camp.custom
            for permission in ("regular", "admin"):
                with self.subTest(permission=permission):
                    daily_weight, stale_weight = ranking.daily_weight, ranking.stale_weight
                    combined = _sums(camp.serialize(ranking, permission))
                    ranking.daily_weight, ranking.stale_weight = 1, 0
                    ranking.save()
                    daily = _sums(camp.serialize(ranking, permission))
                    ranking.daily_weight, ranking.stale_weight = 0, 1
                    ranking.save()
                    stale = _sums(camp.serialize(ranking, permission))
                    ranking.daily_weight, ranking.stale_weight = daily_weight, stale_weight
                    ranking.save()
                    self.assertEqual(combined, {user: daily_weight * daily[user] + stale_weight * stale[user] for user in combined})

    def test_scaled_weights_keep_the_order(self):
        """Multiplying both weights by the same positive number changes sums, but no place and no medal."""
        for camp in self._camps():
            ranking = camp.custom
            before = camp.serialize(ranking, "admin")
            factor = camp.rng.randint(2, 5)
            ranking.daily_weight *= factor
            ranking.stale_weight *= factor
            ranking.save()
            after = camp.serialize(ranking, "admin")
            self.assertEqual(_sums(after), {user: factor * points for user, points in _sums(before).items()})
            self.assertEqual(
                {row["user"].id: (row["place"], row["medal"], row["percentage"]) for row in after["rows"]},
                {row["user"].id: (row["place"], row["medal"], row["percentage"]) for row in before["rows"]},
            )

    def test_rounds_add_up(self):
        """A ranking of several rounds is the sum of the same ranking of each round alone."""
        for camp in self._camps():
            ranking = camp.custom
            for permission in ("regular", "admin"):
                with self.subTest(permission=permission):
                    rounds = list(ranking.rounds.all()) or camp.rounds
                    ranking.rounds.set(rounds)
                    together = _sums(camp.serialize(ranking, permission))
                    total = dict.fromkeys(together, 0)
                    for round in rounds:
                        ranking.rounds.set([round])
                        for user, points in _sums(camp.serialize(ranking, permission)).items():
                            total[user] += points
                    ranking.rounds.set(rounds)
                    self.assertEqual(together, total)

    def test_ranking_of_a_round_is_a_part_of_the_camp_ranking(self):
        """The Dzienne of the camp is the sum of the rankings of its rounds (an "All-time" round has none)."""
        for camp in self._camps():
            total = _sums(camp.serialize(CampRanking.objects.get(contest=camp.contest, name="Dzienne", round=None), "admin"))
            if CampRanking.objects.get(contest=camp.contest, name="Dzienne", round=None).daily_scoring != SCORING_BEST:
                continue
            by_rounds = dict.fromkeys(total, 0)
            for round in camp.rounds:
                if round.is_trial:
                    continue
                for user, points in _sums(camp.serialize(CampRanking.objects.get(round=round), "admin")).items():
                    by_rounds[user] += points
            self.assertEqual(total, by_rounds)

    def test_all_time_is_at_least_daily(self):
        """Nobody has more points in Dzienne than in Stałe, for admins (no freeze) and best scoring."""
        for camp in self._camps():
            stale = _sums(camp.serialize(CampRanking.objects.get(contest=camp.contest, name="Stałe", round=None), "admin"))
            ranking = CampRanking.objects.get(contest=camp.contest, name="Dzienne", round=None)
            ranking.daily_scoring = SCORING_BEST
            ranking.save()
            for user, points in _sums(camp.serialize(ranking, "admin")).items():
                self.assertLessEqual(points, stale[user])

    def test_all_time_cell_is_the_result_on_the_problems_list(self):
        """With nothing cut off, the Stałe points of a problem are the result the participant sees on the list of problems."""
        for camp in self._camps():
            config = get_camp_config(camp.contest)
            config.counting_end_date = None
            config.save()
            Disqualification.objects.filter(contest=camp.contest).delete()
            # The list of problems also counts submissions from before the round, which only admins can send.
            for submission in Submission.objects.filter(problem_instance__contest=camp.contest).select_related("problem_instance__round"):
                if submission.date < submission.problem_instance.round.start_date:
                    submission.delete()
            ranking = CampRanking.objects.get(contest=camp.contest, name="Stałe", round=None)
            pis, rows = _numbers(camp.serialize(ranking, "admin"))
            controller = camp.contest.controller
            for user in camp.users:
                for pi_id, cell in zip(pis, rows[user.id]["cells"], strict=True):
                    result = UserResultForProblem(user=user, problem_instance=ProblemInstance.objects.get(id=pi_id))
                    controller.update_user_result_for_problem(result)
                    self.assertEqual(cell["stale"], None if result.score is None else result.score.to_int())


class TestCampRankingVisibilityProperties(CampPropertiesTestCase):
    """What a viewer must not see must not change what they see."""

    def _all_points(self, camp, permission="regular"):
        return {ranking.id: _points_of(camp.serialize(ranking, permission)) for ranking in camp.rankings()}

    def _submit_everywhere(self, camp, dates):
        """A perfect submission of every participant to every problem at one of ``dates``."""
        if not dates:
            return 0
        count = 0
        for user in camp.users:
            for pi in camp.pis:
                camp.submit(user=user, pi=pi, date=camp.rng.choice(dates), score=10**6, kind="NORMAL")
                count += 1
        return count

    def test_participants_see_the_admin_ranking_without_the_hidden_people(self):
        """Not frozen and everything published: the same rows, and places as if the hidden and disqualified were not there."""
        for camp in self._camps(freeze=False, published=True):
            camp.now = _at(300 * DAY)
            config = get_camp_config(camp.contest)
            disqualified = set(Disqualification.objects.filter(contest=camp.contest, guilty=True, submission=None).values_list("user_id", flat=True))
            left_out = set(config.hidden_users) | disqualified
            for ranking in camp.rankings():
                with self.subTest(ranking=ranking.name):
                    pis, admin_rows = _numbers(camp.serialize(ranking, "admin"))
                    regular = _numbers(camp.serialize(ranking, "regular"))
                    self.assertEqual(regular, (pis, {user: row for user, row in admin_rows.items() if user not in left_out}))

    def test_hidden_participant_changes_nothing_for_the_others(self):
        """Hiding somebody, or taking them out of the camp, gives participants the same ranking."""
        for camp in self._camps(disqualify=False):
            user = camp.rng.choice(camp.users)
            config = get_camp_config(camp.contest)
            config.hidden_users = sorted({*config.hidden_users, user.id})
            config.save()
            hidden = self._all_points(camp)
            Participant.objects.filter(contest=camp.contest, user=user).delete()
            self.assertEqual(self._all_points(camp), hidden)

    def test_disqualified_participant_changes_nothing_for_the_others(self):
        for camp in self._camps():
            user = camp.rng.choice(camp.users)
            Disqualification.objects.create(contest=camp.contest, user=user, title="t", content="c")
            disqualified = self._all_points(camp)
            Participant.objects.filter(contest=camp.contest, user=user).delete()
            self.assertEqual(self._all_points(camp), disqualified)

    def test_outsiders_and_ignored_submissions_change_nothing(self):
        for camp in self._camps():
            before = {permission: self._all_points(camp, permission) for permission in ("regular", "admin")}
            for pi in camp.pis:
                camp.submit(user=camp.outsider, pi=pi, score=10**6, kind="NORMAL")
                camp.submit(user=camp.rng.choice(camp.users), pi=pi, score=10**6, kind="IGNORED")
                # Not judged yet.
                camp.submit(user=camp.rng.choice(camp.users), pi=pi, score=None, kind="NORMAL")
            self.assertEqual({permission: self._all_points(camp, permission) for permission in ("regular", "admin")}, before)

    def test_nothing_after_the_counting_end_counts(self):
        """For nobody, in no ranking."""
        for camp in self._camps():
            config = get_camp_config(camp.contest)
            if config.counting_end_date is None:
                config.counting_end_date = camp.moment()
                config.save()
            before = {permission: self._all_points(camp, permission) for permission in ("regular", "admin")}
            late = [config.counting_end_date + timedelta(minutes=minutes) for minutes in (1, 100, 5000)]
            self._submit_everywhere(camp, late)
            self.assertEqual({permission: self._all_points(camp, permission) for permission in ("regular", "admin")}, before)

    def test_nothing_before_the_round_counts(self):
        for camp in self._camps():
            before = {permission: self._all_points(camp, permission) for permission in ("regular", "admin")}
            for user in camp.users:
                for pi in camp.pis:
                    camp.submit(user=user, pi=pi, date=pi.round.start_date - timedelta(minutes=camp.rng.choice((1, 60))), score=10**6, kind="NORMAL")
            self.assertEqual({permission: self._all_points(camp, permission) for permission in ("regular", "admin")}, before)

    def test_unpublished_rounds_do_not_show_in_participants_rankings(self):
        """Whatever happens in a round which is not published changes nothing participants see."""
        for camp in self._camps():
            unpublished = [r for r in camp.rounds if r.public_results_date is None or r.public_results_date > camp.now]
            if not unpublished:
                continue
            before = self._all_points(camp)
            for user in camp.users:
                for pi in camp.pis:
                    if pi.round in unpublished:
                        camp.submit(user=user, pi=pi, date=pi.round.start_date + timedelta(minutes=camp.rng.choice((0, 10, 400))), score=10**6, kind="NORMAL")
            Submission.objects.filter(problem_instance__round__in=unpublished, id__in=camp.rng.sample(self._ids(camp), k=len(self._ids(camp)) // 2)).delete()
            self.assertEqual(self._all_points(camp), before)

    def _ids(self, camp):
        return list(Submission.objects.filter(problem_instance__contest=camp.contest).order_by("id").values_list("id", flat=True))

    def _freeze(self, camp):
        """Makes the camp frozen at ``now``; returns the moment of the freeze."""
        config = get_camp_config(camp.contest)
        config.rankings_unfrozen = False
        if config.freeze_date is None:
            config.freeze_date = camp.moment()
        config.save()
        camp.now = max(camp.now, config.freeze_date)
        self.assertEqual(camp.frozen_at(), config.freeze_date)
        return config.freeze_date

    def test_freeze_hides_all_time_points(self):
        """Participants of a frozen camp learn nothing about Stałe from submissions sent after the freeze;
        only their number is shown. Admins see the points right away.
        """
        for camp in self._camps():
            frozen_at = self._freeze(camp)
            stale_rankings = [ranking for ranking in camp.rankings() if not ranking.daily_weight]
            before = {ranking.id: camp.serialize(ranking) for ranking in stale_rankings}
            admin_before = {ranking.id: _sums(camp.serialize(ranking, "admin")) for ranking in stale_rankings}
            config = get_camp_config(camp.contest)
            late = [frozen_at + timedelta(minutes=minutes) for minutes in (1, 100, 5000)]
            late = [date for date in late if config.counting_end_date is None or date <= config.counting_end_date]
            sent = self._submit_everywhere(camp, late)
            for ranking in stale_rankings:
                with self.subTest(ranking=ranking.name):
                    after = camp.serialize(ranking)
                    self.assertEqual(_all_time_points_of(after), _all_time_points_of(before[ranking.id]))
                    self.assertEqual(after["frozen_at"], frozen_at)
                    # Every submission after the freeze is counted in its cell, if the round is shown.
                    for row_before, row_after in zip(
                        sorted(before[ranking.id]["rows"], key=lambda row: row["user"].id), sorted(after["rows"], key=lambda row: row["user"].id), strict=True
                    ):
                        for cell_before, cell_after in zip(row_before["cells"], row_after["cells"], strict=True):
                            added = 0 if cell_after["disqualified"] or not sent else 1
                            self.assertEqual(cell_after["after_freeze"], cell_before["after_freeze"] + added)
                    if sent and ranking.stale_weight > 0:
                        # Admins see the new points at once (a negative weight would turn them into a penalty).
                        admin_after = _sums(camp.serialize(ranking, "admin"))
                        self.assertTrue(all(admin_after[user] >= points for user, points in admin_before[ranking.id].items()))

    def test_freeze_does_not_change_what_admins_see(self):
        for camp in self._camps():
            config = get_camp_config(camp.contest)
            config.rankings_unfrozen = True
            config.save()
            unfrozen = self._all_points(camp, "admin")
            self._freeze(camp)
            now = camp.now
            self.assertEqual(self._all_points(camp, "admin"), unfrozen)
            self.assertEqual(camp.now, now)

    def test_unfreezing_shows_participants_what_admins_see(self):
        """After unfreezing, with everything published, participants see the admin numbers of everybody shown to them."""
        for camp in self._camps(published=True):
            self._freeze(camp)
            camp.now = _at(300 * DAY)
            config = get_camp_config(camp.contest)
            config.rankings_unfrozen = True
            config.save()
            for ranking in camp.rankings():
                _pis, admin_rows = _numbers(camp.serialize(ranking, "admin"))
                _pis, regular_rows = _numbers(camp.serialize(ranking, "regular"))
                for user, row in regular_rows.items():
                    self.assertEqual(
                        (row["sum"], row["daily_sum"], row["stale_sum"]), tuple(admin_rows[user][field] for field in ("sum", "daily_sum", "stale_sum"))
                    )

    @pytest.mark.xfail(strict=True, reason="Known: a freeze set before the end of a round does not freeze Dzienne (REVIEW.md 2.5)")
    def test_freeze_hides_daily_points(self):
        """The same as for Stałe: a submission sent after the freeze, while its round is still running, must not
        change the points participants see. Today Dzienne ignores the freeze, so it does.
        """
        failures = []
        # No subtests: each of them would be reported on its own as unexpectedly passing.
        for camp in self._camps(subtests=False):
            running = [round for round in camp.rounds if not round.is_trial and round.end_date is not None]
            if not running:
                continue
            round = camp.rng.choice(running)
            config = get_camp_config(camp.contest)
            config.rankings_unfrozen = False
            config.counting_end_date = None
            # Frozen in the middle of the round.
            config.freeze_date = round.start_date + timedelta(minutes=100)
            config.save()
            round.public_results_date = round.start_date
            round.save()
            camp.now = round.start_date + timedelta(minutes=200)
            before = self._all_points(camp)
            for user in camp.users:
                for pi in camp.pis:
                    if pi.round_id == round.id:
                        camp.submit(user=user, pi=pi, date=round.start_date + timedelta(minutes=150), score=10**6, kind="NORMAL")
            if self._all_points(camp) != before:
                failures.append(camp.contest.id)
        self.assertEqual(failures, [])


class TestCampRankingMonotonicityProperties(CampPropertiesTestCase):
    def test_another_submission_helps_only_its_sender(self):
        """With weights that are not negative and the best submission counting, one more submission never lowers
        the sender's sum or place, and changes nobody else's sum.
        """
        for camp in self._camps(weights=(0, 1, 1, 2, 3)):
            CampRanking.objects.filter(contest=camp.contest).update(daily_scoring=SCORING_BEST)
            before = {ranking.id: camp.serialize(ranking, "admin") for ranking in camp.rankings()}
            user = camp.rng.choice(camp.users)
            camp.submit(user=user, score=camp.rng.choice((0, 50, 100, 1000)), kind="NORMAL")
            for ranking in camp.rankings():
                with self.subTest(ranking=ranking.name):
                    after = camp.serialize(ranking, "admin")
                    old, new = _sums(before[ranking.id]), _sums(after)
                    self.assertGreaterEqual(new[user.id], old[user.id])
                    self.assertEqual({u: p for u, p in new.items() if u != user.id}, {u: p for u, p in old.items() if u != user.id})
                    old_places = {row["user"].id: row["place"] for row in before[ranking.id]["rows"]}
                    new_places = {row["user"].id: row["place"] for row in after["rows"]}
                    for other, place in new_places.items():
                        if place is None:
                            continue
                        if other == user.id:
                            self.assertLessEqual(place, old_places[other])
                        else:
                            self.assertGreaterEqual(place, old_places[other])

    def test_last_submission_scoring_counts_the_last_one(self):
        """With Dzienne counting the last submission, a later one during the round replaces the points, also with fewer."""
        for camp in self._camps(disqualify=False):
            config = get_camp_config(camp.contest)
            config.counting_end_date = None
            config.save()
            ranking = CampRanking.objects.get(contest=camp.contest, name="Dzienne", round=None)
            ranking.daily_scoring = SCORING_LAST
            ranking.save()
            rounds = [round for round in camp.rounds if not round.is_trial]
            if not rounds:
                continue
            round = camp.rng.choice(rounds)
            pi = camp.rng.choice([pi for pi in camp.pis if pi.round_id == round.id])
            user = camp.rng.choice(camp.users)
            Submission.objects.filter(user=user, problem_instance=pi, date__gte=round.start_date + timedelta(minutes=299)).delete()
            points = camp.rng.choice((0, 7, 100))
            camp.submit(user=user, pi=pi, date=round.start_date + timedelta(minutes=299), score=points, kind="NORMAL")
            pis, rows = _numbers(camp.serialize(ranking, "admin"))
            self.assertEqual(rows[user.id]["cells"][pis.index(pi.id)]["daily"], points)
