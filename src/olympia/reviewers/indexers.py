import copy

from django.core.exceptions import ObjectDoesNotExist

import waffle

import olympia.core.logger
from olympia import amo
from olympia.amo.indexers import BaseSearchIndexer
from olympia.amo.utils import attach_trans_dict
from olympia.amo.celery import create_chunked_tasks_signatures
from olympia.lib.es.utils import create_index


log = olympia.core.logger.getLogger('z.es')


class AddonGitRepositoryBlobIndexer(BaseSearchIndexer):
    @classmethod
    def get_mapping(cls):
        mapping = {
            'blobs': {
                'properties': {
                    # {commit_sha}_{blob_path}
                    'id': {'type': 'text', 'analyzer': 'sha_analyzer'},
                    'commit_sha': {'type': 'keyword'},

                    # Might need to be a `text` I guess for better searching?
                    'path': {'type': 'text', 'analyzer': 'path_analyzer'},
                    'filename': {'type': 'keyword'},
                    'content': {
                        'type': 'text',
                        # Adding word-delimiter to split on camelcase, known
                        # words like 'tab', and punctuation, and eliminate
                        # duplicates.
                        'fields': {
                            # Trigrams for partial matches.
                            'trigrams': {
                                'type': 'text',
                                'analyzer': 'trigram',
                            }
                        }
                    },

                    'language': {'type': 'keyword'},

                    # There should probably a bite more inlined
                    # addon/version fields but let's use that for the moment
                    'addon_id': {'type': 'long'},
                    'version_id': {'type': 'long'},
                    'guid': {'type': 'keyword'},
                    'name': {
                        'type': 'text',
                    },
                    'slug': {'type': 'keyword'},
                    'status': {'type': 'byte'},
                    # extension / statictheme / etc
                    'type': {'type': 'byte'},
                },
            },
        }

        return mapping

    @classmethod
    def extract_document(cls, obj):
        """Extract indexable attributes from an add-on."""

        attrs = ('id', 'average_daily_users', 'bayesian_rating',
                 'contributions', 'created',
                 'default_locale', 'guid', 'hotness', 'icon_hash', 'icon_type',
                 'is_disabled', 'is_experimental', 'last_updated',
                 'modified', 'public_stats', 'requires_payment', 'slug',
                 'status', 'type', 'view_source', 'weekly_downloads')
        data = {attr: getattr(obj, attr) for attr in attrs}

        data['colors'] = None
        if obj.type == amo.ADDON_PERSONA:
            # Personas are compatible with all platforms. They don't have files
            # so we have to fake the info to be consistent with the rest of the
            # add-ons stored in ES.
            data['platforms'] = [amo.PLATFORM_ALL.id]
            try:
                data['has_theme_rereview'] = (
                    obj.persona.rereviewqueuetheme_set.exists())
                # Theme popularity is roughly equivalent to average daily users
                # (the period is not the same and the methodology differs since
                # themes don't have updates, but it's good enough).
                data['average_daily_users'] = obj.persona.popularity
                # 'weekly_downloads' field is used globally to sort, but
                # for themes weekly_downloads don't make much sense, use
                # popularity instead. To keep it comparable with extensions,
                # multiply by 7. (FIXME: could we stop sorting by downloads,
                # even stop exposing downloads numbers in API/pages outside of
                # the statistic-specific pages?)
                data['weekly_downloads'] = obj.persona.popularity * 7
                data['persona'] = {
                    'accentcolor': obj.persona.accentcolor,
                    'author': obj.persona.display_username,
                    'header': obj.persona.header,
                    'footer': obj.persona.footer,
                    'is_new': obj.persona.is_new(),
                    'textcolor': obj.persona.textcolor,
                }
            except ObjectDoesNotExist:
                # The instance won't have a persona while it's being created.
                pass
        else:
            if obj.current_version:
                data['platforms'] = [p.id for p in
                                     obj.current_version.supported_platforms]
            data['has_theme_rereview'] = None

            # Extract dominant colors from static themes.
            if obj.type == amo.ADDON_STATICTHEME:
                first_preview = obj.current_previews.first()
                if first_preview:
                    data['colors'] = first_preview.colors

        data['app'] = [app.id for app in obj.compatible_apps.keys()]
        # Boost by the number of users on a logarithmic scale.
        data['boost'] = float(data['average_daily_users'] ** .2)
        # Quadruple the boost if the add-on is public.
        if (obj.status == amo.STATUS_PUBLIC and not obj.is_experimental and
                'boost' in data):
            data['boost'] = float(max(data['boost'], 1) * 4)
        # We can use all_categories because the indexing code goes through the
        # transformer that sets it.
        data['category'] = [cat.id for cat in obj.all_categories]
        data['current_version'] = cls.extract_version(
            obj, obj.current_version)
        data['listed_authors'] = [
            {'name': a.name, 'id': a.id, 'username': a.username,
             'is_public': a.is_public}
            for a in obj.listed_authors
        ]

        data['is_featured'] = obj.is_featured(None, None)
        data['featured_for'] = [
            {'application': [app], 'locales': list(sorted(
                locales, key=lambda x: x or ''))}
            for app, locales in obj.get_featured_by_app().items()]

        data['has_eula'] = bool(obj.eula)
        data['has_privacy_policy'] = bool(obj.privacy_policy)

        data['previews'] = [{'id': preview.id, 'modified': preview.modified,
                             'sizes': preview.sizes}
                            for preview in obj.current_previews]
        data['ratings'] = {
            'average': obj.average_rating,
            'count': obj.total_ratings,
            'text_count': obj.text_ratings_count,
        }
        # We can use tag_list because the indexing code goes through the
        # transformer that sets it (attach_tags).
        data['tags'] = getattr(obj, 'tag_list', [])

        # Handle localized fields.
        # First, deal with the 3 fields that need everything:
        for field in ('description', 'name', 'summary'):
            data.update(cls.extract_field_api_translations(obj, field))
            data.update(cls.extract_field_search_translation(
                obj, field, obj.default_locale))
            data.update(cls.extract_field_analyzed_translations(obj, field))

        # Then add fields that only need to be returned to the API without
        # contributing to search relevancy.
        for field in ('developer_comments', 'homepage', 'support_email',
                      'support_url'):
            data.update(cls.extract_field_api_translations(obj, field))
        if obj.type != amo.ADDON_STATICTHEME:
            # Also do that for preview captions, which are set on each preview
            # object.
            attach_trans_dict(Preview, obj.current_previews)
            for i, preview in enumerate(obj.current_previews):
                data['previews'][i].update(
                    cls.extract_field_api_translations(preview, 'caption'))

        return data


