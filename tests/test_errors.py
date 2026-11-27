"""Every error the core defines is a ReWiiError; the CLI tells user errors from bugs by that."""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import rewii.core
from rewii import cli
from rewii.core.errors import ReWiiError
from rewii.core.fileops import DriveNotConnected, UnsafePath
from rewii.core.progress import Cancelled
from rewii.core.wad import Refused, WadError


def _core_exceptions():
    for m in pkgutil.iter_modules(rewii.core.__path__):
        mod = importlib.import_module(f"rewii.core.{m.name}")
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseException) and obj.__module__ == mod.__name__:
                yield obj


def test_every_core_exception_is_a_rewii_error():
    found = list(_core_exceptions())
    assert len(found) > 25
    assert [e.__name__ for e in found if not issubclass(e, ReWiiError) and e is not Cancelled] == []
    assert not issubclass(Cancelled, ReWiiError)            # cancelling is not an error


def test_second_bases_are_kept():
    assert issubclass(UnsafePath, ValueError)
    assert issubclass(DriveNotConnected, OSError) and issubclass(Refused, WadError)
    e = DriveNotConnected("/nowhere")
    assert "isn't connected" in str(e) and e.filename == "/nowhere"


def test_cli_user_errors_and_cancel(capsys, monkeypatch):
    assert cli.is_user_error(WadError("x")) and cli.is_user_error(OSError("x"))
    assert not cli.is_user_error(ValueError("bug")) and not cli.is_user_error(KeyError("bug"))

    def cancelled(a):
        raise Cancelled()
    monkeypatch.setattr(cli, "cmd_drives", cancelled)
    assert cli.main(["drives"]) == 130
    err = capsys.readouterr().err
    assert err.strip() == "Cancelled." and "error" not in err

    def bug(a):
        raise ValueError("a bug")
    monkeypatch.setattr(cli, "cmd_drives", bug)
    with pytest.raises(ValueError):
        cli.main(["drives"])
