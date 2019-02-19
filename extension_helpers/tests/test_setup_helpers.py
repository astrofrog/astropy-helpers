import os
import sys
import stat
import shutil
import importlib
import contextlib

import pytest

from textwrap import dedent

from setuptools import Distribution

from ..setup_helpers import get_package_info

from . import reset_setup_helpers, reset_distutils_log  # noqa
from . import run_setup, cleanup_import

extension_helpers_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _extension_test_package(tmpdir, request, extension_type='c', include_numpy=False):
    """Creates a simple test package with an extension module."""

    test_pkg = tmpdir.mkdir('test_pkg')
    test_pkg.mkdir('exthtest_eva').ensure('__init__.py')

    # TODO: It might be later worth making this particular test package into a
    # reusable fixture for other build_ext tests

    if extension_type in ('c', 'both'):
        # A minimal C extension for testing
        test_pkg.join('exthtest_eva', 'unit01.c').write(dedent("""\
            #include <Python.h>
            #ifndef PY3K
            #if PY_MAJOR_VERSION >= 3
            #define PY3K 1
            #else
            #define PY3K 0
            #endif
            #endif

            #if PY3K
            static struct PyModuleDef moduledef = {
                PyModuleDef_HEAD_INIT,
                "unit01",
                NULL,
                -1,
                NULL
            };
            PyMODINIT_FUNC
            PyInit_unit01(void) {
                return PyModule_Create(&moduledef);
            }
            #else
            PyMODINIT_FUNC
            initunit01(void) {
                Py_InitModule3("unit01", NULL, NULL);
            }
            #endif
        """))

    if extension_type in ('pyx', 'both'):
        # A minimal Cython extension for testing
        test_pkg.join('exthtest_eva', 'unit02.pyx').write(dedent("""\
            print("Hello cruel angel.")
        """))

    if extension_type == 'c':
        extensions = ['unit01.c']
    elif extension_type == 'pyx':
        extensions = ['unit02.pyx']
    elif extension_type == 'both':
        extensions = ['unit01.c', 'unit02.pyx']

    include_dirs = ['numpy'] if include_numpy else []

    extensions_list = [
        "Extension('exthtest_eva.{0}', [join('exthtest_eva', '{1}')], include_dirs={2})".format(
            os.path.splitext(extension)[0], extension, include_dirs)
        for extension in extensions]

    test_pkg.join('exthtest_eva', 'setup_package.py').write(dedent("""\
        from setuptools import Extension
        from os.path import join
        def get_extensions():
            return [{0}]
    """.format(', '.join(extensions_list))))

    test_pkg.join('setup.py').write(dedent("""\
        import sys
        from os.path import join
        from setuptools import setup
        sys.path.insert(0, r'{extension_helpers_path}')
        from extension_helpers.setup_helpers import get_package_info

        setup(
            name='exthtest_eva',
            version='0.1',
            **get_package_info()
        )
    """.format(extension_helpers_path=extension_helpers_PATH)))

    if '' in sys.path:
        sys.path.remove('')

    sys.path.insert(0, '')

    def finalize():
        cleanup_import('exthtest_eva')

    request.addfinalizer(finalize)

    return test_pkg


@pytest.fixture
def extension_test_package(tmpdir, request):
    return _extension_test_package(tmpdir, request, extension_type='both')


@pytest.fixture
def c_extension_test_package(tmpdir, request):
    # Check whether numpy is installed in the test environment
    has_numpy = bool(importlib.util.find_spec('numpy'))
    return _extension_test_package(tmpdir, request, extension_type='c',
                                   include_numpy=has_numpy)


@pytest.fixture
def pyx_extension_test_package(tmpdir, request):
    return _extension_test_package(tmpdir, request, extension_type='pyx')


def test_cython_autoextensions(tmpdir):
    """
    Ensures that Cython extensions in sub-packages are discovered and built
    only once.
    """

    # Make a simple test package
    test_pkg = tmpdir.mkdir('test_pkg')
    test_pkg.mkdir('yoda').mkdir('luke')
    test_pkg.ensure('yoda', '__init__.py')
    test_pkg.ensure('yoda', 'luke', '__init__.py')
    test_pkg.join('yoda', 'luke', 'dagobah.pyx').write(
        """def testfunc(): pass""")

    # Required, currently, for get_package_info to work
    package_info = get_package_info(str(test_pkg))

    assert len(package_info['ext_modules']) == 2
    assert package_info['ext_modules'][0].name == 'yoda.luke.dagobah'


def test_compiler_module(capsys, c_extension_test_package):
    """
    Test ensuring that the compiler module is built and installed for packages
    that have extension modules.
    """

    test_pkg = c_extension_test_package
    install_temp = test_pkg.mkdir('install_temp')

    with test_pkg.as_cwd():
        # This is one of the simplest ways to install just a package into a
        # test directory
        run_setup('setup.py',
                  ['install',
                   '--single-version-externally-managed',
                   '--install-lib={0}'.format(install_temp),
                   '--record={0}'.format(install_temp.join('record.txt'))])

    with install_temp.as_cwd():
        import exthtest_eva
        # Make sure we imported the exthtest_eva package from the correct place
        dirname = os.path.abspath(os.path.dirname(exthtest_eva.__file__))
        assert dirname == str(install_temp.join('exthtest_eva'))

        import exthtest_eva.compiler_version
        assert exthtest_eva.compiler_version != 'unknown'


def test_no_cython_buildext(capsys, c_extension_test_package, monkeypatch):
    """
    Regression test for https://github.com/astropy/astropy-helpers/pull/35

    This tests the custom build_ext command installed by extension_helpers when
    used with a project that has no Cython extensions (but does have one or
    more normal C extensions).
    """

    test_pkg = c_extension_test_package

    with test_pkg.as_cwd():

        run_setup('setup.py', ['build_ext', '--inplace'])

    sys.path.insert(0, str(test_pkg))

    try:
        import exthtest_eva.unit01
        dirname = os.path.abspath(os.path.dirname(exthtest_eva.unit01.__file__))
        assert dirname == str(test_pkg.join('exthtest_eva'))
    finally:
        sys.path.remove(str(test_pkg))