# addons index settings.
INDEX_SETTINGS = {
    'analysis': {
        'analyzer': {
            'standard_with_word_split': {
                # This analyzer tries to split the text into words by using
                # various methods. It also lowercases them and make sure each
                # token is only returned once.
                # Only use for short things with extremely meaningful content
                # like add-on name - it makes too many modifications to be
                # useful for things like descriptions, for instance.
                'tokenizer': 'standard',
                'filter': [
                    'standard', 'custom_word_delimiter', 'lowercase', 'stop',
                    'custom_dictionary_decompounder', 'unique',
                ]
            },
            'trigram': {
                # Analyzer that splits the text into trigrams.
                'tokenizer': 'ngram_tokenizer',
                'filter': [
                    'lowercase',
                ]
            },
        },
        'tokenizer': {
            'ngram_tokenizer': {
                'type': 'ngram',
                'min_gram': 3,
                'max_gram': 3,
                'token_chars': ['letter', 'digit']
            }
        },
        'normalizer': {
            'lowercase_keyword_normalizer': {
                # By default keywords are indexed 'as-is', but for exact name
                # matches we need to lowercase them before indexing, so this
                # normalizer does that for us.
                'type': 'custom',
                'filter': ['lowercase'],
            },
        },
        'filter': {
            'custom_word_delimiter': {
                # This filter is useful for add-on names that have multiple
                # words sticked together in a way that is easy to recognize,
                # like FooBar, which should be indexed as FooBar and Foo Bar.
                # (preserve_original: True makes us index both the original
                # and the split version.)
                'type': 'word_delimiter',
                'preserve_original': True
            },
            'custom_dictionary_decompounder': {
                # This filter is also useful for add-on names that have
                # multiple words sticked together, but without a pattern that
                # we can automatically recognize. To deal with those, we use
                # a small dictionary of common words. It allows us to index
                # 'awesometabpassword'  as 'awesome tab password', helping
                # users looking for 'tab password' find that add-on.
                'type': 'dictionary_decompounder',
                'word_list': [
                    'all', 'auto', 'ball', 'bar', 'block', 'blog', 'bookmark',
                    'browser', 'bug', 'button', 'cat', 'chat', 'click', 'clip',
                    'close', 'color', 'context', 'cookie', 'cool', 'css',
                    'delete', 'dictionary', 'down', 'download', 'easy', 'edit',
                    'fill', 'fire', 'firefox', 'fix', 'flag', 'flash', 'fly',
                    'forecast', 'fox', 'foxy', 'google', 'grab', 'grease',
                    'html', 'http', 'image', 'input', 'inspect', 'inspector',
                    'iris', 'js', 'key', 'keys', 'lang', 'link', 'mail',
                    'manager', 'map', 'mega', 'menu', 'menus', 'monkey',
                    'name', 'net', 'new', 'open', 'password', 'persona',
                    'privacy', 'query', 'screen', 'scroll', 'search', 'secure',
                    'select', 'smart', 'spring', 'status', 'style', 'super',
                    'sync', 'tab', 'text', 'think', 'this', 'time', 'title',
                    'translate', 'tree', 'undo', 'upload', 'url', 'user',
                    'video', 'window', 'with', 'word', 'zilla',
                ]
            },
        }
    }
}


def create_new_index(index_name=None):
    """
    Create a new index for addons in ES.

    Intended to be used by reindexation (and tests), generally a bad idea to
    call manually.
    """
    if index_name is None:
        index_name = AddonIndexer.get_index_alias()

    index_settings = copy.deepcopy(INDEX_SETTINGS)

    if waffle.switch_is_active('es-use-classic-similarity'):
        # http://bit.ly/es5-similarity-module-docs
        index_settings['similarity'] = {
            'default': {
                'type': 'classic'
            }
        }

    config = {
        'mappings': get_mappings(),
        'settings': {
            # create_index will add its own index settings like number of
            # shards and replicas.
            'index': index_settings
        },
    }
    create_index(index_name, config)


def get_mappings():
    """
    Return a dict with all addons-related ES mappings.
    """
    indexers = (AddonIndexer,)
    return {idxr.get_doctype_name(): idxr.get_mapping() for idxr in indexers}


def reindex_tasks_group(index_name):
    """
    Return the group of tasks to execute for a full reindex of addons on the
    index called `index_name` (which is not an alias but the real index name).
    """
    from olympia.addons.models import Addon
    from olympia.addons.tasks import index_addons

    ids = Addon.unfiltered.values_list('id', flat=True).order_by('id')
    chunk_size = 150
    return create_chunked_tasks_signatures(index_addons, list(ids), chunk_size)
