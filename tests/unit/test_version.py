from importlib.metadata import version

import horizon


def test_public_version_matches_installed_package_metadata():
    assert horizon.__version__ == version("horizon-coding-harness")
