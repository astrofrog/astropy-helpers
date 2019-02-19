# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This module contains a number of utilities for use during
setup/build/packaging that are useful to astropy as a whole.
"""

from __future__ import absolute_import

import collections
import os
import re
import shutil
import subprocess
import sys
import traceback
import warnings
from configparser import ConfigParser
import builtins

from distutils import log
from distutils.errors import DistutilsOptionError, DistutilsModuleError
from distutils.core import Extension
from distutils.core import Command
from distutils.command.sdist import sdist as DistutilsSdist

from setuptools import setup as setuptools_setup
from setuptools.config import read_configuration
from setuptools import find_packages as _find_packages

from .distutils_helpers import (add_command_option, get_compiler_option,
                                get_dummy_distribution, get_distutils_build_option,
                                get_distutils_build_or_install_option)
from .utils import (walk_skip_hidden, import_file, extends_doc,
                    resolve_name, AstropyDeprecationWarning)

# These imports are not used in this module, but are included for backwards
# compat with older versions of this module
from .utils import get_numpy_include_path, write_if_different  # noqa

__all__ = ['register_commands', 'get_package_info']

_module_state = {'registered_commands': None,
                 'have_sphinx': False,
                 'package_cache': None,
                 'exclude_packages': set(),
                 'excludes_too_late': False}

try:
    import sphinx  # noqa
    _module_state['have_sphinx'] = True
except ValueError as e:
    # This can occur deep in the bowels of Sphinx's imports by way of docutils
    # and an occurrence of this bug: http://bugs.python.org/issue18378
    # In this case sphinx is effectively unusable
    if 'unknown locale' in e.args[0]:
        log.warn(
            "Possible misconfiguration of one of the environment variables "
            "LC_ALL, LC_CTYPES, LANG, or LANGUAGE.  For an example of how to "
            "configure your system's language environment on OSX see "
            "http://blog.remibergsma.com/2012/07/10/"
            "setting-locales-correctly-on-mac-osx-terminal-application/")
except ImportError:
    pass
except SyntaxError:
    # occurs if markupsafe is recent version, which doesn't support Python 3.2
    pass


def add_exclude_packages(excludes):

    if _module_state['excludes_too_late']:
        raise RuntimeError(
            "add_package_excludes must be called before all other setup helper "
            "functions in order to properly handle excluded packages")

    _module_state['exclude_packages'].update(set(excludes))


def get_package_info(srcdir='.', exclude=()):
    """
    Collates all of the information for building all subpackages
    and returns a dictionary of keyword arguments that can
    be passed directly to `distutils.setup`.

    The purpose of this function is to allow subpackages to update the
    arguments to the package's ``setup()`` function in its setup.py
    script, rather than having to specify all extensions/package data
    directly in the ``setup.py``.  See Astropy's own
    ``setup.py`` for example usage and the Astropy development docs
    for more details.

    This function obtains that information by iterating through all
    packages in ``srcdir`` and locating a ``setup_package.py`` module.
    This module can contain the following functions:
    ``get_extensions()``, ``get_package_data()``,
    ``get_build_options()``, and ``get_external_libraries()``.

    Each of those functions take no arguments.

    - ``get_extensions`` returns a list of
      `distutils.extension.Extension` objects.

    - ``get_package_data()`` returns a dict formatted as required by
      the ``package_data`` argument to ``setup()``.

    - ``get_build_options()`` returns a list of tuples describing the
      extra build options to add.

    - ``get_external_libraries()`` returns
      a list of libraries that can optionally be built using external
      dependencies.
    """
    ext_modules = []
    packages = []
    package_dir = {}

    # Read in existing package data, and add to it below
    setup_cfg = os.path.join(srcdir, 'setup.cfg')
    if os.path.exists(setup_cfg):
        conf = read_configuration(setup_cfg)
        if 'options' in conf and 'package_data' in conf['options']:
            package_data = conf['options']['package_data']
        else:
            package_data = {}
    else:
        package_data = {}

    if exclude:
        warnings.warn(
            "Use of the exclude parameter is no longer supported since it does "
            "not work as expected. Use add_exclude_packages instead. Note that "
            "it must be called prior to any other calls from setup helpers.",
            AstropyDeprecationWarning)

    # Use the find_packages tool to locate all packages and modules
    packages = find_packages(srcdir, exclude=exclude)

    # Update package_dir if the package lies in a subdirectory
    if srcdir != '.':
        package_dir[''] = srcdir

    # For each of the setup_package.py modules, extract any
    # information that is needed to install them.  The build options
    # are extracted first, so that their values will be available in
    # subsequent calls to `get_extensions`, etc.
    for setuppkg in iter_setup_packages(srcdir, packages):
        if hasattr(setuppkg, 'get_build_options'):
            options = setuppkg.get_build_options()
            for option in options:
                add_command_option('build', *option)
        if hasattr(setuppkg, 'get_external_libraries'):
            libraries = setuppkg.get_external_libraries()
            for library in libraries:
                add_external_library(library)

    for setuppkg in iter_setup_packages(srcdir, packages):
        # get_extensions must include any Cython extensions by their .pyx
        # filename.
        if hasattr(setuppkg, 'get_extensions'):
            ext_modules.extend(setuppkg.get_extensions())
        if hasattr(setuppkg, 'get_package_data'):
            package_data.update(setuppkg.get_package_data())

    # Locate any .pyx files not already specified, and add their extensions in.
    # The default include dirs include numpy to facilitate numerical work.
    ext_modules.extend(get_cython_extensions(srcdir, packages, ext_modules,
                                             ['numpy']))

    # Now remove extensions that have the special name 'skip_cython', as they
    # exist Only to indicate that the cython extensions shouldn't be built
    for i, ext in reversed(list(enumerate(ext_modules))):
        if ext.name == 'skip_cython':
            del ext_modules[i]

    # On Microsoft compilers, we need to pass the '/MANIFEST'
    # commandline argument.  This was the default on MSVC 9.0, but is
    # now required on MSVC 10.0, but it doesn't seem to hurt to add
    # it unconditionally.
    if get_compiler_option() == 'msvc':
        for ext in ext_modules:
            ext.extra_link_args.append('/MANIFEST')

    if len(ext_modules) > 0:
        main_package_dir = min(packages, key=len)
        src_path = os.path.relpath(os.path.join(os.path.dirname(__file__), 'src'))
        shutil.copy(os.path.join(src_path, 'compiler.c'),
                    os.path.join(srcdir, main_package_dir, '_compiler.c'))
        ext = Extension(main_package_dir + '.compiler_version',
                        [os.path.join(main_package_dir, '_compiler.c')])
        ext_modules.append(ext)

    return {
        'ext_modules': ext_modules,
        'packages': packages,
        'package_dir': package_dir,
        'package_data': package_data,
        }


def iter_setup_packages(srcdir, packages):
    """ A generator that finds and imports all of the ``setup_package.py``
    modules in the source packages.

    Returns
    -------
    modgen : generator
        A generator that yields (modname, mod), where `mod` is the module and
        `modname` is the module name for the ``setup_package.py`` modules.

    """

    for packagename in packages:
        package_parts = packagename.split('.')
        package_path = os.path.join(srcdir, *package_parts)
        setup_package = os.path.relpath(
            os.path.join(package_path, 'setup_package.py'))

        if os.path.isfile(setup_package):
            module = import_file(setup_package,
                                 name=packagename + '.setup_package')
            yield module


def iter_pyx_files(package_dir, package_name):
    """
    A generator that yields Cython source files (ending in '.pyx') in the
    source packages.

    Returns
    -------
    pyxgen : generator
        A generator that yields (extmod, fullfn) where `extmod` is the
        full name of the module that the .pyx file would live in based
        on the source directory structure, and `fullfn` is the path to
        the .pyx file.
    """
    for dirpath, dirnames, filenames in walk_skip_hidden(package_dir):
        for fn in filenames:
            if fn.endswith('.pyx'):
                fullfn = os.path.relpath(os.path.join(dirpath, fn))
                # Package must match file name
                extmod = '.'.join([package_name, fn[:-4]])
                yield (extmod, fullfn)

        break  # Don't recurse into subdirectories


def get_cython_extensions(srcdir, packages, prevextensions=tuple(),
                          extincludedirs=None):
    """
    Looks for Cython files and generates Extensions if needed.

    Parameters
    ----------
    srcdir : str
        Path to the root of the source directory to search.
    prevextensions : list of `~distutils.core.Extension` objects
        The extensions that are already defined.  Any .pyx files already here
        will be ignored.
    extincludedirs : list of str or None
        Directories to include as the `include_dirs` argument to the generated
        `~distutils.core.Extension` objects.

    Returns
    -------
    exts : list of `~distutils.core.Extension` objects
        The new extensions that are needed to compile all .pyx files (does not
        include any already in `prevextensions`).
    """

    # Vanilla setuptools and old versions of distribute include Cython files
    # as .c files in the sources, not .pyx, so we cannot simply look for
    # existing .pyx sources in the previous sources, but we should also check
    # for .c files with the same remaining filename. So we look for .pyx and
    # .c files, and we strip the extension.
    prevsourcepaths = []
    ext_modules = []

    for ext in prevextensions:
        for s in ext.sources:
            if s.endswith(('.pyx', '.c', '.cpp')):
                sourcepath = os.path.realpath(os.path.splitext(s)[0])
                prevsourcepaths.append(sourcepath)

    for package_name in packages:
        package_parts = package_name.split('.')
        package_path = os.path.join(srcdir, *package_parts)

        for extmod, pyxfn in iter_pyx_files(package_path, package_name):
            sourcepath = os.path.realpath(os.path.splitext(pyxfn)[0])
            if sourcepath not in prevsourcepaths:
                ext_modules.append(Extension(extmod, [pyxfn],
                                             include_dirs=extincludedirs))

    return ext_modules


class DistutilsExtensionArgs(collections.defaultdict):
    """
    A special dictionary whose default values are the empty list.

    This is useful for building up a set of arguments for
    `distutils.Extension` without worrying whether the entry is
    already present.
    """
    def __init__(self, *args, **kwargs):
        def default_factory():
            return []

        super(DistutilsExtensionArgs, self).__init__(
            default_factory, *args, **kwargs)

    def update(self, other):
        for key, val in other.items():
            self[key].extend(val)


def pkg_config(packages, default_libraries, executable='pkg-config'):
    """
    Uses pkg-config to update a set of distutils Extension arguments
    to include the flags necessary to link against the given packages.

    If the pkg-config lookup fails, default_libraries is applied to
    libraries.

    Parameters
    ----------
    packages : list of str
        A list of pkg-config packages to look up.

    default_libraries : list of str
        A list of library names to use if the pkg-config lookup fails.

    Returns
    -------
    config : dict
        A dictionary containing keyword arguments to
        `distutils.Extension`.  These entries include:

        - ``include_dirs``: A list of include directories
        - ``library_dirs``: A list of library directories
        - ``libraries``: A list of libraries
        - ``define_macros``: A list of macro defines
        - ``undef_macros``: A list of macros to undefine
        - ``extra_compile_args``: A list of extra arguments to pass to
          the compiler
    """

    flag_map = {'-I': 'include_dirs', '-L': 'library_dirs', '-l': 'libraries',
                '-D': 'define_macros', '-U': 'undef_macros'}
    command = "{0} --libs --cflags {1}".format(executable, ' '.join(packages)),

    result = DistutilsExtensionArgs()

    try:
        pipe = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)
        output = pipe.communicate()[0].strip()
    except subprocess.CalledProcessError as e:
        lines = [
            ("{0} failed. This may cause the build to fail below."
             .format(executable)),
            "  command: {0}".format(e.cmd),
            "  returncode: {0}".format(e.returncode),
            "  output: {0}".format(e.output)
            ]
        log.warn('\n'.join(lines))
        result['libraries'].extend(default_libraries)
    else:
        if pipe.returncode != 0:
            lines = [
                "pkg-config could not lookup up package(s) {0}.".format(
                    ", ".join(packages)),
                "This may cause the build to fail below."
                ]
            log.warn('\n'.join(lines))
            result['libraries'].extend(default_libraries)
        else:
            for token in output.split():
                # It's not clear what encoding the output of
                # pkg-config will come to us in.  It will probably be
                # some combination of pure ASCII (for the compiler
                # flags) and the filesystem encoding (for any argument
                # that includes directories or filenames), but this is
                # just conjecture, as the pkg-config documentation
                # doesn't seem to address it.
                arg = token[:2].decode('ascii')
                value = token[2:].decode(sys.getfilesystemencoding())
                if arg in flag_map:
                    if arg == '-D':
                        value = tuple(value.split('=', 1))
                    result[flag_map[arg]].append(value)
                else:
                    result['extra_compile_args'].append(value)

    return result


def add_external_library(library):
    """
    Add a build option for selecting the internal or system copy of a library.

    Parameters
    ----------
    library : str
        The name of the library.  If the library is `foo`, the build
        option will be called `--use-system-foo`.
    """

    for command in ['build', 'build_ext', 'install']:
        add_command_option(command, str('use-system-' + library),
                           'Use the system {0} library'.format(library),
                           is_bool=True)


def use_system_library(library):
    """
    Returns `True` if the build configuration indicates that the given
    library should use the system copy of the library rather than the
    internal one.

    For the given library `foo`, this will be `True` if
    `--use-system-foo` or `--use-system-libraries` was provided at the
    commandline or in `setup.cfg`.

    Parameters
    ----------
    library : str
        The name of the library

    Returns
    -------
    use_system : bool
        `True` if the build should use the system copy of the library.
    """
    return (
        get_distutils_build_or_install_option('use_system_{0}'.format(library)) or
        get_distutils_build_or_install_option('use_system_libraries'))


@extends_doc(_find_packages)
def find_packages(where='.', exclude=(), invalidate_cache=False):
    """
    This version of ``find_packages`` caches previous results to speed up
    subsequent calls.  Use ``invalide_cache=True`` to ignore cached results
    from previous ``find_packages`` calls, and repeat the package search.
    """

    if exclude:
        warnings.warn(
            "Use of the exclude parameter is no longer supported since it does "
            "not work as expected. Use add_exclude_packages instead. Note that "
            "it must be called prior to any other calls from setup helpers.",
            AstropyDeprecationWarning)

    # Calling add_exclude_packages after this point will have no effect
    _module_state['excludes_too_late'] = True

    if not invalidate_cache and _module_state['package_cache'] is not None:
        return _module_state['package_cache']

    packages = _find_packages(
        where=where, exclude=list(_module_state['exclude_packages']))
    _module_state['package_cache'] = packages

    return packages
