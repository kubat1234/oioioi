from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from oioioi.base.admin import system_admin_menu_registry
from oioioi.base.permissions import enforce_condition, is_superuser, make_request_condition
from oioioi.contests.menu import contest_admin_menu_registry
from oioioi.contests.models import Contest, Round
from oioioi.contests.utils import contest_exists, is_contest_basicadmin
from oioioi.czeciecamp.controllers import CzecieCampContestController, get_freeze_date, get_last_round_end, is_frozen
from oioioi.czeciecamp.forms import AddParticipantsForm, CampSettingsForm, CreateCampForm, FreezeDateForm, RankingForm, RoundForm
from oioioi.czeciecamp.models import (
    CONTROLLER_NAME,
    CampRanking,
    CzecieCampConfig,
    create_default_rankings,
    get_camp_config,
    ordered_rankings,
    ordered_rounds,
    round_weights,
)
from oioioi.participants.models import Participant
from oioioi.rankings.models import Ranking

NEW_ROUND_PREFIX = "new"
ROUNDS_FORM_ID = "rounds-form"
DEFAULT_START_HOUR = 20
CAMP_SUBMISSIONS_LIMIT = 25


@make_request_condition
def is_czecie_camp(request):
    contest = getattr(request, "contest", None)
    return contest is not None and isinstance(contest.controller, CzecieCampContestController)


def _panel_url(contest):
    return reverse("czeciecamp_panel", kwargs={"contest_id": contest.id})


def _round_prefix(round):
    return f"r{round.id}"


def rounded_now(now):
    """The first moment from ``now`` on whose minutes are divisible by 5."""
    rounded = now.replace(second=0, microsecond=0)
    if rounded < now:
        rounded += timedelta(minutes=1)
    return rounded + timedelta(minutes=-rounded.minute % 5)


def rounded_down(now):
    """The last moment up to ``now`` whose minutes are divisible by 5."""
    rounded = now.replace(second=0, microsecond=0)
    return rounded - timedelta(minutes=rounded.minute % 5)


def day_name(number):
    """Name of a camp round, in the language of the admin who creates it."""
    return gettext("Day %(number)d") % {"number": number}


def _next_round_name(contest):
    number = contest.round_set.filter(is_trial=False).count() + 1
    while contest.round_set.filter(name=day_name(number)).exists():
        number += 1
    return day_name(number)


def _round_rows(contest, config, now):
    rows = []
    for round in ordered_rounds(contest):
        rows.append(
            {
                "round": round,
                "form": RoundForm(
                    instance=round,
                    prefix=_round_prefix(round),
                    html_form=ROUNDS_FORM_ID,
                    counted=round.id not in config.excluded_rounds,
                ),
                "results_visible": round.results_date is not None and round.results_date <= now,
            }
        )
    return rows


def _ranking_rows(contest, now):
    return [
        {
            "ranking": ranking,
            "visible": ranking.public_date is not None and ranking.public_date <= now,
            "rounds": ranking.get_rounds() if ranking.round_id is None else None,
        }
        for ranking in ordered_rankings(contest)
    ]


def _report_form_errors(request, form, title=""):
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        for error in errors:
            message = f"{label}: {error}" if label else str(error)
            messages.error(request, f"{title} – {message}" if title else message)


