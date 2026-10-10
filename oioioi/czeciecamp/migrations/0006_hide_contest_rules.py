from django.db import migrations

CONTROLLER_NAME = "oioioi.czeciecamp.controllers.CzecieCampContestController"


def hide_rules(apps, schema_editor):
    Contest = apps.get_model("contests", "Contest")
    Contest.objects.filter(controller_name=CONTROLLER_NAME).update(show_contest_rules=False)


class Migration(migrations.Migration):
    dependencies = [
        ("czeciecamp", "0005_rename_ranking_labels"),
        ("contests", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(hide_rules, migrations.RunPython.noop),
    ]
