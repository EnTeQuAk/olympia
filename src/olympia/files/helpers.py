import codecs
import mimetypes
import os
import stat

from django.conf import settings
from django.core.files.storage import default_storage as storage
from django.utils.datastructures import SortedDict
from django.utils.encoding import smart_unicode
from django.utils.translation import get_language
from django.template.defaultfilters import filesizeformat
from validator.testcases.packagelayout import (
    blacklisted_extensions, blacklisted_magic_numbers)

import jinja2
import commonware.log
from cache_nuggets.lib import memoize, Message
from jingo import register, env
from tower import ugettext as _

from olympia import amo
from olympia.amo.utils import rm_local_tmp_dir
from olympia.amo.urlresolvers import reverse
from olympia.files.utils import extract_xpi, get_md5

# Allow files with a shebang through.
blacklisted_magic_numbers = [b for b in list(blacklisted_magic_numbers)
                             if b != (0x23, 0x21)]
blacklisted_extensions = [b for b in list(blacklisted_extensions)
                          if b != 'sh']
task_log = commonware.log.getLogger('z.task')


@register.function
def file_viewer_class(value, key):
    result = []
    if value['directory']:
        result.append('directory closed')
    else:
        result.append('file')
    if value['short'] == key:
        result.append('selected')
    if value.get('diff'):
        result.append('diff')
    return ' '.join(result)


@register.function
def file_tree(files, selected):
    depth = 0
    output = ['<ul class="root">']
    t = env.get_template('files/node.html')
    for k, v in files.items():
        if v['depth'] > depth:
            output.append('<ul class="js-hidden">')
        elif v['depth'] < depth:
            output.extend(['</ul>' for x in range(v['depth'], depth)])
        output.append(t.render({'value': v, 'selected': selected}))
        depth = v['depth']
    output.extend(['</ul>' for x in range(depth, -1, -1)])
    return jinja2.Markup('\n'.join(output))


