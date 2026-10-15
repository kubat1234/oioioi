from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from oioioi.base.utils.deps import check_django_app_dependencies
from oioioi.contests.models import Contest

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
    daily_weight = models.PositiveIntegerField(
        default=1,
        verbose_name=_("daily ranking weight"),
        help_text=_("Main ranking = daily weight × Daily + all-time weight × All-time. If one weight is 0, the other ranking is the main one."),
    )
    stale_weight = models.PositiveIntegerField(default=1, verbose_name=_("all-time ranking weight"))
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

    def clean(self):
        super().clean()
        check_weights(self.daily_weight, self.stale_weight)

    def has_main_ranking(self):
        """With one weight 0 there is no combined ranking, the other one is the main one."""
        return bool(self.daily_weight and self.stale_weight)

    def main_ranking_label(self):
        """E.g. "Daily + All-time" or "2 × Daily + 3 × All-time"; a weight of 1 is not written."""
        parts = []
        for weight, name in ((self.daily_weight, _("Daily")), (self.stale_weight, _("All-time"))):
            if weight:
                parts.append(str(name) if weight == 1 else f"{weight} × {name}")
        return " + ".join(parts)


def check_weights(daily_weight, stale_weight):
    if daily_weight == 0 and stale_weight == 0:
        raise ValidationError(_("At least one weight must be greater than 0."))


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
