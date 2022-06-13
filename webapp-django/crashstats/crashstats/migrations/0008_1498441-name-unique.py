# Generated by Django 1.11.16 on 2018-10-12 18:06

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("crashstats", "0007_1498441-platform")]

    operations = [
        migrations.AlterField(
            model_name="platform",
            name="name",
            field=models.CharField(
                help_text=b"Name of the platform", max_length=20, unique=True
            ),
        )
    ]
