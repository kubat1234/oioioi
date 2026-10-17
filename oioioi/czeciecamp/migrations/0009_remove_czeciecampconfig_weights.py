from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('czeciecamp', '0008_camp_rankings'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='czeciecampconfig',
            name='daily_weight',
        ),
        migrations.RemoveField(
            model_name='czeciecampconfig',
            name='stale_weight',
        ),
    ]
