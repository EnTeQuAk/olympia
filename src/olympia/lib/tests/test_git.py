import os
import subprocess

import pytest

from django.conf import settings

from olympia import amo
from olympia.amo.tests import addon_factory
from olympia.lib.git import AddonGitRepository


def test_git_repo_init():
    # This actually works completely without any add-on object and only
    # creates the necessary file structure
    repo = AddonGitRepository(1)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, str(1), 'package')
    assert os.listdir(repo.git_repository_path) == ['.git']


def test_git_repo_init_opens_existing_repo():
    expected_path = os.path.join(
        settings.GIT_FILE_STORAGE_PATH, str(1), 'package')

    assert not os.path.exists(expected_path)
    repo = AddonGitRepository(1)
    assert os.path.exists(expected_path)

    repo2 = AddonGitRepository(1)
    assert repo.git_repository_path == repo2.git_repository_path


@pytest.mark.django_db
def test_extract_and_commit_from_file_obj():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_file_obj(
        addon.current_version.all_files[0],
        amo.RELEASE_CHANNEL_LISTED)

    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, str(addon.id), 'package')
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
