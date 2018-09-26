# -*- coding: utf-8 -*-
import uuid
import os
import shutil
import tempfile

import pygit2

from django.conf import settings

from olympia import amo
from olympia.files.utils import SafeZip


BRANCHES = {
    amo.RELEASE_CHANNEL_LISTED: 'listed',
    amo.RELEASE_CHANNEL_UNLISTED: 'unlisted'
}


class AddonGitRepository(object):

    def __init__(self, addon_id, package_type='package'):
        assert package_type in ('package', 'source')

        self.repository_path = os.path.join(
            settings.GIT_FILE_STORAGE_PATH,
            str(addon_id),
            package_type)

        if not os.path.exists(self.repository_path):
            os.makedirs(self.repository_path)
            self.repository = pygit2.init_repository(
                path=self.repository_path,
                mode=settings.GIT_FILE_STORAGE_PERMISSIONS,
                bare=False)
            # Write first commit to 'master' to act as HEAD
            tree = self.repository.TreeBuilder().write()
            commit_oid = self.repository.create_commit(
                'HEAD',  # ref
                self.get_author(),  # author
                self.get_author(),  # commitor
                'Initializing repository',  # message
                tree,  # tree
                [])  # parents
            print('Initialized,', self.repository[commit_oid])

        else:
            self.repository = pygit2.Repository(self.repository_path)

    @staticmethod
    def extract_and_commit_from_file_obj(file_obj):
        """Extract all files from `file_obj` and comit them.

        This ignores the fact that there may be a race-condition of two
        versions being created at the same time. When this happens
        we kinda have to hope for the best and hope for the best version to
        be committed last. Given that we are saving the git-sha in the database
        of the previous version we should still end up with the correct diff.
        """
        addon = file_obj.version.addon
        repo = AddonGitRepository(addon.id)

        source_path = file_obj.current_file_path

        tempdir = tempfile.mkdtemp(dir=settings.TMP_PATH)
        tmp_worktree_id = uuid.uuid4().hex
        worktree = repo.repository.add_worktree(
            tmp_worktree_id,
            os.path.join(tempdir, 'data'))

        tmp_repo = pygit2.Repository(worktree.path)

        # Clean the workdir
        for entry in os.listdir(tmp_repo.workdir):
            path = os.path.join(tmp_repo.workdir, entry)

            if entry == '.git':
                continue

            if os.path.isfile(path):
                os.unlink(path)
            else:
                shutil.rmtree(path)

        # Now extract the zip to the workdir
        zip_file = SafeZip(source_path, force_fsync=True)
        zip_file.extract_to_dest(tmp_repo.workdir)

        import ipdb; ipdb.set_trace()

    # # If something changed, add files to the index and commit
    # for path, flags in repo.status().items():
    #     if flags == pygit2.GIT_STATUS_CURRENT or flags == pygit2.GIT_STATUS_IGNORED:
    #         continue

    #     logging.info("     Comitting changes")

    #     repo.index.add_all()
    #     repo.index.write()
    #     tree = repo.index.write_tree()
    #     repo.create_commit(branch, author, author, "Update", tree, [repo.head.target])
    #     logging.info("     Pushing branch")
    #     repo.remotes['origin'].push(['+' + branch], callbacks=gitcallbacks)

    #     break


    #     if addon.type == amo.ADDON_SEARCH and repo.src.endswith('.xml'):
    #         shutil.copyfile(
    #             source_path,
    #             os.path.join(repo.repository_path, file_obj.filename))
    #         #
    #     else:
    #         worktree = repo.add_worktree()






    # # Walk the template directory
    # for root, dirs, files in os.walk(templatedir):
    #     for template in files:
    #         outputfile, ext = os.path.splitext(template)

    #         # Only process files that have a .j2 extension
    #         if ext == '.j2':
    #             subpath = os.path.relpath(root, start=templatedir)
    #             outputpath = os.path.join(repo.workdir, subpath)

    #             # Make sure the directories exist
    #             os.makedirs(outputpath, exist_ok=True)

    #             # Process the template
    #             environment.get_template(os.path.join(subpath, template)).stream(context).dump(os.path.join(outputpath, outputfile))

    #             # Make sure file permissions match
    #             mode = os.lstat(os.path.join(root, template)).st_mode
    #             os.chmod(os.path.join(outputpath, outputfile), mode)











    def get_author(self):
        return pygit2.Signature(
            name='Mozilla Add-ons Robot',
            email='addons-dev-automation+github@mozilla.com')

    def find_or_create_branch(self, name, checkout=False):
        branch = self.repository.branches.get(name)

        if branch is None:
            branch = self.repository.branches.local.create(
                name=name, commit=self.repository.head.get_object())

        return branch
