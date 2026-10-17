import django.db.models.deletion
from django.db import migrations, models

DAILY_NAME = "Dzienne"
STALE_NAME = "Stałe"


def _label(daily_weight, stale_weight):
    parts = []
    for weight, name in ((daily_weight, DAILY_NAME), (stale_weight, STALE_NAME)):
        if weight:
            parts.append(name if weight == 1 else f"{weight} × {name}")
    return " + ".join(parts)


def create_rankings(apps, schema_editor):
    """Turns the weights of each camp into its default rankings and gives
    every round its own ranking, visible as the round ranking was.
    """
    CzecieCampConfig = apps.get_model("czeciecamp", "CzecieCampConfig")
    CampRanking = apps.get_model("czeciecamp", "CampRanking")
    Round = apps.get_model("contests", "Round")
    for config in CzecieCampConfig.objects.all():
        rounds = list(Round.objects.filter(contest_id=config.contest_id))
        public_dates = [r.public_results_date for r in rounds if r.public_results_date is not None]
        aggregate_public_date = min(public_dates, default=None)
        daily, stale = config.daily_weight, config.stale_weight
        rankings = []
        if daily and stale:
            rankings.append((_label(daily, stale), daily, stale))
        aggregate = [(STALE_NAME, 0, 1), (DAILY_NAME, 1, 0)]
        if not stale:
            aggregate.reverse()
        rankings += aggregate
        for order, (name, daily_weight, stale_weight) in enumerate(rankings):
            CampRanking.objects.create(
                contest_id=config.contest_id,
                name=name,
                order=order,
                daily_weight=daily_weight,
                stale_weight=stale_weight,
                public_date=aggregate_public_date,
            )
        for round in rounds:
            CampRanking.objects.create(
                contest_id=config.contest_id,
                name=round.name,
                round=round,
                daily_weight=0 if round.is_trial else 1,
                stale_weight=1 if round.is_trial else 0,
                public_date=round.public_results_date,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('contests', '0027_probleminstance_can_access_editorial'),
        ('czeciecamp', '0007_round_weights_and_excluded_rounds'),
    ]

    operations = [
        migrations.CreateModel(
            name='CampRanking',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, verbose_name='name')),
                ('order', models.IntegerField(default=0, verbose_name='order')),
                ('daily_weight', models.IntegerField(default=1, verbose_name='daily weight')),
                ('stale_weight', models.IntegerField(default=1, verbose_name='all-time weight')),
                ('daily_scoring', models.CharField(choices=[('best', 'best submission'), ('last', 'last submission')], default='best', help_text='Of the submissions sent during the round. All-time always counts the best one.', max_length=8, verbose_name='Daily counts the')),
                ('public_date', models.DateTimeField(blank=True, help_text='Empty means hidden.', null=True, verbose_name='visible for participants from')),
                ('contest', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='camp_rankings', to='contests.contest', verbose_name='contest')),
                ('round', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='camp_rankings', to='contests.round', verbose_name='ranking of round')),
                ('rounds', models.ManyToManyField(blank=True, help_text='None selected means all counted rounds.', related_name='+', to='contests.round', verbose_name='rounds')),
            ],
            options={
                'verbose_name': 'camp ranking',
                'verbose_name_plural': 'camp rankings',
            },
        ),
        migrations.RunPython(create_rankings, migrations.RunPython.noop),
    ]