@contest_admin_menu_registry.register_decorator(
    _("Czecie panel"),
    lambda request: _panel_url(request.contest),
    condition=is_czecie_camp,
    order=1,
)
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def panel_view(request):
    contest = request.contest
    config = get_camp_config(contest)
    settings_form = CampSettingsForm(instance=config)
    participants_form = AddParticipantsForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "settings":
            settings_form = CampSettingsForm(request.POST, instance=config)
            if settings_form.is_valid():
                settings_form.save()
                Ranking.invalidate_contest(contest)
                messages.success(request, _("Settings saved."))
                return redirect(_panel_url(contest))
        elif action == "freeze_date":
            freeze_form = FreezeDateForm(request.POST, instance=config)
            if freeze_form.is_valid():
                config = freeze_form.save(commit=False)
                # The end of the last round is the default, keep following it.
                if config.freeze_date == get_last_round_end(contest):
                    config.freeze_date = None
                config.save()
                Ranking.invalidate_contest(contest)
                messages.success(request, _("Freeze time saved."))
            else:
                _report_form_errors(request, freeze_form)
            return redirect(_panel_url(contest))
        elif action == "participants":
            participants_form = AddParticipantsForm(request.POST)
            if participants_form.is_valid():
                added = 0
                for user in participants_form.cleaned_data["users"]:
                    _participant, created = Participant.objects.get_or_create(contest=contest, user=user)
                    added += created
                Ranking.invalidate_contest(contest)
                messages.success(request, _("Added %(count)d participants.") % {"count": added})
                return redirect(_panel_url(contest))
        else:
            raise PermissionDenied

    now = request.timestamp
    context = {
        "config": config,
        "settings_form": settings_form,
        "participants_form": participants_form,
        "participants": Participant.objects.filter(contest=contest).select_related("user").order_by("user__last_name", "user__first_name"),
        "hidden_users": set(config.hidden_users),
        "rounds": _round_rows(contest, config, now),
        "rankings": _ranking_rows(contest, now),
        "ranking_moves": [("up", "▲", _("Move up")), ("down", "▼", _("Move down"))],
        "new_round_form": RoundForm(
            prefix=NEW_ROUND_PREFIX,
            html_form="new-round-form",
            initial={
                "name": _next_round_name(contest),
                "start_date": rounded_now(now),
                "end_date": rounded_now(now),
            },
        ),
        "freeze_date": get_freeze_date(contest, config),
        "freeze_form": FreezeDateForm(instance=config, initial={"freeze_date": get_freeze_date(contest, config)}),
        "frozen": is_frozen(contest, config, now),
    }
    return TemplateResponse(request, "czeciecamp/panel.html", context)


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def round_action_view(request, round_id, action):
    round = get_object_or_404(Round, id=round_id, contest=request.contest)
    # A round number of minutes is easier to read later.
    now = rounded_down(request.timestamp)
    if action == "show_results":
        round.results_date = now
    elif action == "hide_results":
        round.results_date = None
    else:
        raise PermissionDenied
    round.save()
    Ranking.invalidate_contest(request.contest)
    return redirect(_panel_url(request.contest))


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def freeze_view(request, frozen):
    config = get_camp_config(request.contest)
    config.rankings_unfrozen = not frozen
    config.save()
    Ranking.invalidate_contest(request.contest)
    if frozen:
        messages.success(request, _("Rankings frozen for participants."))
    else:
        messages.success(request, _("Rankings unfrozen. Participants now see the final rankings."))
    return redirect(_panel_url(request.contest))


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def add_round_view(request):
    contest = request.contest
    form = RoundForm(request.POST, instance=Round(contest=contest), prefix=NEW_ROUND_PREFIX)
    if form.is_valid():
        round = form.save(commit=False)
        if round.is_trial:
            # Results of an "All-time" round are visible right away.
            round.results_date = round.start_date
        round.save()
        config = get_camp_config(contest)
        config.round_order = [r.id for r in ordered_rounds(contest)]
        if not form.cleaned_data["counted"]:
            config.excluded_rounds.append(round.id)
        config.save()
        Ranking.invalidate_contest(contest)
        messages.success(request, _("Added %(name)s.") % {"name": round.name})
    else:
        _report_form_errors(request, form)
    return redirect(_panel_url(contest))


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def save_rounds_view(request):
    """Saves all rows of the rounds table and their order at once."""
    contest = request.contest
    rounds = {round.id: round for round in contest.round_set.all()}
    forms = [
        RoundForm(request.POST, instance=round, prefix=_round_prefix(round)) for round in rounds.values() if f"{_round_prefix(round)}-name" in request.POST
    ]
    invalid = [form for form in forms if not form.is_valid()]
    if invalid:
        for form in invalid:
            _report_form_errors(request, form, title=form.instance.name)
        return redirect(_panel_url(contest))

    order = [int(i) for i in request.POST.getlist("order") if i.isdigit() and int(i) in rounds]
    with transaction.atomic():
        config = get_camp_config(contest)
        excluded = set(config.excluded_rounds)
        for form in forms:
            form.save()
            _update_round_ranking(form)
            if form.cleaned_data["counted"]:
                excluded.discard(form.instance.id)
            else:
                excluded.add(form.instance.id)
        config.excluded_rounds = sorted(excluded)
        config.round_order = order + [r.id for r in ordered_rounds(contest) if r.id not in order]
        config.save()
    Ranking.invalidate_contest(contest)
    messages.success(request, _("Rounds saved."))
    return redirect(_panel_url(contest))


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def participant_ranking_view(request, user_id, hidden):
    """Hides a participant in rankings or shows them again."""
    contest = request.contest
    participant = get_object_or_404(Participant, contest=contest, user_id=user_id)
    config = get_camp_config(contest)
    hidden_users = set(config.hidden_users)
    if hidden:
        hidden_users.add(participant.user_id)
    else:
        hidden_users.discard(participant.user_id)
    config.hidden_users = sorted(hidden_users)
    config.save()
    Ranking.invalidate_contest(contest)
    return redirect(_panel_url(contest) + "#participants")


