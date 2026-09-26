from pathlib import Path

from muca_server.run_server import DEFAULT_DB_DIR


def test_default_db_dir_is_absolute_not_relative_to_cwd():
    # Restarting the server from a different shell/cwd must not silently
    # create a second, empty database instead of finding the one from
    # before -- that requires the default to not depend on cwd at all.
    assert DEFAULT_DB_DIR.is_absolute()
    # Beside the package, not inside it: data never lands among source.
    assert DEFAULT_DB_DIR == Path(__file__).resolve().parent.parent / "data"
