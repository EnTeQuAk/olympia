from django.db import models

from olympia.amo.models import ModelBase


class DiscoveryModule(ModelBase):
    """
    Keeps the application, ordering, and locale metadata for a module.

    The modules are defined statically in modules.py and linked to a database
    row through the module's name.
    """
    app = models.PositiveIntegerField(choices=amo.APPS_CHOICES,
                                      db_column='app_id')
    module = models.CharField(max_length=255)
    ordering = models.IntegerField(null=True, blank=True)
    locales = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'discovery_modules'
        unique_together = ('app', 'module')
