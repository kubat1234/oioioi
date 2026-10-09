from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from oioioi.base.admin import system_admin_menu_registry
from oioioi.base.permissions import enforce_condition, is_superuser, make_request_condition
from oioioi.contests.menu import contest_admin_menu_registry
from oioioi.contests.models import Contest, Round
from oioioi.contests.utils import contest_exists, is_contest_basicadmin
from oioioi.czeciecamp.controllers import CzecieCampContestController, get_freeze_date, get_last_round_end, is_frozen
from oioioi.czeciecamp.forms import AddParticipantsForm, CampSettingsForm, CreateCampForm, FreezeDateForm, RoundForm
from oioioi.czeciecamp.models import CzecieCampConfig, get_camp_config, ordered_rounds
from oioioi.participants.models import Participant
from oioioi.rankings.models import Ranking

CONTROLLER_NAME = "oioioi.czeciecamp.controllers.CzecieCampContestController"
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


def _next_round_name(contest):
    number = contest.round_set.filter(is_trial=False).count() + 1
    while contest.round_set.filter(name=f"Dzień {number}").exists():
        number += 1
    return f"Dzień {number}"


def _round_rows(contest, now):
    rows = []
    for round in ordered_rounds(contest):
        rows.append(
            {
                "round": round,
                "form": RoundForm(instance=round, prefix=_round_prefix(round), html_form=ROUNDS_FORM_ID),
                "results_visible": round.results_date is not None and round.results_date <= now,
                "ranking_visible": round.public_results_date is not None and round.public_results_date <= now,
            }
        )
    return rows


def _report_form_errors(request, form, title=""):
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        for error in errors:
            message = f"{label}: {error}" if label else str(error)
            messages.error(request, f"{title} – {message}" if title else message)


@contest_admin_menu_registry.register_decorator(
    _("Camp panel"),
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
        "rounds": _round_rows(contest, now),
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
        round.public_results_date = None
    elif action == "show_ranking":
        if round.results_date is None or round.results_date > now:
            round.results_date = now
        round.public_results_date = now
    elif action == "hide_ranking":
        round.public_results_date = None
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
            # Trial round results are visible right away.
            round.results_date = round.start_date
        round.save()
        config = get_camp_config(contest)
        config.round_order = [r.id for r in ordered_rounds(contest)]
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
        for form in forms:
            form.save()
        config = get_camp_config(contest)
        config.round_order = order + [r.id for r in ordered_rounds(contest) if r.id not in order]
        config.save()
    Ranking.invalidate_contest(contest)
    messages.success(request, _("Rounds saved."))
    return redirect(_panel_url(contest))


@require_POST
@enforce_condition(contest_exists & is_contest_basicadmin & is_czecie_camp)
def delete_round_view(request, round_id):
    round = get_object_or_404(Round, id=round_id, contest=request.contest)
    name = round.name
    round.delete()
    config = get_camp_config(request.contest)
    config.round_order = [r for r in config.round_order if r != round_id]
    config.save()
    Ranking.invalidate_contest(request.contest)
    messages.success(request, _("Deleted %(name)s.") % {"name": name})
    return redirect(_panel_url(request.contest))


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
            )
            CzecieCampConfig.objects.update_or_create(
                contest=contest,
                defaults={
                    "results_visible_during_round": data["results_visible_during_round"],
                    "daily_weight": data["daily_weight"],
                },
            )
            # Dzień 0 is the trial round; its results are visible right away.
            Round.objects.create(contest=contest, name="Dzień 0", start_date=start, end_date=start, results_date=start, is_trial=True)
            for day in range(1, data["days"] + 1):
                day_start = start + timedelta(days=day)
                Round.objects.create(contest=contest, name=f"Dzień {day}", start_date=day_start, end_date=day_start)
        messages.success(request, _("Camp created. Set the round times and add problems and participants below."))
        return redirect(_panel_url(contest))
    return TemplateResponse(request, "czeciecamp/create.html", {"form": form})
