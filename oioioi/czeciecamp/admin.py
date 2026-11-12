from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from oioioi.base import admin
from oioioi.contests.admin import ProblemInstanceAdmin
from oioioi.czeciecamp.controllers import CzecieCampContestController
from oioioi.czeciecamp.models import CampProblemSettings, get_camp_config


def _is_camp(contest):
    return contest is not None and isinstance(contest.controller, CzecieCampContestController)


class CampProblemSettingsInline(admin.TabularInline):
    model = CampProblemSettings
    fields = ["round_submissions_limit"]
    # At most one row per problem; no row means the camp defaults.
    extra = 1
    max_num = 1
    can_delete = False


class CampProblemInstanceAdminMixin:
    """Links to the report of the latest rejudge of a problem, if there was
    one, and edits the camp settings of the problem.
    """

    def get_inlines(self, request, obj):
        inlines = super().get_inlines(request, obj)
        if obj is not None and _is_camp(obj.contest):
            inlines = tuple(inlines) + (CampProblemSettingsInline,)
        return inlines

    def inline_actions(self, instance):
        actions = super().inline_actions(instance)
        contest = instance.contest
        if _is_camp(contest) and str(instance.id) in get_camp_config(contest).last_rejudges:
            url = reverse("czeciecamp_rejudge_report", kwargs={"contest_id": contest.id, "problem_instance_id": instance.id})
            actions.append((url, _("Rejudge report")))
        return actions


ProblemInstanceAdmin.mix_in(CampProblemInstanceAdminMixin)
