import olympia.core.logger

from olympia.activity.models import ActivityLog, CommentLog, VersionLog
from olympia.addons.models import Addon
from olympia.amo.celery import task
from olympia.amo.decorators import use_primary_db
from olympia.reviewers.models import AutoApprovalSummary
from olympia.reviewers.indexers import AddonGitRepositoryBlobIndexer
from olympia.versions.models import Version
from olympia.lib.es.utils import index_objects


log = olympia.core.logger.getLogger('z.task')


@task
def add_commentlog(items, **kw):
    log.info('[%s@%s] Adding CommentLog starting with ActivityLog: %s' %
             (len(items), add_commentlog.rate_limit, items[0]))

    for al in ActivityLog.objects.filter(pk__in=items):
        # Delete existing entries:
        CommentLog.objects.filter(activity_log=al).delete()

        # Create a new entry:
        if 'comments' in al.details:
            CommentLog(comments=al.details['comments'], activity_log=al).save()


@task
def add_versionlog(items, **kw):
    log.info('[%s@%s] Adding VersionLog starting with ActivityLog: %s' %
             (len(items), add_versionlog.rate_limit, items[0]))

    for al in ActivityLog.objects.filter(pk__in=items):
        # Delete existing entries:
        VersionLog.objects.filter(activity_log=al).delete()

        for a in al.arguments:
            if isinstance(a, Version):
                vl = VersionLog(version=a, activity_log=al)
                vl.save()
                # We need to save it twice to backdate the created date.
                vl.created = al.created
                vl.save()


@task
@use_primary_db
def recalculate_post_review_weight(ids):
    """Recalculate the post-review weight that should be assigned to
    auto-approved add-on versions from a list of ids."""
    addons = Addon.objects.filter(id__in=ids)
    for addon in addons:
        summaries = AutoApprovalSummary.objects.filter(
            version__in=addon.versions.all())

        for summary in summaries:
            summary.calculate_weight()
            summary.save()


@task(acks_late=True)
@use_primary_db
def index_blobs(addon_ids, **kw):
    log.info('Indexing git blobs for add-ons %s-%s. [%s]' % (
        addon_ids[0], addon_ids[-1], len(addon_ids)))
    index_objects(
        addon_ids, Addon, AddonGitRepositoryBlobIndexer.extract_document,
        kw.pop('index', None), None, Addon.unfiltered)
