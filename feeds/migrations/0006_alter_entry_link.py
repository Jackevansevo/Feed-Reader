# Generated by Django 4.1 on 2022-08-13 10:20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("feeds", "0005_alter_entry_title"),
    ]

    operations = [
        migrations.AlterField(
            model_name="entry",
            name="link",
            field=models.URLField(blank=True, max_length=1000, null=True),
        ),
    ]
