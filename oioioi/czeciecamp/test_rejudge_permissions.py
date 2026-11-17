"""Who may start a rejudge, or change what a rejudge depends on.

Every way of doing it over HTTP is tried on a problem and a submission of camp
"b" by everyone who must not be able to: nobody logged in, a participant of
that camp, an observer of it, a participant and an admin of another camp (the
admin also through the URLs of their own camp). Each request is then repeated
by an admin of camp "b", which must work: a denial test proves nothing if the
request would have failed for everybody.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse

from oioioi.contests.models import ContestPermission, ProblemInstance, Submission
from oioioi.contests.scores import IntegerScore
from oioioi.czeciecamp.models import get_camp_config
from oioioi.czeciecamp.test_integration import TwoCampsTestCase, _date
from oioioi.evalmgr.models import QueuedJob
from oioioi.programs.models import ProgramSubmission
from oioioi.suspendjudge.models import SuspendedProblem

JUDGE = "oioioi.problems.controllers.ProblemController.judge"
DENIED = (302, 400, 403, 404, 405)


class Endpoint:
    """One request against camp "b". ``url`` and ``data`` take the test case;
    ``url`` also takes the contest whose URLs are used. ``changed`` tells
    whether the request did something other than judging; a response
    containing ``shows`` counts as that too.
    """

    def __init__(self, name, url, data=None, method="post", judges=True, changed=None, setup=None, shows=None):
        self.name = name
        self.url = url
        self.data = data or (lambda test: {})
        self.method = method
        self.judges = judges
        self.changed = changed or (lambda test: False)
        self.setup = setup or (lambda test: None)
        self.shows = shows


def _url(name, **kwargs):
    return lambda test, contest_id: reverse(name, kwargs={"contest_id": contest_id, **{key: value(test) for key, value in kwargs.items()}})


def _admin_url(name, suffix=""):
    return lambda test, contest_id: reverse("oioioiadmin:" + name, kwargs={"contest_id": contest_id}) + suffix


def _suspend(test):
    SuspendedProblem.objects.create(problem_instance=test.pi_b)
    QueuedJob.objects.create(job_id="suspended-job", state="SUSPENDED", submission=test.sub_b)


def _queue(test):
    QueuedJob.objects.create(job_id="waiting-job", state="WAITING", submission=test.sub_b)


def _make_suspected(test):
    Submission.objects.filter(id=test.sub_b.id).update(kind="SUSPECTED")


def _remember_rejudge(test):
    config = get_camp_config(test.camp_b)
    config.last_rejudges = {str(test.pi_b.id): {"at": _date(2).isoformat(), "count": 1}}
    config.save()


ENDPOINTS = [
    Endpoint("one submission", _url("rejudge_submission", submission_id=lambda test: test.sub_b.id)),
    Endpoint(
        "all submissions of a problem",
        _url("rejudge_all_submissions_for_problem", problem_instance_id=lambda test: test.pi_b.id),
        lambda test: {"rejudge_type": "FULL"},
    ),
    Endpoint(
        "several problems",
        _url("rejudge_multiple_problems"),
        lambda test: {"ids": str(test.pi_b.id), "rejudge_type": "FULL"},
    ),
    Endpoint(
        "chosen submissions, the form",
        _admin_url("contests_submission_changelist"),
        lambda test: {"action": "rejudge_action", "_selected_action": [test.sub_b.id]},
        judges=False,
        # With an id from another camp an admin gets the form without any submission.
        shows='name="submissions"',
    ),
    Endpoint(
        "chosen submissions",
        _admin_url("contests_submission_changelist", "rejudge/"),
        lambda test: {"submissions": [test.sub_b.id], "rejudge_type": "FULL"},
    ),
    # A link, so a GET.
    Endpoint("model solutions", _url("model_solutions_rejudge", problem_instance_id=lambda test: test.pi_b.id), method="get"),
    Endpoint(
        "resume and rejudge",
        lambda test, contest_id: reverse("oioioiadmin:suspendjudge_resume_and_rejudge", kwargs={"contest_id": contest_id, "problem_instance_id": test.pi_b.id}),
        changed=lambda test: not SuspendedProblem.objects.filter(problem_instance=test.pi_b).exists(),
        setup=_suspend,
    ),
    Endpoint(
        "resume and clear the queue",
        lambda test, contest_id: reverse("oioioiadmin:suspendjudge_resume_and_clear", kwargs={"contest_id": contest_id, "problem_instance_id": test.pi_b.id}),
        judges=False,
        changed=lambda test: not QueuedJob.objects.filter(job_id="suspended-job").exists(),
        setup=_suspend,
    ),
    Endpoint(
        "suspend judging",
        lambda test, contest_id: reverse("oioioiadmin:suspendjudge_suspend_all", kwargs={"contest_id": contest_id, "problem_instance_id": test.pi_b.id}),
        judges=False,
        changed=lambda test: SuspendedProblem.objects.filter(problem_instance=test.pi_b).exists(),
    ),
    Endpoint(
        "suspend judging of final tests",
        lambda test, contest_id: reverse(
            "oioioiadmin:suspendjudge_suspend_all_but_init", kwargs={"contest_id": contest_id, "problem_instance_id": test.pi_b.id}
        ),
        judges=False,
        changed=lambda test: SuspendedProblem.objects.filter(problem_instance=test.pi_b).exists(),
    ),
    Endpoint(
        "evaluation queue",
        _admin_url("evalmgr_contestqueuedjob_changelist"),
        lambda test: {"action": "rejudge_selected", "_selected_action": ["waiting-job"]},
        changed=lambda test: QueuedJob.objects.get(job_id="waiting-job").state != "WAITING",
        setup=_queue,
    ),
    Endpoint(
        "suspected submission made normal",
        _url("change_submission_kind", submission_id=lambda test: test.sub_b.id, kind=lambda test: "NORMAL"),
        changed=lambda test: Submission.objects.get(id=test.sub_b.id).kind != "SUSPECTED",
        setup=_make_suspected,
    ),
    Endpoint(
        "mark for rejudge",
        _url("mark_for_rejudge", problem_instance_id=lambda test: test.pi_b.id),
        lambda test: {"confirm": "1"},
        judges=False,
        changed=lambda test: ProblemInstance.objects.get(id=test.pi_b.id).needs_rejudge,
    ),
    Endpoint(
        "rejudge not needed",
        _url("rejudge_not_needed", problem_instance_id=lambda test: test.pi_b.id),
        lambda test: {"confirm": "1"},
        judges=False,
        changed=lambda test: not ProblemInstance.objects.get(id=test.pi_b.id).needs_rejudge,
        setup=lambda test: ProblemInstance.objects.filter(id=test.pi_b.id).update(needs_rejudge=True),
    ),
]


class TestRejudgePermissions(TwoCampsTestCase):
    def setUp(self):
        super().setUp()
        self.sub_b = ProgramSubmission.objects.create(
            user=self.user_b,
            problem_instance=self.pi_b,
            date=_date(1, hour=1),
            kind="NORMAL",
            status="OK",
            score=IntegerScore(50),
            source_file=ContentFile(b"int main() {}", name="b.cpp"),
        )
        self.admin_a = self._user("admin_a", self.camp_a, "contests.contest_admin")
        self.basicadmin_a = self._user("basicadmin_a", self.camp_a, "contests.contest_basicadmin")
        self.observer_b = self._user("observer_b", self.camp_b, "contests.contest_observer")
        self.admin_b = self._user("admin_b", self.camp_b, "contests.contest_admin")
        self.basicadmin_b = self._user("basicadmin_b", self.camp_b, "contests.contest_basicadmin")

    def _user(self, username, contest, permission):
        user = User.objects.create_user(username, f"{username}@example.com", "password")
        ContestPermission.objects.create(user=user, contest=contest, permission=permission)
        return user

    def _attempt(self, endpoint, user, contest_id="b"):
        """Sends the request and rolls everything back. Returns the status
        code, how many submissions went to judging and whether anything else
        has changed.
        """
        self.client.logout()
        with transaction.atomic():
            endpoint.setup(self)
            if user is not None:
                self.client.force_login(user)
            url = endpoint.url(self, contest_id)
            with patch(JUDGE) as judge:
                if endpoint.method == "get":
                    response = self.client.get(url)
                else:
                    response = self.client.post(url, endpoint.data(self))
            changed = bool(endpoint.changed(self)) or (endpoint.shows is not None and endpoint.shows in response.content.decode())
            result = response.status_code, judge.call_count, changed
            transaction.set_rollback(True)
        return result

    def _assert_denied(self, user, contest_id="b"):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint.name, user=getattr(user, "username", None), contest=contest_id):
                status, judged, changed = self._attempt(endpoint, user, contest_id)
                self.assertEqual((judged, changed), (0, False))
                self.assertIn(status, DENIED if endpoint.shows is None else (*DENIED, 200))

    def test_anonymous(self):
        self._assert_denied(None)

    def test_participant_of_the_camp(self):
        self._assert_denied(self.user_b)

    def test_observer_of_the_camp(self):
        self._assert_denied(self.observer_b)

    def test_participant_of_another_camp(self):
        self._assert_denied(self.user_a)
        self._assert_denied(self.user_a, contest_id="c")

    def test_admin_of_another_camp(self):
        for admin in (self.admin_a, self.basicadmin_a):
            self._assert_denied(admin)
            # The ids of camp "b" in the URLs of the admin's own camp.
            self._assert_denied(admin, contest_id="c")

    def test_admin_of_the_camp(self):
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint.name):
                status, judged, changed = self._attempt(endpoint, self.admin_b)
                self.assertIn(status, (200, 302))
                if endpoint.judges:
                    self.assertGreater(judged, 0)
                else:
                    self.assertEqual(judged, 0)
                    self.assertTrue(changed)

    def test_basic_admin_of_the_camp(self):
        """A basic admin rejudges submissions, but neither the model solutions
        nor the evaluation queue belong to them.
        """
        full_admin_only = ("model solutions", "evaluation queue")
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint.name):
                status, judged, changed = self._attempt(endpoint, self.basicadmin_b)
                if endpoint.name in full_admin_only:
                    self.assertEqual((judged, changed), (0, False))
                    self.assertIn(status, DENIED)
                elif endpoint.judges:
                    self.assertGreater(judged, 0)

    def test_rejudge_report(self):
        _remember_rejudge(self)
        url = reverse("czeciecamp_rejudge_report", kwargs={"contest_id": "b", "problem_instance_id": self.pi_b.id})
        own_url = reverse("czeciecamp_rejudge_report", kwargs={"contest_id": "c", "problem_instance_id": self.pi_b.id})
        for user in (None, self.user_b, self.observer_b, self.user_a, self.admin_a):
            self.client.logout()
            if user is not None:
                self.client.force_login(user)
            self.assertIn(self.client.get(url).status_code, DENIED)
            self.assertIn(self.client.get(own_url).status_code, DENIED)
        self.client.force_login(self.basicadmin_b)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_chosen_submissions_of_another_camp_are_left_out(self):
        """An admin's list holds only the submissions of their camp, whatever ids are sent."""
        sub_a = ProgramSubmission.objects.create(
            user=self.user_a, problem_instance=self.pi_a, date=_date(1, hour=1), kind="NORMAL", source_file=ContentFile(b"int main() {}", name="a.cpp")
        )
        self.client.force_login(self.admin_a)
        url = reverse("oioioiadmin:contests_submission_changelist", kwargs={"contest_id": "c"}) + "rejudge/"
        with patch(JUDGE) as judge:
            response = self.client.post(url, {"submissions": [sub_a.id, self.sub_b.id], "rejudge_type": "FULL"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([call.args[0].id for call in judge.call_args_list], [sub_a.id])

    def test_unknown_rejudge_type_is_rejected(self):
        self.client.force_login(self.admin_b)
        url = reverse("oioioiadmin:contests_submission_changelist", kwargs={"contest_id": "b"}) + "rejudge/"
        with patch(JUDGE) as judge:
            response = self.client.post(url, {"submissions": [self.sub_b.id], "rejudge_type": "EVERYTHING", "tests": ["no-such-test"]})
        self.assertEqual((response.status_code, judge.call_count), (400, 0))
