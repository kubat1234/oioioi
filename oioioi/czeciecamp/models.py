from django.db import models
from django.utils.translation import gettext_lazy as _

from oioioi.base.utils.deps import check_django_app_dependencies
from oioioi.contests.models import Contest

check_django_app_dependencies(__name__, ["oioioi.participants"])


class CzecieCampConfig(models.Model):
    """Per-camp settings of a Czecie Camp Contest."""

    DAILY_WEIGHT_CHOICES = [(1, _("Daily + All-time")), (2, _("2 × Daily + All-time"))]

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
    daily_weight = models.IntegerField(
        choices=DAILY_WEIGHT_CHOICES,
        default=1,
        verbose_name=_("main ranking"),
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

    class Meta:
        verbose_name = _("Czecie camp config")
        verbose_name_plural = _("Czecie camp configs")

    def __str__(self):
        return str(self.contest)


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
