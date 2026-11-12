from collections import Counter
from operator import itemgetter  # pylint: disable=E0611

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Max
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

from oioioi.contests.controllers import get_score_badge_class
from oioioi.contests.models import ProblemInstance, Submission
from oioioi.contests.utils import has_any_active_round, is_contest_basicadmin, is_contest_observer
from oioioi.czeciecamp.models import (
    DAILY_NAME,
    MEDALS_NONE,
    SCORING_LAST,
    STALE_NAME,
    CampRanking,
    CzecieCampConfig,
    create_default_rankings,
    get_camp_config,
    get_round_submissions_limit,
    medal_places,
    ordered_rankings,
)
from oioioi.participants.controllers import ParticipantsController
from oioioi.participants.utils import is_participant
from oioioi.programs.controllers import ProgrammingContestController
from oioioi.rankings.controllers import DefaultRankingController
from oioioi.rankings.models import Ranking

PRIVILEGED_PERMISSIONS = ("admin", "observer")
# The newest report of these kinds decides about the score of a submission.
RESULT_REPORT_KINDS = ("NORMAL", "FAILURE")


def get_last_round_end(contest):
    return contest.round_set.filter(is_trial=False).aggregate(Max("end_date"))["end_date__max"]


def get_freeze_date(contest, config=None):
    """Rankings freeze for participants at the time set in the camp panel,
    by default at the end of the last non-trial round.
    """
    config = config or get_camp_config(contest)
    return config.freeze_date or get_last_round_end(contest)


def is_frozen(contest, config, timestamp):
    if config.rankings_unfrozen:
        return False
    freeze_date = get_freeze_date(contest, config)
    return freeze_date is not None and timestamp >= freeze_date


def after_freeze_text(count):
    """Polish, like the names Dzienne and Stałe."""
    if count == 1:
        noun = "zgłoszenie"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        noun = "zgłoszenia"
    else:
        noun = "zgłoszeń"
    return f"Po zamrożeniu: {count} {noun}"


def is_daily_submission(round, date):
    """A submission counts as "Dzienne" if it was sent while its round was
    running. A trial round is treated as if it ended right after it started.
    """
    if round.is_trial or date < round.start_date:
        return False
    return round.end_date is None or date <= round.end_date


def best_submissions(submissions, pi_by_id, counting_end, stale_end, last_daily, score_of):
    """Picks the submissions counted in rankings, from ``submissions`` sorted
    by date, with the score of each given by ``score_of`` (``None`` means it
    is left out).

    Returns dicts for Dzienne and Stałe, mapping (user_id, pi_id) to
    (points, submission_id); earliest best one wins, or the last one for
    Dzienne counting the last submission.
    """
    best_daily = {}
    best_stale = {}
    for submission in submissions:
        if counting_end is not None and submission.date > counting_end:
            continue
        pi = pi_by_id[submission.problem_instance_id]
        # E.g. sent before the round was moved later; only admins can submit early.
        if submission.date < pi.round.start_date:
            continue
        score = score_of(submission)
        if score is None:
            continue
        points = score.to_int()
        cell_key = (submission.user_id, pi.id)
        if stale_end is None or submission.date <= stale_end:
            if cell_key not in best_stale or points > best_stale[cell_key][0]:
                best_stale[cell_key] = (points, submission.id)
        if is_daily_submission(pi.round, submission.date):
            if last_daily or cell_key not in best_daily or points > best_daily[cell_key][0]:
                best_daily[cell_key] = (points, submission.id)
    return best_daily, best_stale


def round_in_rankings(round, timestamp):
    """Participants see a round in rankings once it is published in them
    (its public results date). Personal results are shown separately
    (the results date, or right away if results are visible during rounds).
    """
    return round.public_results_date is not None and round.public_results_date <= timestamp


class CzecieCampRegistrationController(ParticipantsController):
    """Only contest admins add participants, there is no self-registration."""


