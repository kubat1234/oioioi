"""Property tests of LLM detection: whatever a contest looks like, assessing
it never fails and the staff pages open.

The panel assesses the whole contest when it is opened, so one submission
the scoring cannot cope with would lock the staff out of the panel of that
contest. The contests here are random but made of everything that is unusual:
empty, binary and huge sources, sources missing from the storage, problems
without a round, rounds without an end, submissions without a score, one
participant, everybody sending the same code, earlier contests, references.

The cases come from ``LLMDETECT_PROPERTY_SEED``; a failure names its seed.
"""

import math
import os
import random
from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse

from oioioi.base.tests import TestCase
from oioioi.contests.models import Contest, ContestPermission, ProblemInstance, Round
from oioioi.contests.scores import IntegerScore
from oioioi.llmdetect.models import LLMReferenceSolution, ProblemViewEvent, ReviewDecision, RiskAssessment, get_config
from oioioi.llmdetect.scoring import assess_contest, contest_submissions, user_rankings
from oioioi.participants.models import Participant
from oioioi.problems.models import Problem
from oioioi.programs.models import ProgramSubmission

SEED = int(os.environ.get("LLMDETECT_PROPERTY_SEED", "20261116"))
CASES = int(os.environ.get("LLMDETECT_PROPERTY_CASES", "20"))
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
CONTROLLER = "oioioi.czeciecamp.controllers.CzecieCampContestController"

LLM_LIKE = """#include <iostream>
#include <vector>

// Reads the numbers and prints their sum.
int main() {
    std::ios::sync_with_stdio(false);
    int count;
    std::cin >> count;
    std::vector<long long> values(count);
    long long total = 0;
    for (int index = 0; index < count; ++index) {
        std::cin >> values[index];
        total += values[index];
    }
    std::cout << total << '\\n';
    return 0;
}
"""
HUMAN_LIKE = """#include <bits/stdc++.h>
using namespace std;
#define ll long long
int n;ll s,a[1000007];
int main(){
    scanf("%d",&n);
    for(int i=0;i<n;i++){scanf("%lld",&a[i]);s+=a[i];}
    //cerr<<s<<endl;
    printf("%lld\\n",s);
}
"""
PYTHON = "n = int(input())\nprint(sum(int(x) for x in input().split()))\n"
WORDS = "ans cnt res tab dist vis que sum best cur nxt lo hi mid dp pref suf val idx tmp".split()


def _source(rng):
    kind = rng.random()
    if kind < 0.04:
        return b"", "a.cpp"
    if kind < 0.08:
        return bytes(rng.randrange(256) for _ in range(rng.randint(1, 400))), "a.cpp"
    if kind < 0.11:
        return rng.choice((b"\n" * 500, b" " * 3000, b"}" * 2000, b"/*" * 1500, ("ż" * 900).encode(), b"\x00" * 300, b"int main(){}")), "a.cpp"
    if kind < 0.14:
        return (LLM_LIKE * 150).encode(), "a.cpp"
    if kind < 0.2:
        return PYTHON.encode(), "a.py"
    if kind < 0.25:
        return ("```cpp\n" + LLM_LIKE + "```\nHere is the solution. I hope this helps!\n").encode(), "a.cpp"
    base = rng.choice((LLM_LIKE, HUMAN_LIKE))
    lines = []
    for i in range(rng.randint(0, 25)):
        a, b, c = rng.sample(WORDS, 3)
        lines.append(
            rng.choice(
                (f"    for (int {a} = 0; {a} < {b}; ++{a}) {c} += {i};", f"    if ({a} > {b}) {c} = {a} - {b};", f"    long long {a}{i} = {b} % {i + 2};")
            )
        )
    code = base.replace("int main", "\n".join(lines) + "\nint main", 1) if rng.random() < 0.5 else base
    if rng.random() < 0.2:
        code = code.replace("\n", "\r\n")
    return code.encode(), rng.choice(("a.cpp", "a.cpp", "pasted_code.cpp", "A.CPP", "a.c", "noextension"))


