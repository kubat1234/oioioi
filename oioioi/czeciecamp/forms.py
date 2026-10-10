from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from oioioi.base.utils.validators import validate_db_string_id
from oioioi.contests.models import Contest, Round
from oioioi.czeciecamp.models import CzecieCampConfig

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"


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

    class Meta:
        model = CzecieCampConfig
        fields = ["counting_end_date", "daily_weight", "results_visible_during_round"]


class RoundForm(BootstrapFormMixin, forms.ModelForm):
    """Adds or edits a round in the camp panel.

    ``html_form`` is the id of the <form> element the inputs belong to,
    so that a form can span a whole table row.
    """

    start_date = DateTimeLocalField(label=_("Start"))
    end_date = DateTimeLocalField(label=_("End"), required=False)

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


class FreezeDateForm(BootstrapFormMixin, forms.ModelForm):
    freeze_date = DateTimeLocalField(
        required=False,
        label=CzecieCampConfig._meta.get_field("freeze_date").verbose_name,
        help_text=CzecieCampConfig._meta.get_field("freeze_date").help_text,
    )

    class Meta:
        model = CzecieCampConfig
        fields = ["freeze_date"]


class AddParticipantsForm(BootstrapFormMixin, forms.Form):
    users = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "login1\nlogin2\nemail@example.com"}),
        label=_("Usernames or emails, one per line"),
    )

    def clean_users(self):
        found = []
        missing = []
        for line in self.cleaned_data["users"].splitlines():
            name = line.strip()
            if not name:
                continue
            user = User.objects.filter(Q(username=name) | Q(email__iexact=name)).order_by("id").first()
            if user is None:
                missing.append(name)
            else:
                found.append(user)
        if missing:
            raise forms.ValidationError(_("Unknown users: %(users)s"), params={"users": ", ".join(missing)})
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
    daily_weight = forms.TypedChoiceField(
        choices=CzecieCampConfig.DAILY_WEIGHT_CHOICES,
        coerce=int,
        initial=1,
        label=_("Main ranking"),
    )

    def clean(self):
        cleaned_data = super().clean()
        if "contest_id" in self.errors or not cleaned_data.get("name"):
            return cleaned_data
        contest_id = cleaned_data.get("contest_id") or make_contest_id(cleaned_data["name"])
        if not contest_id:
            self.add_error("contest_id", _("Enter an ID."))
        elif Contest.objects.filter(id=contest_id).exists():
            self.add_error("contest_id", _("A contest with this ID already exists."))
        cleaned_data["contest_id"] = contest_id
        return cleaned_data
