from __future__ import annotations

from rifferr import plugins
from rifferr.backends import base
from rifferr.backends.base import LibraryBackend


def test_BACKEND_1_protocol_members() -> None:
    members = {
        "capabilities",
        "query",
        "evaluate",
        "matches",
        "apply",
        "import_folder",
        "command_env",
    }
    declared = set(dir(LibraryBackend)) | set(LibraryBackend.__annotations__)
    assert members <= declared


def test_BACKEND_1_protocol_is_runtime_checkable() -> None:
    class NotABackend:
        pass

    assert not isinstance(NotABackend(), LibraryBackend)


def test_BACKEND_1_backends_entry_point_group_is_public() -> None:
    assert plugins.BACKENDS == "rifferr.backends"
    assert plugins.BACKENDS in plugins.ALL_GROUPS


def test_BACKEND_1_run_marker_name() -> None:
    assert base.RUN_MARKER == "rifferr_run"
