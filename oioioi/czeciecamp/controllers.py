from operator import itemgetter  # pylint: disable=E0611

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Max
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from oioioi.contests.models import ProblemInstance, Submission
from oioioi.contests.utils import is_contest_basicadmin
from oioioi.czeciecamp.models import CzecieCampConfig, get_camp_config, ordered_rounds
from oioioi.participants.controllers import ParticipantsController
from oioioi.participants.utils import is_participant
from oioioi.programs.controllers import ProgrammingContestController
from oioioi.rankings.controllers import DefaultRankingController
from oioioi.rankings.models import Ranking

MAIN_KEY = "main"
STALE_KEY = "stale"
DAILY_KEY = "dzienne"
AGGREGATE_KEYS = (MAIN_KEY, STALE_KEY, DAILY_KEY)
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


class CzecieCampRegistrationController(ParticipantsController):
    """Only contest admins add participants, there is no self-registration."""


class CzecieCampContestController(ProgrammingContestController):
    description = _("Czecie Camp Contest")
    create_forum = False
    scoring_description = _(
        "The submissions are scored on a set of groups of test cases.\n"
        "Participants may submit to a round after it ends. Dzienne ranking counts only submissions "
        "sent during the round, Stałe ranking counts all submissions sent until the counting end."
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
    """Rankings of a camp: Dzienne+Stałe (main), Stałe, Dzienne and Dzienne
    for every round.

    A full key ends with a tag describing which rounds are public and whether
    the ranking is frozen, so a scheduled publication or the freeze yields
    a fresh ranking instead of a stale cached one.
    """

    description = _("Czecie Camp ranking")

    def get_full_key(self, request, partial_key):
        key = super().get_full_key(request, partial_key)
        return key + "#" + self._state_tag(self._key_permission(key), request.timestamp)

    def _state_tag(self, permission, timestamp):
        if permission in PRIVILEGED_PERMISSIONS:
            return "all"
        rounds = sorted(r.id for r in self._iter_rounds(False, timestamp, MAIN_KEY))
        tag = "r" + "-".join(map(str, rounds))
        if is_frozen(self.contest, get_camp_config(self.contest), timestamp):
            tag += "f"
        return tag

    def _iter_rounds(self, can_see_all, timestamp, partial_key, request=None):
        ccontroller = self.contest.controller
        queryset = self.contest.round_set.all()
        if partial_key not in AGGREGATE_KEYS:
            if not partial_key.isdigit():
                return
            queryset = queryset.filter(id=partial_key)
        for round in ordered_rounds(self.contest, queryset):
            times = ccontroller.get_round_times(request, round)
            if can_see_all or times.public_results_visible(timestamp):
                yield round

    def _rounds_for_key(self, key):
        can_see_all = self._key_permission(key) in PRIVILEGED_PERMISSIONS
        return self._iter_rounds(can_see_all, timezone.now(), self.get_partial_key(key))

    def available_rankings(self, request):
        rounds = list(self._rounds_for_ranking(request, MAIN_KEY))
        if not rounds:
            return []
        config = get_camp_config(self.contest)
        main_label = dict(CzecieCampConfig.DAILY_WEIGHT_CHOICES)[config.daily_weight]
        rankings = [
            (MAIN_KEY, main_label),
            (STALE_KEY, _("Stałe")),
            (DAILY_KEY, _("Dzienne")),
        ]
        return rankings + [(str(r.id), r.name) for r in rounds]

    def partial_keys_for_probleminstance(self, pi):
        return list(AGGREGATE_KEYS) + [str(pi.round_id)]

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
        partial_key = self.get_partial_key(key)
        now = timezone.now()
        config = get_camp_config(self.contest)
        rounds = list(self._rounds_for_key(key))
        round_position = {round.id: i for i, round in enumerate(rounds)}
        pis = sorted(
            ProblemInstance.objects.filter(round__in=rounds).select_related("problem", "round"),
            key=lambda pi: (round_position[pi.round_id], pi.short_name),
        )
        if partial_key == DAILY_KEY:
            pis = [pi for pi in pis if not pi.round.is_trial]
        pi_by_id = {pi.id: pi for pi in pis}

        frozen_at = None
        if self._key_permission(key) not in PRIVILEGED_PERMISSIONS and is_frozen(self.contest, config, now):
            frozen_at = get_freeze_date(self.contest, config)
        counting_end = config.counting_end_date
        stale_end = min((d for d in (counting_end, frozen_at) if d is not None), default=None)

        users = list(self.filter_users_for_ranking(key, User.objects.all()).distinct().order_by("last_name", "first_name", "username"))
        submissions = (
            Submission.objects.filter(
                problem_instance__in=pis,
                user__in=users,
                kind="NORMAL",
                score__isnull=False,
            )
            .only("id", "user_id", "problem_instance_id", "date", "score")
            .order_by("date")
        )

        # (user_id, pi_id) -> (points, submission_id); earliest best one wins
        best_daily = {}
        best_stale = {}
        for submission in submissions:
            if counting_end is not None and submission.date > counting_end:
                continue
            pi = pi_by_id[submission.problem_instance_id]
            points = submission.score.to_int()
            cell_key = (submission.user_id, pi.id)
            if stale_end is None or submission.date <= stale_end:
                if cell_key not in best_stale or points > best_stale[cell_key][0]:
                    best_stale[cell_key] = (points, submission.id)
            if is_daily_submission(pi.round, submission.date):
                if cell_key not in best_daily or points > best_daily[cell_key][0]:
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
                show_stale = partial_key == STALE_KEY or (partial_key not in AGGREGATE_KEYS and pi.round.is_trial)
                shown = stale if show_stale else daily
                cell = {
                    "daily": daily[0] if daily else None,
                    "stale": stale[0] if stale else None,
                    "value": shown[0] if shown else None,
                    "url": self._submission_url(stale if partial_key == MAIN_KEY else shown),
                }
                if partial_key == MAIN_KEY and (daily or stale):
                    cell["weighted_daily"] = config.daily_weight * (cell["daily"] or 0)
                    cell["value"] = cell["weighted_daily"] + (cell["stale"] or 0)
                cells.append(cell)
            if partial_key == MAIN_KEY:
                total = config.daily_weight * daily_sum + stale_sum
            elif partial_key == STALE_KEY:
                total = stale_sum
            elif partial_key == DAILY_KEY:
                total = daily_sum
            else:
                total = stale_sum if rounds and rounds[0].is_trial else daily_sum
            rows.append(
                {
                    "user": user,
                    "cells": cells,
                    "sum": total,
                    "daily_sum": daily_sum,
                    "stale_sum": stale_sum,
                }
            )

        self._assign_places(rows, itemgetter("sum"))
        leader_sum = rows[0]["sum"] if rows else 0
        for row in rows:
            row["diff"] = row["sum"] - leader_sum
            row["medal"] = row["place"] if row["place"] <= 3 and row["sum"] > 0 else None
        return {
            "rows": rows,
            "problem_instances": self._get_pis_with_visibility(key, pis),
            "participants_on_page": getattr(settings, "PARTICIPANTS_ON_PAGE", 100),
            "is_main": partial_key == MAIN_KEY,
            "is_aggregate": partial_key in AGGREGATE_KEYS,
            "daily_weight": config.daily_weight,
            "frozen_at": frozen_at,
            "frozen_for_participants": self._key_permission(key) in PRIVILEGED_PERMISSIONS and is_frozen(self.contest, config, now),
        }

    def _render_ranking_page(self, key, data, page):
        request = self._fake_request(page)
        data["is_admin"] = self.is_admin_key(key)
        return render_to_string("czeciecamp/ranking.html", context=data, request=request)

    def _get_csv_header(self, key, data):
        header = [_("No."), _("Login"), _("First name"), _("Last name"), _("Sum")]
        if data["is_main"]:
            header += [_("Dzienne"), _("Stałe")]
        for pi, _statement_visible in data["problem_instances"]:
            if data["is_main"]:
                header += [
                    "{} ({})".format(pi.get_short_name_display(), _("Dzienne")),
                    "{} ({})".format(pi.get_short_name_display(), _("Stałe")),
                ]
            else:
                header.append(pi.get_short_name_display())
        return header

    def _get_csv_row(self, key, row):
        line = [
            row["place"],
            row["user"].username,
            row["user"].first_name,
            row["user"].last_name,
            row["sum"],
        ]
        if self.get_partial_key(key) == MAIN_KEY:
            line += [row["daily_sum"], row["stale_sum"]]
            for cell in row["cells"]:
                line += [_blank_if_none(cell["daily"]), _blank_if_none(cell["stale"])]
        else:
            line += [_blank_if_none(cell["value"]) for cell in row["cells"]]
        return line


def _blank_if_none(value):
    return "" if value is None else value
