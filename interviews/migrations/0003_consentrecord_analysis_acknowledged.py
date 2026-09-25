from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("interviews", "0002_suggestedquestion_superseded_state")]

    operations = [
        migrations.AddField(
            model_name="consentrecord",
            name="analysis_acknowledged",
            field=models.BooleanField(default=False),
        ),
    ]
