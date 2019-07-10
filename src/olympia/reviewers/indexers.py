import copy

import olympia.core.logger
from olympia.amo.indexers import BaseSearchIndexer
from olympia.amo.celery import create_chunked_tasks_signatures
from olympia.lib.es.utils import create_index


log = olympia.core.logger.getLogger('z.es')


class AddonGitRepositoryBlobIndexer(BaseSearchIndexer):
    @classmethod
    def get_doctype_name(cls):
        """Return the document type name for this indexer. We default to simply
        use the db table from the corresponding model."""
        return 'git_blobs'

    @classmethod
    def get_model(cls):
        from olympia.addons.models import Addon
        return Addon

    @classmethod
    def get_mapping(cls):
        doc_name = cls.get_doctype_name()

        mapping = {
            doc_name: {
                'properties': {
                    # {commit_sha}_{blob_path}
                    'id': {'type': 'text'},
                    'commit_sha': {'type': 'keyword'},

                    # # Might need to be a `text` I guess for better searching?
                    'path': {'type': 'text', 'analyzer': 'lowercase'},
                    'filename': {'type': 'keyword'},
                    'content': {
                        'type': 'text',
                        'fields': {
                            # Trigrams for partial matches to speed up regular
                            # expression searches
                            'trigrams': {
                                'type': 'text',
                                'analyzer': 'trigram',
                            }
                        }
                    },

                    'language': {'type': 'keyword'},

                    # # There should probably be a bit more inlined
                    # # addon/version fields but let's use that for the moment
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
        print('EXTRACT', obj)
        data = {}

        return data


# addons index settings.
INDEX_SETTINGS = {
    'analysis': {
        'analyzer': {
            'trigram': {
                # Analyzer that splits the text into trigrams.
                'tokenizer': 'ngram_tokenizer',
                'filter': [
                    'lowercase',
                ]
            },
            'lowercase': {  # Not used here but defined for plugins' use
                'type': 'custom',
                'filter': ['lowercase'],
                'tokenizer': 'keyword'
            }
        },
        'tokenizer': {
            'ngram_tokenizer': {
                'type': 'ngram',
                'min_gram': 3,
                'max_gram': 3,
                'token_chars': ['letter', 'digit']
            }
        },
    }
}


def create_new_index(index_name=None):
    """
    Create a new index for addons in ES.

    Intended to be used by reindexation (and tests), generally a bad idea to
    call manually.
    """
    if index_name is None:
        index_name = AddonGitRepositoryBlobIndexer.get_index_alias()

    index_settings = copy.deepcopy(INDEX_SETTINGS)

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
    indexers = (AddonGitRepositoryBlobIndexer,)
    print('XXXXXXXXXXXXXXXXXXXXXXXXXXXXX', indexers)
    return {idxr.get_doctype_name(): idxr.get_mapping() for idxr in indexers}


def reindex_tasks_group(index_name):
    """
    Return the group of tasks to execute for a full reindex of addons on the
    index called `index_name` (which is not an alias but the real index name).
    """
    from olympia.addons.models import Addon
    from olympia.reviewers.tasks import index_blobs

    ids = Addon.unfiltered.values_list('id', flat=True).order_by('id')
    chunk_size = 150
    print('AAAAAAAAAAAAAA', ids)
    return create_chunked_tasks_signatures(index_blobs, list(ids), chunk_size)
