from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils.text import capfirst, slugify
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from oioioi.base.utils.validators import validate_db_string_id
from oioioi.contests.models import Contest, Round
from oioioi.czeciecamp.models import CampRanking, CzecieCampConfig, check_weights, ordered_rounds

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"
# Defaults of the camp wizard: a limit during rounds, none for the whole camp.
CAMP_ROUND_SUBMISSIONS_LIMIT = 50
CAMP_SUBMISSIONS_LIMIT = 0


class DateTimeLocalField(forms.DateTimeField):
    widget = forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DATETIME_FORMAT)

    def __init__(self, **kwargs):
        kwargs.setdefault("input_formats", [DATETIME_FORMAT])
        super().__init__(**kwargs)


class BootstrapFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


class CampSettingsForm(BootstrapFormMixin, forms.ModelForm):
    counting_end_date = DateTimeLocalField(
        required=False,
        label=CzecieCampConfig._meta.get_field("counting_end_date").verbose_name,
        help_text=CzecieCampConfig._meta.get_field("counting_end_date").help_text,
    )

    # Stored in the contest, where SIO keeps it.
    default_submissions_limit = forms.IntegerField(
        min_value=0,
        label=capfirst(_("default submissions limit for the whole camp")),
        help_text=_("Given to problems added to the camp from now on; a problem's own limit is set on its admin page. 0 means no limit."),
    )

    class Meta:
        model = CzecieCampConfig
        fields = ["counting_end_date", "results_visible_during_round", "default_round_submissions_limit", "default_submissions_limit"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_submissions_limit"].initial = self.instance.contest.default_submissions_limit

    def save(self, commit=True):
        config = super().save(commit)
        contest = config.contest
        contest.default_submissions_limit = self.cleaned_data["default_submissions_limit"]
        contest.save(update_fields=["default_submissions_limit"])
        return config


class RoundForm(BootstrapFormMixin, forms.ModelForm):
    """Adds or edits a round in the camp panel.

    ``html_form`` is the id of the <form> element the inputs belong to,
    so that a form can span a whole table row.
    """

    start_date = DateTimeLocalField(label=_("Start"))
    end_date = DateTimeLocalField(label=_("End"), required=False)
    # An "All-time" round is stored as a trial round.
    is_trial = forms.TypedChoiceField(
        choices=[(False, pgettext_lazy("round type", "Daily")), (True, pgettext_lazy("round type", "All-time"))],
        coerce=lambda value: value == "True",
        label=_("Type"),
    )

    class Meta:
        model = Round
        fields = ["name", "start_date", "end_date", "is_trial"]

    def __init__(self, *args, html_form=None, **kwargs):
        super().__init__(*args, **kwargs)
        if html_form:
            for field in self.fields.values():
                field.widget.attrs["form"] = html_form

    def clean_name(self):
        name = self.cleaned_data["name"]
        same_name = Round.objects.filter(contest=self.instance.contest, name=name).exclude(pk=self.instance.pk)
        if same_name.exists():
            raise forms.ValidationError(_("A round with this name already exists."))
        return name


class RankingForm(BootstrapFormMixin, forms.ModelForm):
    public_date = DateTimeLocalField(
        required=False,
        label=CampRanking._meta.get_field("public_date").verbose_name,
        help_text=CampRanking._meta.get_field("public_date").help_text,
    )

    class Meta:
        model = CampRanking
        fields = [
            "name",
            "daily_weight",
            "stale_weight",
            "daily_scoring",
            "rounds",
            "public_date",
            "medals",
            "show_sum",
            "show_percentage",
            "show_difference",
        ]
        widgets = {"rounds": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        contest = self.instance.contest
        if self.instance.round_id is not None:
            # The ranking of a round always shows that round.
            del self.fields["rounds"]
        else:
            rounds = self.fields["rounds"]
            rounds.queryset = contest.round_set.all()
            rounds.choices = [(round.id, round.name) for round in ordered_rounds(contest)]
            rounds.widget.attrs["class"] = "form-check-input"

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("daily_weight") is not None and cleaned_data.get("stale_weight") is not None:
            try:
                check_weights(cleaned_data["daily_weight"], cleaned_data["stale_weight"])
            except forms.ValidationError as error:
                self.add_error(None, error)
        return cleaned_data


class FreezeDateForm(BootstrapFormMixin, forms.ModelForm):
    freeze_date = DateTimeLocalField(
        required=False,
        label=CzecieCampConfig._meta.get_field("freeze_date").verbose_name,
        help_text=CzecieCampConfig._meta.get_field("freeze_date").help_text,
    )

    class Meta:
        model = CzecieCampConfig
        fields = ["freeze_date"]


def find_users_by_name(line):
    """Users whose "first last" (or "last first") name is the line, ignoring case.

    Every split of the words is tried, so multi-word first or last names match too.
    """
    words = line.split()
    query = Q(pk__in=[])
    for i in range(1, len(words)):
        first, last = " ".join(words[:i]), " ".join(words[i:])
        query |= Q(first_name__iexact=first, last_name__iexact=last)
        query |= Q(first_name__iexact=last, last_name__iexact=first)
    return list(User.objects.filter(query).order_by("id")[:2])


class AddParticipantsForm(BootstrapFormMixin, forms.Form):
    users = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "login1\nemail@example.com\nJan Kowalski"}),
        label=_("Usernames, emails or full names, one per line"),
        help_text=_("A name is added only if exactly one user has it. Lines that do not match are reported and stay in the field."),
    )

    def clean_users(self):
        """Matched users; lines that did not match go to ``self.unknown`` and ``self.ambiguous``."""
        found = []
        self.unknown = []
        self.ambiguous = []
        for line in self.cleaned_data["users"].splitlines():
            name = " ".join(line.split())
            if not name:
                continue
            user = User.objects.filter(Q(username=name) | Q(email__iexact=name)).order_by("id").first()
            if user is not None:
                found.append(user)
                continue
            by_name = find_users_by_name(name)
            if len(by_name) == 1:
                found.append(by_name[0])
            elif by_name:
                self.ambiguous.append(name)
            else:
                self.unknown.append(name)
        return found


