from django.db import migrations, models


def use_mlx_provider_name(apps, schema_editor):
    apps.get_model("interviews", "SuggestedQuestion").objects.filter(
        provider="hf-qwen-local"
    ).update(provider="hf-qwen-mlx")


def restore_legacy_provider_name(apps, schema_editor):
    apps.get_model("interviews", "SuggestedQuestion").objects.filter(
        provider="hf-qwen-mlx"
    ).update(provider="hf-qwen-local")


class Migration(migrations.Migration):
    dependencies = [("interviews", "0001_initial")]

    operations = [
        migrations.RunPython(use_mlx_provider_name, restore_legacy_provider_name),
        migrations.AlterField(
            model_name="suggestedquestion",
            name="state",
            field=models.CharField(
                choices=[
                    ("suggested", "Önerildi"),
                    ("asked", "Soruldu"),
                    ("dismissed", "Reddedildi"),
                    ("superseded", "Yeni yanıtla güncellendi"),
                ],
                default="suggested",
                max_length=16,
            ),
        ),
    ]
