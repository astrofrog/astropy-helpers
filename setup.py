#!/usr/bin/env python
# Licensed under a 3-clause BSD style license - see LICENSE.rst

import setuptools_bootstrap
import pkg_resources
from setuptools import setup
from astropy_helpers.version_helpers import generate_version_py

NAME = 'astropy_helpers'
VERSION = '0.4.dev'
RELEASE = 'dev' not in VERSION
DOWNLOAD_BASE_URL = 'http://pypi.python.org/packages/source/a/astropy-helpers'

generate_version_py(NAME, VERSION, RELEASE, False)

# Use the updated version including the git rev count
from astropy_helpers.version import version as VERSION

setup(
    name=pkg_resources.safe_name(NAME),  # astropy_helpers -> astropy-helpers
    version=VERSION,
    description='',
    author='The Astropy Developers',
    author_email='astropy.team@gmail.com',
    license='BSD',
    url='http://astropy.org',
    long_description='',
    packages=['astropy_helpers', 'astropy_helpers.sphinx', 'astropy_helpers.sphinx.ext', 'astropy_helpers.sphinx.ext.tests'],
    package_data={'astropy_helpers.sphinx':['themes'], 'astropy_helpers.sphinx.ext':['templates']},
    download_url='{0}/astropy-{1}.tar.gz'.format(DOWNLOAD_BASE_URL, VERSION),
    classifiers=[],
    cmdclass={},
    zip_safe=False,
)