MAX_CAMP_DAYS = 30


def make_contest_id(name):
    """Contest ID from a camp name: lowercase, words joined with "-"."""
    name = name.replace("ł", "l").replace("Ł", "L")
    return slugify(name)[:32].strip("-_")


class CreateCampForm(BootstrapFormMixin, forms.Form):
    name = forms.CharField(max_length=255, label=_("Name"))
    contest_id = forms.CharField(
        max_length=32,
        required=False,
        label=_("ID"),
        validators=[validate_db_string_id],
        help_text=_("Used in URLs. Filled in from the name."),
    )
    start_date = DateTimeLocalField(label=_("Start (Day 0)"))
    days = forms.IntegerField(
        min_value=0,
        max_value=MAX_CAMP_DAYS,
        initial=5,
        label=_("Number of days after Day 0"),
        help_text=_("Day 1, Day 2, ... one round per day after Day 0."),
    )
    results_visible_during_round = forms.BooleanField(
        required=False,
        label=CzecieCampConfig._meta.get_field("results_visible_during_round").verbose_name,
        help_text=CzecieCampConfig._meta.get_field("results_visible_during_round").help_text,
    )
    daily_weight = forms.IntegerField(
        min_value=0,
        initial=1,
        label=capfirst(_("daily ranking weight")),
        help_text=_(
            "Main ranking = daily weight × Daily + all-time weight × All-time. If one weight is 0, the other ranking is the main one. "
            "Each ranking can be changed later in the camp panel."
        ),
    )
    stale_weight = forms.IntegerField(
        min_value=0,
        initial=1,
        label=capfirst(_("all-time ranking weight")),
    )
    round_submissions_limit = forms.IntegerField(
        min_value=0,
        initial=CAMP_ROUND_SUBMISSIONS_LIMIT,
        label=capfirst(CzecieCampConfig._meta.get_field("default_round_submissions_limit").verbose_name),
        help_text=_("Submissions to a problem a participant may send while its round is running. 0 means no limit."),
    )
    submissions_limit = forms.IntegerField(
        min_value=0,
        initial=CAMP_SUBMISSIONS_LIMIT,
        label=capfirst(_("default submissions limit for the whole camp")),
        help_text=_("Submissions to a problem a participant may send in the whole camp, during and after its round. 0 means no limit."),
    )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("daily_weight") is not None and cleaned_data.get("stale_weight") is not None:
            try:
                check_weights(cleaned_data["daily_weight"], cleaned_data["stale_weight"])
            except forms.ValidationError as error:
                self.add_error(None, error)
        if "contest_id" in self.errors or not cleaned_data.get("name"):
            return cleaned_data
        contest_id = cleaned_data.get("contest_id") or make_contest_id(cleaned_data["name"])
        if not contest_id:
            self.add_error("contest_id", _("Enter an ID."))
        elif Contest.objects.filter(id=contest_id).exists():
            self.add_error("contest_id", _("A contest with this ID already exists."))
        cleaned_data["contest_id"] = contest_id
        return cleaned_data