def _update_round_ranking(form):
    """The ranking of a round follows changes of its type, and of its name
    unless the ranking was renamed.
    """
    for ranking in form.instance.camp_rankings.all():
        if "is_trial" in form.changed_data:
            ranking.daily_weight, ranking.stale_weight = round_weights(form.instance)
        if "name" in form.changed_data and ranking.name == form.initial.get("name"):
            ranking.name = form.instance.name
        ranking.save()


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def delete_round_view(request, round_id):
    round = get_object_or_404(Round, id=round_id, contest=request.contest)
    name = round.name
    round.delete()
    config = get_camp_config(request.contest)
    config.round_order = [r for r in config.round_order if r != round_id]
    config.excluded_rounds = [r for r in config.excluded_rounds if r != round_id]
    config.save()
    Ranking.invalidate_contest(request.contest)
    messages.success(request, _("Deleted %(name)s.") % {"name": name})
    return redirect(_panel_url(request.contest))


@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def edit_ranking_view(request, ranking_id=None):
    contest = request.contest
    if ranking_id is None:
        next_order = (contest.camp_rankings.filter(round=None).aggregate(Max("order"))["order__max"] or 0) + 1
        ranking = CampRanking(contest=contest, order=next_order)
    else:
        ranking = get_object_or_404(CampRanking, id=ranking_id, contest=contest)
    form = RankingForm(request.POST or None, instance=ranking)
    if request.method == "POST" and form.is_valid():
        form.save()
        Ranking.invalidate_contest(contest)
        messages.success(request, _("Ranking saved."))
        return redirect(_panel_url(contest))
    return TemplateResponse(request, "czeciecamp/ranking_form.html", {"form": form, "ranking": ranking})


def _move_ranking(contest, ranking, step):
    """Swaps a ranking without a round with its neighbour in the order."""
    rankings = [r for r in ordered_rankings(contest) if r.round_id is None]
    i = next(i for i, r in enumerate(rankings) if r.id == ranking.id)
    j = i + step
    if 0 <= j < len(rankings):
        rankings[i], rankings[j] = rankings[j], rankings[i]
    for order, r in enumerate(rankings):
        if r.order != order:
            r.order = order
            r.save(update_fields=["order"])


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def ranking_action_view(request, ranking_id, action):
    contest = request.contest
    ranking = get_object_or_404(CampRanking, id=ranking_id, contest=contest)
    now = rounded_down(request.timestamp)
    if action == "show":
        ranking.public_date = now
        round = ranking.round
        # The ranking of a round shows only rounds with results.
        if round is not None and not get_camp_config(contest).results_visible_during_round and (round.results_date is None or round.results_date > now):
            round.results_date = now
            round.save()
        ranking.save()
    elif action == "hide":
        ranking.public_date = None
        ranking.save()
    elif action == "up":
        _move_ranking(contest, ranking, -1)
    elif action == "down":
        _move_ranking(contest, ranking, 1)
    elif action == "delete":
        ranking.delete()
        messages.success(request, _("Deleted %(name)s.") % {"name": ranking.name})
    else:
        raise PermissionDenied
    Ranking.invalidate_contest(contest)
    return redirect(_panel_url(contest))


system_admin_menu_registry.register(
    "czeciecamp_create",
    _("Create Czecie camp"),
    lambda request: reverse("czeciecamp_create"),
    order=5,
)


@enforce_condition(is_superuser)
def create_camp_view(request):
    today_evening = timezone.localtime(request.timestamp).replace(hour=DEFAULT_START_HOUR, minute=0, second=0, microsecond=0)
    form = CreateCampForm(request.POST or None, initial={"start_date": today_evening})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        start = data["start_date"]
        with transaction.atomic():
            contest = Contest.objects.create(
                id=data["contest_id"],
                name=data["name"],
                controller_name=CONTROLLER_NAME,
                default_submissions_limit=CAMP_SUBMISSIONS_LIMIT,
                # The generic rules page does not describe camp rankings correctly.
                show_contest_rules=False,
            )
            CzecieCampConfig.objects.update_or_create(
                contest=contest,
                defaults={"results_visible_during_round": data["results_visible_during_round"]},
            )
            # Saving the contest created the default rankings, with weights 1.
            contest.camp_rankings.all().delete()
            create_default_rankings(contest, data["daily_weight"], data["stale_weight"])
            # Rounds get their own rankings when they are created.
            # Day 0 is an "All-time" round; its results are visible right away.
            Round.objects.create(contest=contest, name=day_name(0), start_date=start, end_date=start, results_date=start, is_trial=True)
            for day in range(1, data["days"] + 1):
                day_start = start + timedelta(days=day)
                Round.objects.create(contest=contest, name=day_name(day), start_date=day_start, end_date=day_start)
        messages.success(request, _("Camp created. Set the round times and add problems and participants below."))
        return redirect(_panel_url(contest))
    return TemplateResponse(request, "czeciecamp/create.html", {"form": form})
