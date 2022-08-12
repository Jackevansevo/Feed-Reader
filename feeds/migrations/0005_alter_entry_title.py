# Generated by Django 4.1 on 2022-08-12 19:47

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("feeds", "0004_alter_entry_link"),
    ]

    operations = [
        migrations.AlterField(
            model_name="entry",
            name="title",
            field=models.CharField(
                blank=True,
                max_length=400,
                null=True,
                validators=[django.core.validators.MinLengthValidator(1)],
            ),
        ),
    ]
