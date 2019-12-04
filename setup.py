from setuptools import setup, find_packages

setup(
    name='sync',
    version='0.1',
    description='Synchronize changes between gecko and web-platform-tests',
    url='https://github.com/mozilla/wpt-sync',
    author='Mozilla',
    author_email='mozilla-tools@lists.mozilla.org',
    license='MPL 2.0',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Programming Language :: Python :: 2.7',
    ],
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'wptsync = sync.command:main'
        ]
    },
    install_requires=[
        'bugsy>=0.10.1',
        'celery>=4.1',
        'enum34>=1.1.6',
        'GitPython>=2.1.8',
        'PyGithub>=1.35',
        'Mercurial>=4.4',
        'requests>=2.18',
        'filelock>=2.0.13',
        'slugid>=1.0.7',
        'taskcluster>=3.0.0',
        'phabricator>=0.7.0'
    ],
)