class CzecieCampContestController(ProgrammingContestController):
    description = _("Czecie Camp Contest")
    create_forum = False
    # A problem without a round is hidden from participants (only admins can
    # submit to it), e.g. extra problems that are not used yet.
    allow_problems_without_round = True
    scoring_description = _(
        "The submissions are scored on a set of groups of test cases.\n"
        "Participants may submit to a round after it ends. The daily ranking counts only submissions "
        "sent during the round, the all-time ranking counts all submissions sent until the counting end."
    )

    def uses_threshold_linear_scoring(self):
        return True

    def fill_evaluation_environ(self, environ, submission):
        super().fill_evaluation_environ(environ, submission)
        environ["test_scorer"] = "oioioi.programs.utils.threshold_linear_test_scorer"

    def registration_controller(self):
        return CzecieCampRegistrationController(self.contest)

    def ranking_controller(self):
        return CzecieCampRankingController(self.contest)

    def separate_public_results(self):
        return True

    def show_last_submission_status(self, request):
        # The problems list of a camp shows only the score.
        return False

    def problems_list_result_badge_class(self, request, result):
        """A gold frame around full points if some submission got full points
        while the round of the problem was running (as for Dzienne).
        """
        if result.score is None or result.submission_report is None:
            return ""
        shown = result.submission_report.submission
        if get_score_badge_class(result.score, shown.max_score) != "text-bg-success":
            return ""
        round = result.problem_instance.round
        if round is None:
            return ""
        submissions = Submission.objects.filter(
            user=result.user, problem_instance=result.problem_instance, kind="NORMAL", score__isnull=False, date__gte=round.start_date
        )
        for submission in submissions:
            if is_daily_submission(round, submission.date) and get_score_badge_class(submission.score, submission.max_score) == "text-bg-success":
                return "camp-full-in-round"
        return ""

    def results_visible(self, request, submission):
        if get_camp_config(self.contest).results_visible_during_round:
            return True
        return super().results_visible(request, submission)

    def problem_rejudge_started(self, problem_instance, submissions):
        """Remembers the moment, so that a report can compare the results
        from before it with the current ones. Only submissions which already
        have a result can be compared.
        """
        count = (
            Submission.objects.filter(
                pk__in=list(submissions.values_list("pk", flat=True)),
                kind="NORMAL",
                user__isnull=False,
                submissionreport__kind__in=RESULT_REPORT_KINDS,
            )
            .distinct()
            .count()
        )
        if not count:
            return
        get_camp_config(self.contest)
        with transaction.atomic():
            config = CzecieCampConfig.objects.select_for_update().get(contest=self.contest)
            config.last_rejudges[str(problem_instance.id)] = {"at": timezone.now().isoformat(), "count": count}
            config.save(update_fields=["last_rejudges"])

    def adjust_contest(self):
        super().adjust_contest()
        get_camp_config(self.contest)
        if not self.contest.camp_rankings.exists():
            create_default_rankings(self.contest)

    def can_see_round(self, request_or_context, round, no_admin=False):
        """While a round is active, participants see only the active rounds.
        Otherwise every round that has started is visible. Visibility changes
        exactly at the round start, there is no preparation time before it.
        """
        context = self.make_context(request_or_context)
        if not no_admin and context.is_admin:
            return True
        if has_any_active_round(request_or_context):
            return self.get_round_times(request_or_context, round).is_active(context.timestamp)
        return super().can_see_round(request_or_context, round, no_admin)

    def can_submit(self, request, problem_instance, check_round_times=True):
        """Participants may submit to every round that has already started,
        also after it ends.
        """
        if request.user.is_anonymous:
            return False
        if is_contest_basicadmin(request):
            return True
        if not is_participant(request):
            return False
        if not super().can_submit(request, problem_instance, check_round_times=False):
            return False
        if not check_round_times:
            return True
        rtimes = self.get_round_times(request, problem_instance.round)
        return not rtimes.is_future(request.timestamp)

    # The default implementation lets the problem controller count all
    # submissions; a camp does not count compilation errors, so it counts here.
    def _counted_submissions(self, request, problem_instance, kind):
        """Submissions that use up the limits: all but compilation errors."""
        if request.user.is_anonymous:
            return Submission.objects.none()
        return Submission.objects.filter(user=request.user, problem_instance=problem_instance, kind=kind).exclude(status="CE")

    def get_submissions_count(self, request, problem_instance, kind=None):
        if kind is None:
            kind = self.get_default_submission_kind(request)
        return self._counted_submissions(request, problem_instance, kind).count()

    def _binding_limit(self, request, problem_instance, kind, noadmin=False):
        """Of the limits that apply now, the one that leaves the fewest
        submissions, as (limit, submissions left), or None if none applies.

        The limit for the whole camp is the usual one of the problem. While
        the round of the problem is running (as for Dzienne), submissions sent
        during the round are also limited by the round limit.
        """
        total = super().get_submissions_limit(request, problem_instance, kind, noadmin)
        if total is None:
            # Admins, ignored submissions: no limit at all.
            return None
        submissions = self._counted_submissions(request, problem_instance, kind)
        limits = []
        if total:
            limits.append((total, submissions.count()))
        round = problem_instance.round
        if kind == "NORMAL" and round is not None and is_daily_submission(round, request.timestamp):
            round_limit = get_round_submissions_limit(problem_instance)
            if round_limit:
                during_round = submissions.filter(date__gte=round.start_date)
                if round.end_date is not None:
                    during_round = during_round.filter(date__lte=round.end_date)
                limits.append((round_limit, during_round.count()))
        if not limits:
            return None
        limit, used = min(limits, key=lambda limit_used: limit_used[0] - limit_used[1])
        return limit, max(0, limit - used)

    def get_submissions_limit(self, request, problem_instance, kind="NORMAL", noadmin=False):
        """The limit shown next to the submissions left ("X of Y")."""
        binding = self._binding_limit(request, problem_instance, kind, noadmin)
        if binding is None:
            return super().get_submissions_limit(request, problem_instance, kind, noadmin)
        return binding[0]

    def get_submissions_left(self, request, problem_instance, kind=None):
        if request.user.is_anonymous:
            return None
        if kind is None:
            kind = self.get_default_submission_kind(request)
        binding = self._binding_limit(request, problem_instance, kind)
        return None if binding is None else binding[1]

    def is_submissions_limit_exceeded(self, request, problem_instance, kind=None):
        submissions_left = self.get_submissions_left(request, problem_instance, kind)
        return submissions_left is not None and submissions_left <= 0