class FileViewer(object):
    """
    Provide access to a storage-managed file by copying it locally and
    extracting info from it. `src` is a storage-managed path and `dest` is a
    local temp path.
    """

    def __init__(self, file_obj):
        self.file = file_obj
        self.addon = self.file.version.addon
        self.src = file_obj.current_file_path
        self.dest = os.path.join(settings.TMP_PATH, 'file_viewer',
                                 str(file_obj.pk))
        self._files, self.selected = None, None

    def __str__(self):
        return str(self.file.id)

    def _extraction_cache_key(self):
        return ('%s:file-viewer:extraction-in-progress:%s' %
                (settings.CACHE_PREFIX, self.file.id))

    def extract(self):
        """
        Will make all the directories and expand the files.
        Raises error on nasty files.
        """
        try:
            os.makedirs(os.path.dirname(self.dest))
        except OSError, err:
            pass

        if self.is_search_engine() and self.src.endswith('.xml'):
            try:
                os.makedirs(self.dest)
            except OSError, err:
                pass
            copyfileobj(storage.open(self.src),
                        open(os.path.join(self.dest,
                                          self.file.filename), 'w'))
        else:
            try:
                extract_xpi(self.src, self.dest, expand=True)
            except Exception, err:
                task_log.error('Error (%s) extracting %s' % (err, self.src))
                raise

    def cleanup(self):
        if os.path.exists(self.dest):
            rm_local_tmp_dir(self.dest)

    def is_search_engine(self):
        """Is our file for a search engine?"""
        return self.file.version.addon.type == amo.ADDON_SEARCH

    def is_extracted(self):
        """If the file has been extracted or not."""
        return (os.path.exists(self.dest) and not
                Message(self._extraction_cache_key()).get())

    def _is_binary(self, mimetype, path):
        """Uses the filename to see if the file can be shown in HTML or not."""
        # Re-use the blacklisted data from amo-validator to spot binaries.
        ext = os.path.splitext(path)[1][1:]
        if ext in blacklisted_extensions:
            return True

        if os.path.exists(path) and not os.path.isdir(path):
            with storage.open(path, 'r') as rfile:
                bytes = tuple(map(ord, rfile.read(4)))
            if any(bytes[:len(x)] == x for x in blacklisted_magic_numbers):
                return True

        if mimetype:
            major, minor = mimetype.split('/')
            if major == 'image':
                return 'image'  # Mark that the file is binary, but an image.

        return False

    def read_file(self, allow_empty=False):
        """
        Reads the file. Imposes a file limit and tries to cope with
        UTF-8 and UTF-16 files appropriately. Return file contents and
        a list of error messages.
        """
        try:
            file_data = self._read_file(allow_empty)
            return file_data
        except (IOError, OSError):
            self.selected['msg'] = _('That file no longer exists.')
            return ''

    def _read_file(self, allow_empty=False):
        if not self.selected and allow_empty:
            return ''
        assert self.selected, 'Please select a file'
        if self.selected['size'] > settings.FILE_VIEWER_SIZE_LIMIT:
            # L10n: {0} is the file size limit of the file viewer.
            msg = _(u'File size is over the limit of {0}.').format(
                filesizeformat(settings.FILE_VIEWER_SIZE_LIMIT))
            self.selected['msg'] = msg
            return ''

        with storage.open(self.selected['full'], 'r') as opened:
            cont = opened.read()
            codec = 'utf-16' if cont.startswith(codecs.BOM_UTF16) else 'utf-8'
            try:
                return cont.decode(codec)
            except UnicodeDecodeError:
                cont = cont.decode(codec, 'ignore')
                # L10n: {0} is the filename.
                self.selected['msg'] = (
                    _('Problems decoding {0}.').format(codec))
                return cont

    def select(self, file_):
        self.selected = self.get_files().get(file_)

    def is_binary(self):
        if self.selected:
            binary = self.selected['binary']
            if binary and (binary != 'image'):
                self.selected['msg'] = _('This file is not viewable online. '
                                         'Please download the file to view '
                                         'the contents.')
            return binary

    def is_directory(self):
        if self.selected:
            if self.selected['directory']:
                self.selected['msg'] = _('This file is a directory.')
            return self.selected['directory']

    def get_default(self, key=None):
        """Gets the default file and copes with search engines."""
        if key:
            return key

        files = self.get_files()
        for manifest in ('install.rdf', 'manifest.json', 'package.json'):
            if manifest in files:
                return manifest
        return files.keys()[0] if files else None  # Eg: it's a search engine.

    def get_files(self):
        """
        Returns a SortedDict, ordered by the filename of all the files in the
        addon-file. Full of all the useful information you'll need to serve
        this file, build templates etc.
        """
        if self._files:
            return self._files

        if not self.is_extracted():
            return {}
        # In case a cron job comes along and deletes the files
        # mid tree building.
        try:
            self._files = self._get_files(locale=get_language())
            return self._files
        except (OSError, IOError):
            return {}

    def truncate(self, filename, pre_length=15,
                 post_length=10, ellipsis=u'..'):
        """
        Truncates a filename so that
           somelongfilename.htm
        becomes:
           some...htm
        as it truncates around the extension.
        """
        root, ext = os.path.splitext(filename)
        if len(root) > pre_length:
            root = root[:pre_length] + ellipsis
        if len(ext) > post_length:
            ext = ext[:post_length] + ellipsis
        return root + ext

    def get_syntax(self, filename):
        """
        Converts a filename into a syntax for the syntax highlighter, with
        some modifications for specific common mozilla files.
        The list of syntaxes is from:
        http://alexgorbatchev.com/SyntaxHighlighter/manual/brushes/
        """
        if filename:
            short = os.path.splitext(filename)[1][1:]
            syntax_map = {'xul': 'xml', 'rdf': 'xml', 'jsm': 'js',
                          'json': 'js'}
            short = syntax_map.get(short, short)
            if short in ['actionscript3', 'as3', 'bash', 'shell', 'cpp', 'c',
                         'c#', 'c-sharp', 'csharp', 'css', 'diff', 'html',
                         'java', 'javascript', 'js', 'jscript', 'patch',
                         'pas', 'php', 'plain', 'py', 'python', 'sass',
                         'scss', 'text', 'sql', 'vb', 'vbnet', 'xml', 'xhtml',
                         'xslt']:
                return short
        return 'plain'

    @memoize(prefix='file-viewer', time=60 * 60)
    def _get_files(self, locale=None):
        """We need the `locale` parameter for the memoization.

        The `@memoize` decorator uses the prefix *and the parameters* to come
        up with a memoize key. We thus add a (seemingly useless) `locale`
        parameter.

        Otherwise, we would just always have the urls for the files with the
        locale from the first person checking them.
        """
        all_files, res = [], SortedDict()
        # Not using os.path.walk so we get just the right order.

        def iterate(path):
            path_dirs, path_files = storage.listdir(path)
            for dirname in sorted(path_dirs):
                full = os.path.join(path, dirname)
                all_files.append(full)
                iterate(full)

            for filename in sorted(path_files):
                full = os.path.join(path, filename)
                all_files.append(full)

        iterate(self.dest)

        for path in all_files:
            filename = smart_unicode(os.path.basename(path), errors='replace')
            short = smart_unicode(path[len(self.dest) + 1:], errors='replace')
            mime, encoding = mimetypes.guess_type(filename)
            directory = os.path.isdir(path)

            res[short] = {
                'binary': self._is_binary(mime, path),
                'depth': short.count(os.sep),
                'directory': directory,
                'filename': filename,
                'full': path,
                'md5': get_md5(path) if not directory else '',
                'mimetype': mime or 'application/octet-stream',
                'syntax': self.get_syntax(filename),
                'modified': os.stat(path)[stat.ST_MTIME],
                'short': short,
                'size': os.stat(path)[stat.ST_SIZE],
                'truncated': self.truncate(filename),
                'url': reverse('files.list',
                               args=[self.file.id, 'file', short]),
                'url_serve': reverse('files.redirect',
                                     args=[self.file.id, short]),
                'version': self.file.version.version,
            }

        return res