class RandomContest:
    def __init__(self, rng, name, users, with_history=None):
        self.contest = Contest.objects.create(id=name, name=name, controller_name=CONTROLLER)
        self.admin = User.objects.create(username=f"{name}-admin")
        ContestPermission.objects.create(user=self.admin, contest=self.contest, permission="contests.contest_admin")
        self.users = users
        for user in users:
            Participant.objects.create(contest=self.contest, user=user)
        rounds = [None]
        for i in range(rng.randint(0, 3)):
            start = T0 + timedelta(days=i)
            rounds.append(
                Round.objects.create(
                    contest=self.contest,
                    name=f"R{i}",
                    start_date=start,
                    end_date=rng.choice((start + timedelta(hours=5), start + timedelta(hours=5), None, start)),
                    is_trial=rng.random() < 0.2,
                )
            )
        self.pis = []
        for i in range(rng.randint(1, 4)):
            problem = Problem.objects.create(
                legacy_name=f"{name}{i}", short_name=f"{name}{i}", controller_name="oioioi.sinolpack.controllers.SinolProblemController"
            )
            self.pis.append(ProblemInstance.objects.create(problem=problem, round=rng.choice(rounds), contest=self.contest, short_name=f"p{i}"))
        same = _source(rng)
        self.submissions = []
        for _ in range(rng.randint(0, 25)):
            pi = rng.choice(self.pis)
            data, filename = same if rng.random() < 0.3 else _source(rng)
            submission = ProgramSubmission(
                user=rng.choice((*users, *users, self.admin, None)),
                problem_instance=pi,
                kind=rng.choice(("NORMAL",) * 8 + ("IGNORED", "SUSPECTED")),
                status=rng.choice(("OK", "OK", "CE", "WA", "?", "SE")),
                score=rng.choice((None, IntegerScore(0), IntegerScore(50), IntegerScore(100))),
                date=T0 + timedelta(minutes=rng.choice((-60, 0, 1, 30, 299, 300, 301, 1440, 5000))),
            )
            submission.source_file.save(filename, ContentFile(data))
            submission.save()
            if rng.random() < 0.05:
                # Lost from the storage.
                submission.source_file.storage.delete(submission.source_file.name)
            self.submissions.append(submission)
            if submission.user_id is not None and rng.random() < 0.3:
                ProblemViewEvent.objects.get_or_create(
                    user=submission.user, problem_instance=pi, defaults={"date": submission.date - timedelta(minutes=rng.choice((-5, 0, 3, 200)))}
                )
        for pi in self.pis:
            for _ in range(rng.choice((0, 0, 1, 5))):
                data, filename = _source(rng)
                LLMReferenceSolution.objects.create(
                    problem_instance=pi, source=data.decode("utf-8", "replace").replace("\x00", ""), filename=filename, model_name="m"
                )
        config = get_config(self.contest)
        config.time_from_statement = rng.random() < 0.5
        config.history_weight = rng.choice((0.05, 0.5, 1.0))
        config.save()
        config.trusted_rounds.set([r for r in rounds if r is not None and rng.random() < 0.3])
        if with_history is not None:
            config.history_contests.set([with_history.contest])
        with_user = [s for s in self.submissions if s.user_id is not None]
        for submission in rng.sample(with_user, min(len(with_user), rng.randint(0, 2))):
            ReviewDecision.objects.create(
                contest=self.contest, user=submission.user, submission=submission, verdict=rng.choice(("clear", "talk", "confirmed")), reviewer=self.admin
            )


def _numbers(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int | float):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _numbers(item)


class TestAssessmentProperties(TestCase):
    def _contests(self):
        for seed in range(SEED, SEED + CASES):
            rng = random.Random(seed)
            with transaction.atomic():
                users = [User.objects.create(username=f"u{seed}-{i}", first_name="F", last_name=f"L{i}") for i in range(rng.randint(1, 5))]
                earlier = RandomContest(rng, f"old{seed}", users[: rng.randint(1, len(users))])
                contest = RandomContest(rng, f"new{seed}", users, with_history=earlier if rng.random() < 0.6 else None)
                with self.subTest(seed=seed):
                    yield rng, contest
                transaction.set_rollback(True)

    def test_assessing_never_fails_and_risks_are_probabilities(self):
        for _rng, case in self._contests():
            assess_contest(case.contest)
            assessed = {a.submission_id: a for a in RiskAssessment.objects.filter(contest=case.contest)}
            expected = set(contest_submissions(case.contest).values_list("id", flat=True))
            # Only participants' own submissions of this contest, each at most once.
            self.assertLessEqual(set(assessed), expected)
            config = get_config(case.contest)
            # Whatever is missing could not be read (the count also covers earlier contests).
            self.assertLessEqual(len(expected) - len(assessed), config.read_errors)
            if not config.history_contests.exists():
                self.assertEqual(len(expected) - len(assessed), config.read_errors)
            for assessment in assessed.values():
                self.assertTrue(0.0 <= assessment.risk <= 1.0)
                self.assertEqual(assessment.user_id, assessment.submission.user_id)
                for number in _numbers([assessment.signals, assessment.context]):
                    self.assertTrue(math.isfinite(number))
            for row in user_rankings(case.contest):
                self.assertTrue(math.isfinite(row["score"]))
                self.assertIn(row["user_id"], {user.id for user in case.users})

    def test_assessing_twice_gives_the_same_risks(self):
        for _rng, case in self._contests():
            assess_contest(case.contest)
            first = {a.submission_id: (a.risk, a.signals) for a in RiskAssessment.objects.filter(contest=case.contest)}
            assess_contest(case.contest)
            second = {a.submission_id: (a.risk, a.signals) for a in RiskAssessment.objects.filter(contest=case.contest)}
            self.assertEqual(first, second)

    def test_staff_pages_open(self):
        for _rng, case in self._contests():
            self.client.force_login(case.admin)
            kwargs = {"contest_id": case.contest.id}
            self.assertEqual(self.client.get(reverse("llmdetect_panel", kwargs=kwargs)).status_code, 200)
            for user in case.users:
                url = reverse("llmdetect_user", kwargs={**kwargs, "user_id": user.id})
                self.assertEqual(self.client.get(url).status_code, 200)
                for assessment in RiskAssessment.objects.filter(contest=case.contest, user=user):
                    self.assertEqual(self.client.get(url, {"submission": assessment.submission_id}).status_code, 200)
            for submission in case.submissions:
                if submission.user_id is not None:
                    others = [s for s in case.submissions if s.user_id not in (None, submission.user_id)]
                    if others:
                        url = reverse("llmdetect_user", kwargs={**kwargs, "user_id": submission.user_id})
                        response = self.client.get(url, {"submission": submission.id, "compare": others[0].id})
                        self.assertEqual(response.status_code, 200)

    def test_other_contests_do_not_change_the_risks(self):
        """Without earlier contests chosen, what happens elsewhere on the instance is irrelevant."""
        for rng, case in self._contests():
            get_config(case.contest).history_contests.clear()
            assess_contest(case.contest)
            before = {a.submission_id: (a.risk, a.signals) for a in RiskAssessment.objects.filter(contest=case.contest)}
            RandomContest(rng, f"x{case.contest.id}", case.users)
            assess_contest(case.contest)
            after = {a.submission_id: (a.risk, a.signals) for a in RiskAssessment.objects.filter(contest=case.contest)}
            self.assertEqual(before, after)