class CzecieCampRankingController(DefaultRankingController):
    """Rankings of a camp, defined by ``CampRanking`` objects; a partial key
    is the id of one. Each one sums, over its rounds, daily weight × Dzienne
    + all-time weight × Stałe.

    A full key ends with a tag describing which rounds are public and whether
    the ranking is frozen, so a scheduled publication or the freeze yields
    a fresh ranking instead of a stale cached one, followed by the viewer's
    language, because rankingsd renders the HTML once per key.
    """

    description = _("Czecie Camp ranking")

    def get_full_key(self, request, partial_key):
        key = super().get_full_key(request, partial_key)
        return "#".join([key, self._state_tag(self._key_permission(key), request.timestamp), translation.get_language()])

    def _key_language(self, key):
        parts = key.split("#")
        return parts[3] if len(parts) > 3 else settings.LANGUAGE_CODE

    def _state_tag(self, permission, timestamp):
        if permission in PRIVILEGED_PERMISSIONS:
            return "all"
        config = get_camp_config(self.contest)
        rounds = sorted(r.id for r in self.contest.round_set.all() if round_in_rankings(r, timestamp))
        tag = "r" + "-".join(map(str, rounds))
        if is_frozen(self.contest, config, timestamp):
            tag += "f"
        return tag

    def _get_ranking(self, partial_key):
        if not partial_key.isdigit():
            return None
        return CampRanking.objects.filter(contest=self.contest, id=partial_key).first()

    def _visible_rounds(self, ranking, can_see_all, timestamp):
        return [r for r in ranking.get_rounds() if can_see_all or round_in_rankings(r, timestamp)]

    def available_rankings(self, request):
        """Rankings shown to participants; the ranking of a round appears
        only once the round is published in rankings.
        """
        can_see_all = is_contest_basicadmin(request) or is_contest_observer(request)
        return [
            (str(ranking.id), ranking.name)
            for ranking in ordered_rankings(self.contest)
            if can_see_all
            or (
                ranking.public_date is not None
                and ranking.public_date <= request.timestamp
                and (ranking.round_id is None or round_in_rankings(ranking.round, request.timestamp))
            )
        ]

    def partial_keys_for_probleminstance(self, pi):
        return [str(pk) for pk in self.contest.camp_rankings.values_list("id", flat=True)]

    def invalidate_pi(self, pi):
        # Full keys contain a state tag, so we invalidate the whole contest.
        Ranking.invalidate_contest(self.contest)

    def can_search_for_users(self):
        # Camp rankings are small, the search form is just clutter.
        return False

    def filter_users_for_ranking(self, key, queryset):
        return self.contest.controller.registration_controller().filter_participants(queryset)

    def _submission_url(self, best):
        if best is None:
            return None
        return reverse("submission", kwargs={"contest_id": self.contest.id, "submission_id": best[1]})

    def _disqualifications(self):
        """Users disqualified in the whole contest, and (user id, problem
        instance id) of problems with a disqualified submission. A
        disqualified submission makes its whole problem count 0: otherwise
        resubmitting the same code would bring the points back.
        """
        if "oioioi.disqualification" not in settings.INSTALLED_APPS:
            return set(), set()
        from oioioi.disqualification.models import Disqualification

        users, problems = set(), set()
        for user_id, pi_id in Disqualification.objects.filter(contest=self.contest, guilty=True).values_list("user_id", "submission__problem_instance_id"):
            if pi_id is None:
                users.add(user_id)
            else:
                problems.add((user_id, pi_id))
        return users, problems

    def _count_after_freeze(self, pis, users, frozen_at, counting_end):
        """Counts the submissions sent after the freeze, which all-time
        points leave out, by (user id, problem instance id). All of them
        count, also ones not judged yet or not compiled, so that the count
        tells nothing about their results.
        """
        submissions = Submission.objects.filter(problem_instance__in=pis, user__in=users, kind="NORMAL", date__gt=frozen_at)
        if counting_end is not None:
            submissions = submissions.filter(date__lte=counting_end)
        return Counter(submissions.values_list("user_id", "problem_instance_id"))

    def serialize_ranking(self, key):
        now = timezone.now()
        config = get_camp_config(self.contest)
        privileged = self._key_permission(key) in PRIVILEGED_PERMISSIONS
        ranking = self._get_ranking(self.get_partial_key(key))
        rounds = self._visible_rounds(ranking, privileged, now) if ranking else []
        round_position = {round.id: i for i, round in enumerate(rounds)}
        pis = sorted(
            ProblemInstance.objects.filter(round__in=rounds).select_related("problem", "round"),
            key=lambda pi: (round_position[pi.round_id], pi.short_name),
        )
        daily_weight = ranking.daily_weight if ranking else 0
        stale_weight = ranking.stale_weight if ranking else 0
        if not stale_weight:
            # An "All-time" round has no daily points.
            pis = [pi for pi in pis if not pi.round.is_trial]
        pi_by_id = {pi.id: pi for pi in pis}
        last_daily = ranking is not None and ranking.daily_scoring == SCORING_LAST
        combined = daily_weight and stale_weight

        frozen_at = None
        if not privileged and is_frozen(self.contest, config, now):
            frozen_at = get_freeze_date(self.contest, config)
        counting_end = config.counting_end_date
        stale_end = min((d for d in (counting_end, frozen_at) if d is not None), default=None)

        hidden_users = set(config.hidden_users)
        users = list(self.filter_users_for_ranking(key, User.objects.all()).distinct().order_by("last_name", "first_name", "username"))
        disqualified_users, disqualified_problems = self._disqualifications()
        if not privileged:
            # Admins see disqualified participants, marked, without a place.
            users = [user for user in users if user.id not in hidden_users and user.id not in disqualified_users]
        submissions = (
            Submission.objects.filter(
                problem_instance__in=pis,
                user__in=users,
                kind="NORMAL",
                score__isnull=False,
            )
            .only("id", "user_id", "problem_instance_id", "date", "score")
            .order_by("date", "id")
        )
        if disqualified_problems:
            submissions = [s for s in submissions if (s.user_id, s.problem_instance_id) not in disqualified_problems]

        best_daily, best_stale = best_submissions(submissions, pi_by_id, counting_end, stale_end, last_daily, lambda submission: submission.score)
        # Participants see where someone submitted after the freeze, but not the points.
        after_freeze = self._count_after_freeze(pis, users, frozen_at, counting_end) if frozen_at is not None and stale_weight else {}

        rows = []
        for user in users:
            cells = []
            daily_sum = 0
            stale_sum = 0
            for pi in pis:
                daily = best_daily.get((user.id, pi.id))
                stale = best_stale.get((user.id, pi.id))
                daily_sum += daily[0] if daily else 0
                stale_sum += stale[0] if stale else 0
                cell = {
                    "daily": daily[0] if daily else None,
                    "stale": stale[0] if stale else None,
                    "url": self._submission_url(stale if stale_weight else daily),
                    "disqualified": (user.id, pi.id) in disqualified_problems,
                }
                cell["after_freeze"] = 0 if cell["disqualified"] else after_freeze.get((user.id, pi.id), 0)
                if cell["after_freeze"]:
                    cell["after_freeze_text"] = after_freeze_text(cell["after_freeze"])
                if combined:
                    if daily or stale:
                        cell["weighted_daily"] = daily_weight * (cell["daily"] or 0)
                        cell["weighted_stale"] = stale_weight * (cell["stale"] or 0)
                        cell["value"] = cell["weighted_daily"] + cell["weighted_stale"]
                    else:
                        cell["value"] = None
                else:
                    shown = stale if stale_weight else daily
                    weight = stale_weight or daily_weight
                    cell["value"] = weight * shown[0] if shown else None
                cells.append(cell)
            rows.append(
                {
                    "user": user,
                    "cells": cells,
                    "sum": daily_weight * daily_sum + stale_weight * stale_sum,
                    "daily_sum": daily_sum,
                    "stale_sum": stale_sum,
                    "combined": bool(combined),
                    "hidden": user.id in hidden_users,
                    "disqualified": user.id in disqualified_users,
                }
            )

        # Hidden and disqualified participants (shown only to admins) get no
        # place or medal, and do not move the others down.
        shown = [row for row in rows if not row["hidden"] and not row["disqualified"]]
        self._assign_places(shown, itemgetter("sum"))
        for row in rows:
            if row["hidden"] or row["disqualified"]:
                row["place"] = None
        rows.sort(key=itemgetter("sum"), reverse=True)
        leader_sum = shown[0]["sum"] if shown else 0
        gold, silver, bronze = medal_places(ranking.medals if ranking else MEDALS_NONE, len(shown))
        for row in rows:
            row["diff"] = row["sum"] - leader_sum
            row["percentage"] = 100 * row["sum"] / leader_sum if leader_sum else 0
            row["medal"] = None
            if row["sum"] > 0 and row["place"] is not None:
                for medal, last_place in enumerate((gold, silver, bronze), start=1):
                    if row["place"] <= last_place:
                        row["medal"] = medal
                        break
        return {
            "rows": rows,
            "problem_instances": self._get_pis_with_visibility(key, pis),
            "participants_on_page": getattr(settings, "PARTICIPANTS_ON_PAGE", 100),
            "is_main": bool(combined),
            "is_aggregate": ranking is not None and ranking.round_id is None,
            "show_sum": ranking is None or ranking.show_sum,
            "show_percentage": ranking is not None and ranking.show_percentage,
            "show_difference": ranking is not None and ranking.show_difference,
            "daily_weight": daily_weight,
            "stale_weight": stale_weight,
            "frozen_at": frozen_at,
            "frozen_for_participants": privileged and is_frozen(self.contest, config, now),
        }

    def _render_ranking_page(self, key, data, page):
        request = self._fake_request(page)
        data["is_admin"] = self.is_admin_key(key)
        with translation.override(self._key_language(key)):
            return render_to_string("czeciecamp/ranking.html", context=data, request=request)

    def _get_csv_header(self, key, data):
        header = [_("No."), _("Login"), _("First name"), _("Last name"), _("Sum")]
        if data["is_main"]:
            header += [DAILY_NAME, STALE_NAME]
        for pi, _statement_visible in data["problem_instances"]:
            if data["is_main"]:
                header += [
                    f"{pi.get_short_name_display()} ({DAILY_NAME})",
                    f"{pi.get_short_name_display()} ({STALE_NAME})",
                ]
            else:
                header.append(pi.get_short_name_display())
        return header

    def _get_csv_row(self, key, row):
        line = [
            _blank_if_none(row["place"]),
            row["user"].username,
            row["user"].first_name,
            row["user"].last_name,
            row["sum"],
        ]
        if row["combined"]:
            line += [row["daily_sum"], row["stale_sum"]]
            for cell in row["cells"]:
                line += ["DQ", "DQ"] if cell.get("disqualified") else [_blank_if_none(cell["daily"]), _blank_if_none(cell["stale"])]
        else:
            line += ["DQ" if cell.get("disqualified") else _blank_if_none(cell["value"]) for cell in row["cells"]]
        return line


def _blank_if_none(value):
    return "" if value is None else value
