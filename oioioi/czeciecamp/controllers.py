from operator import itemgetter  # pylint: disable=E0611

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Max
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

from oioioi.contests.models import ProblemInstance, Submission
from oioioi.contests.utils import is_contest_basicadmin, is_contest_observer
from oioioi.czeciecamp.models import (
    DAILY_NAME,
    MEDALS_NONE,
    SCORING_LAST,
    STALE_NAME,
    CampRanking,
    create_default_rankings,
    get_camp_config,
    medal_places,
    ordered_rankings,
)
from oioioi.participants.controllers import ParticipantsController
from oioioi.participants.utils import is_participant
from oioioi.programs.controllers import ProgrammingContestController
from oioioi.rankings.controllers import DefaultRankingController
from oioioi.rankings.models import Ranking

PRIVILEGED_PERMISSIONS = ("admin", "observer")


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


def is_daily_submission(round, date):
    """A submission counts as "Dzienne" if it was sent while its round was
    running. A trial round is treated as if it ended right after it started.
    """
    if round.is_trial or date < round.start_date:
        return False
    return round.end_date is None or date <= round.end_date


def round_in_rankings(round, config, timestamp):
    """Participants see a round in rankings once its results are shown
    (or once it starts, if results are visible during rounds).
    """
    if config.results_visible_during_round:
        return round.start_date <= timestamp
    return round.results_date is not None and round.results_date <= timestamp


class CzecieCampRegistrationController(ParticipantsController):
    """Only contest admins add participants, there is no self-registration."""


class CzecieCampContestController(ProgrammingContestController):
    description = _("Czecie Camp Contest")
    create_forum = False
    scoring_description = _(
        "The submissions are scored on a set of groups of test cases.\n"
        "Participants may submit to a round after it ends. The daily ranking counts only submissions "
        "sent during the round, the all-time ranking counts all submissions sent until the counting end."
    )

    def registration_controller(self):
        return CzecieCampRegistrationController(self.contest)

    def ranking_controller(self):
        return CzecieCampRankingController(self.contest)

    def separate_public_results(self):
        return True

    def results_visible(self, request, submission):
        if get_camp_config(self.contest).results_visible_during_round:
            return True
        return super().results_visible(request, submission)

    def adjust_contest(self):
        super().adjust_contest()
        get_camp_config(self.contest)
        if not self.contest.camp_rankings.exists():
            create_default_rankings(self.contest)

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
        rounds = sorted(r.id for r in self.contest.round_set.all() if round_in_rankings(r, config, timestamp))
        tag = "r" + "-".join(map(str, rounds))
        if is_frozen(self.contest, config, timestamp):
            tag += "f"
        return tag

    def _get_ranking(self, partial_key):
        if not partial_key.isdigit():
            return None
        return CampRanking.objects.filter(contest=self.contest, id=partial_key).first()

    def _visible_rounds(self, ranking, can_see_all, timestamp):
        config = get_camp_config(self.contest)
        return [r for r in ranking.get_rounds() if can_see_all or round_in_rankings(r, config, timestamp)]

    def available_rankings(self, request):
        can_see_all = is_contest_basicadmin(request) or is_contest_observer(request)
        return [
            (str(ranking.id), ranking.name)
            for ranking in ordered_rankings(self.contest)
            if can_see_all or (ranking.public_date is not None and ranking.public_date <= request.timestamp)
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
        if not privileged:
            users = [user for user in users if user.id not in hidden_users]
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

        # (user_id, pi_id) -> (points, submission_id); earliest best one wins,
        # or the last one for Dzienne counting the last submission
        best_daily = {}
        best_stale = {}
        for submission in submissions:
            if counting_end is not None and submission.date > counting_end:
                continue
            pi = pi_by_id[submission.problem_instance_id]
            # E.g. sent before the round was moved later; only admins can submit early.
            if submission.date < pi.round.start_date:
                continue
            points = submission.score.to_int()
            cell_key = (submission.user_id, pi.id)
            if stale_end is None or submission.date <= stale_end:
                if cell_key not in best_stale or points > best_stale[cell_key][0]:
                    best_stale[cell_key] = (points, submission.id)
            if is_daily_submission(pi.round, submission.date):
                if last_daily or cell_key not in best_daily or points > best_daily[cell_key][0]:
                    best_daily[cell_key] = (points, submission.id)

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
                }
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
                }
            )

        # Hidden participants (shown only to admins) get no place or medal.
        shown = [row for row in rows if not row["hidden"]]
        self._assign_places(shown, itemgetter("sum"))
        for row in rows:
            if row["hidden"]:
                row["place"] = None
        rows.sort(key=itemgetter("sum"), reverse=True)
        leader_sum = shown[0]["sum"] if shown else 0
        gold, silver, bronze = medal_places(ranking.medals if ranking else MEDALS_NONE, len(shown))
        for row in rows:
            row["diff"] = row["sum"] - leader_sum
            row["percentage"] = 100 * row["sum"] / leader_sum if leader_sum else 0
            row["medal"] = None
            if row["sum"] > 0 and not row["hidden"]:
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
                line += [_blank_if_none(cell["daily"]), _blank_if_none(cell["stale"])]
        else:
            line += [_blank_if_none(cell["value"]) for cell in row["cells"]]
        return line


def _blank_if_none(value):
    return "" if value is None else value
