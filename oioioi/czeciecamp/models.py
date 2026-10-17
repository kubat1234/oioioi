import math

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from oioioi.base.utils.deps import check_django_app_dependencies
from oioioi.contests.models import Contest, Round

check_django_app_dependencies(__name__, ["oioioi.participants"])


class CzecieCampConfig(models.Model):
    """Per-camp settings of a Czecie Camp Contest."""

    contest = models.OneToOneField(
        Contest,
        on_delete=models.CASCADE,
        related_name="czeciecamp_config",
        verbose_name=_("contest"),
    )
    counting_end_date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("counting end"),
        help_text=_("Submissions sent after this moment are still judged, but they do not count in any ranking. Empty means no limit."),
    )
    results_visible_during_round = models.BooleanField(
        default=False,
        verbose_name=_("results visible during rounds"),
        help_text=_("Participants see full results of their submissions right away. Otherwise only example tests until results are shown."),
    )
    rankings_unfrozen = models.BooleanField(
        default=False,
        verbose_name=_("rankings unfrozen"),
        help_text=_("Rankings freeze for participants at the end of the last round, until unfrozen."),
    )
    freeze_date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("freeze time"),
        help_text=_("Empty means the end of the last round."),
    )
    # Round ids in the order set in the camp panel. Rounds missing from the
    # list (e.g. added in the admin) go last, by start date.
    round_order = models.JSONField(default=list, blank=True, verbose_name=_("round order"))
    # Ids of rounds left out of the Daily, All-time and main rankings.
    # They still have their own ranking.
    excluded_rounds = models.JSONField(default=list, blank=True, verbose_name=_("rounds not counted"))

    class Meta:
        verbose_name = _("Czecie camp config")
        verbose_name_plural = _("Czecie camp configs")

    def __str__(self):
        return str(self.contest)


CONTROLLER_NAME = "oioioi.czeciecamp.controllers.CzecieCampContestController"

SCORING_BEST = "best"
SCORING_LAST = "last"

# Rankings are always named in Polish, whatever the language of the admin.
DAILY_NAME = "Dzienne"
STALE_NAME = "Stałe"

MEDALS_NONE = "none"
MEDALS_PODIUM = "podium"
MEDALS_IOI = "ioi"


class CampRanking(models.Model):
    """One ranking of a camp: daily weight × Daily + all-time weight × All-time
    over a set of rounds.

    Rankings are created automatically: Dzienne + Stałe, Stałe and Dzienne
    with the camp, and one for every round with the round (``round``).
    All of them can be edited, deleted and added in the camp panel.
    """

    contest = models.ForeignKey(Contest, on_delete=models.CASCADE, related_name="camp_rankings", verbose_name=_("contest"))
    name = models.CharField(max_length=255, verbose_name=_("name"))
    # Order of the rankings without a round. Rankings of rounds follow them,
    # in the order of the rounds.
    order = models.IntegerField(default=0, verbose_name=_("order"))
    round = models.ForeignKey(
        Round,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="camp_rankings",
        verbose_name=_("ranking of round"),
    )
    rounds = models.ManyToManyField(
        Round,
        blank=True,
        related_name="+",
        verbose_name=_("rounds"),
        help_text=_("None selected means all counted rounds."),
    )
    daily_weight = models.IntegerField(default=1, verbose_name=_("daily weight"))
    stale_weight = models.IntegerField(default=1, verbose_name=_("all-time weight"))
    daily_scoring = models.CharField(
        max_length=8,
        choices=[(SCORING_BEST, _("best submission")), (SCORING_LAST, _("last submission"))],
        default=SCORING_BEST,
        verbose_name=_("Daily counts the"),
        help_text=_("Of the submissions sent during the round. All-time always counts the best one."),
    )
    public_date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("visible for participants from"),
        help_text=_("Empty means hidden."),
    )
    medals = models.CharField(
        max_length=8,
        choices=[
            (MEDALS_NONE, _("no medals")),
            (MEDALS_PODIUM, _("places 1, 2 and 3")),
            (MEDALS_IOI, _("IOI: 1/12 gold, 1/6 silver, 1/4 bronze")),
        ],
        default=MEDALS_PODIUM,
        verbose_name=_("medals"),
    )
    show_sum = models.BooleanField(default=True, verbose_name=_("show sum"))
    show_percentage = models.BooleanField(default=False, verbose_name=_("show percentage of the leader's sum"))
    show_difference = models.BooleanField(default=True, verbose_name=_("show difference to the leader"))

    class Meta:
        verbose_name = _("camp ranking")
        verbose_name_plural = _("camp rankings")

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        check_weights(self.daily_weight, self.stale_weight)

    def is_combined(self):
        return bool(self.daily_weight and self.stale_weight)

    def get_rounds(self):
        """Rounds of the ranking in the camp order."""
        if self.round_id is not None:
            return [self.round]
        selected = list(self.rounds.all())
        if selected:
            return ordered_rounds(self.contest, selected)
        excluded = get_camp_config(self.contest).excluded_rounds
        return ordered_rounds(self.contest, self.contest.round_set.exclude(id__in=excluded))