class DiffHelper(object):

    def __init__(self, left, right):
        self.left = FileViewer(left)
        self.right = FileViewer(right)
        self.addon = self.left.addon
        self.key = None

    def __str__(self):
        return '%s:%s' % (self.left, self.right)

    def extract(self):
        self.left.extract(), self.right.extract()

    def cleanup(self):
        self.left.cleanup(), self.right.cleanup()

    def is_extracted(self):
        return self.left.is_extracted() and self.right.is_extracted()

    def get_url(self, short):
        return reverse('files.compare',
                       args=[self.left.file.id, self.right.file.id,
                             'file', short])

    def get_files(self):
        """
        Get the files from the primary and:
        - remap any diffable ones to the compare url as opposed to the other
        - highlight any diffs
        """
        left_files = self.left.get_files()
        right_files = self.right.get_files()
        different = []
        for key, file in left_files.items():
            file['url'] = self.get_url(file['short'])
            diff = file['md5'] != right_files.get(key, {}).get('md5')
            file['diff'] = diff
            if diff:
                different.append(file)

        # Now mark every directory above each different file as different.
        for diff in different:
            for depth in range(diff['depth']):
                key = '/'.join(diff['short'].split('/')[:depth + 1])
                if key in left_files:
                    left_files[key]['diff'] = True

        return left_files

    def get_deleted_files(self):
        """
        Get files that exist in right, but not in left. These
        are files that have been deleted between the two versions.
        Every element will be marked as a diff.
        """
        different = SortedDict()
        if self.right.is_search_engine():
            return different

        def keep(path):
            if path not in different:
                copy = dict(right_files[path])
                copy.update({'url': self.get_url(file['short']), 'diff': True})
                different[path] = copy

        left_files = self.left.get_files()
        right_files = self.right.get_files()
        for key, file in right_files.items():
            if key not in left_files:
                # Make sure we have all the parent directories of
                # deleted files.
                dir = key
                while os.path.dirname(dir):
                    dir = os.path.dirname(dir)
                    keep(dir)

                keep(key)

        return different

    def read_file(self):
        """Reads both selected files."""
        return [self.left.read_file(allow_empty=True),
                self.right.read_file(allow_empty=True)]

    def select(self, key):
        """
        Select a file and adds the file object to self.one and self.two
        for later fetching. Does special work for search engines.
        """
        self.key = key
        self.left.select(key)
        if key and self.right.is_search_engine():
            # There's only one file in a search engine.
            key = self.right.get_default()

        self.right.select(key)
        return self.left.selected and self.right.selected

    def is_binary(self):
        """Tells you if both selected files are binary."""
        return (self.left.is_binary() or
                self.right.is_binary())

    def is_diffable(self):
        """Tells you if the selected files are diffable."""
        if not self.left.selected and not self.right.selected:
            return False

        for obj in [self.left, self.right]:
            if obj.is_binary():
                return False
            if obj.is_directory():
                return False
        return True


def copyfileobj(fsrc, fdst, length=64 * 1024):
    """copy data from file-like object fsrc to file-like object fdst"""
    while True:
        buf = fsrc.read(length)
        if not buf:
            break
        fdst.write(buf)


def rmtree(prefix):
    dirs, files = storage.listdir(prefix)
    for fname in files:
        storage.delete(os.path.join(prefix, fname))
    for d in dirs:
        rmtree(os.path.join(prefix, d))
    storage.delete(prefix)