def medal_places(medals, participants):
    """The last places that get a gold, silver and bronze medal."""
    if medals == MEDALS_PODIUM:
        return 1, 2, 3
    if medals == MEDALS_IOI:
        gold = math.ceil(participants / 12)
        silver = gold + math.ceil(participants / 6)
        return gold, silver, silver + math.ceil(participants / 4)
    return 0, 0, 0


def check_weights(daily_weight, stale_weight):
    if daily_weight == 0 and stale_weight == 0:
        raise ValidationError(_("At least one weight must be different from 0."))


def ranking_label(daily_weight, stale_weight):
    """E.g. "Dzienne + Stałe" or "2 × Dzienne + 3 × Stałe"; a weight of 1 is not written."""
    parts = []
    for weight, name in ((daily_weight, DAILY_NAME), (stale_weight, STALE_NAME)):
        if weight:
            parts.append(name if weight == 1 else f"{weight} × {name}")
    return " + ".join(parts)


def round_weights(round):
    """A round's own ranking shows Dzienne, or Stałe for an "All-time" round."""
    return (0, 1) if round.is_trial else (1, 0)


def create_default_rankings(contest, daily_weight=1, stale_weight=1):
    """Dzienne + Stałe (only with both weights), then Stałe and Dzienne;
    with no all-time weight Dzienne goes first, as the main ranking.
    """
    rankings = []
    if daily_weight and stale_weight:
        rankings.append((ranking_label(daily_weight, stale_weight), daily_weight, stale_weight))
    aggregate = [(STALE_NAME, 0, 1), (DAILY_NAME, 1, 0)]
    if not stale_weight:
        aggregate.reverse()
    rankings += aggregate
    for order, (name, daily, stale) in enumerate(rankings):
        CampRanking.objects.create(contest=contest, name=name, order=order, daily_weight=daily, stale_weight=stale)


def create_round_ranking(round):
    daily_weight, stale_weight = round_weights(round)
    return CampRanking.objects.create(
        contest=round.contest,
        name=round.name,
        round=round,
        daily_weight=daily_weight,
        stale_weight=stale_weight,
        show_difference=False,
    )


def ordered_rankings(contest):
    """Rankings without a round by their order, then rankings of rounds in the round order."""
    rankings = list(contest.camp_rankings.select_related("round"))
    position = {round.id: i for i, round in enumerate(ordered_rounds(contest))}
    return sorted(rankings, key=lambda r: (r.round_id is not None, position.get(r.round_id, 0), r.order, r.id))


@receiver(post_save, sender=Round)
def _create_round_ranking(sender, instance, created, raw=False, **kwargs):
    if created and not raw and instance.contest.controller_name == CONTROLLER_NAME:
        create_round_ranking(instance)


def get_camp_config(contest):
    config, _created = CzecieCampConfig.objects.get_or_create(contest=contest)
    return config


def ordered_rounds(contest, rounds=None):
    """Returns the rounds (all rounds of the contest by default) in the order
    set in the camp panel.
    """
    if rounds is None:
        rounds = contest.round_set.all()
    position = {round_id: i for i, round_id in enumerate(get_camp_config(contest).round_order)}
    return sorted(rounds, key=lambda r: (position.get(r.id, len(position)), r.start_date, r.id))
