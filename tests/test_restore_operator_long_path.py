"""Actual Windows long-path private custody; isolated synthetic scratch only."""
from __future__ import annotations

import importlib
import ctypes
import os
from pathlib import Path
import stat

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows Win32 path contract')


def operator():
    return importlib.import_module('secretary.infrastructure.restore_operator')


def native_name(path):
    assert path.is_absolute() and not str(path).startswith('\\\\')
    return '\\\\?\\' + str(path)


def native_info(path):
    return os.stat(native_name(path), follow_symlinks=False)


def native_rename(path, target):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    if not kernel.MoveFileExW(native_name(path), native_name(target), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def native_delete(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.DeleteFileW.argtypes = [ctypes.c_wchar_p]
    if not kernel.DeleteFileW(native_name(path)):
        raise ctypes.WinError(ctypes.get_last_error())


def native_write(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(native_name(path), 0x40000000, 3, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CloseHandle(handle)


def private_read(path):
    with operator()._WindowsAdapter().file(path) as stream:
        return stream.read()


@pytest.fixture
def private(tmp_path):
    path = tmp_path / 'private'
    operator().create_private_directory(path)
    return path


def long_directory(private):
    # Test setup uses an explicit Win32 prefix within this private scratch;
    # production receives the ordinary public Path and must open it safely.
    path = private
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    while len(str(path)) <= 280:
        path = path / ('synthetic-custody-segment-' + 'x' * 32)
        assert path.is_relative_to(private)
        assert kernel.CreateDirectoryW(native_name(path), None), ctypes.get_last_error()
    assert len(str(path)) > 260
    return path


def protected_long_file(private):
    short = private / 'short-private.data'
    operator()._WindowsAdapter().write_new(short, b'synthetic long private data')
    target = long_directory(private) / 'private-long.data'
    assert target.is_relative_to(private)
    native_rename(short, target)
    return target


def test_private_directory_creation_beyond_max_path_keeps_strict_acl_and_public_path(private):
    port = operator()
    # Keep the parent within legacy Win32 bounds so RED reaches the actual
    # CreateDirectoryW failure rather than an earlier long-parent handle.
    parent = private / ('p' * max(1, 230 - len(str(private)) - 1))
    parent.mkdir()
    path = parent / ('new-explicit-private-' + 'x' * 80)
    assert len(str(parent)) <= 248 and len(str(path)) > 260
    before = str(path)
    port.create_private_directory(path)
    assert str(path) == before and not before.startswith('\\\\?\\')
    with port._protected_scope(path, strict=True):
        with pytest.raises(PermissionError): native_rename(path, parent / 'renamed')
    assert stat.S_ISDIR(native_info(path).st_mode)
    with pytest.raises(port.OperatorError): port.create_private_directory(path)


def test_existing_directory_handle_beyond_max_path_denies_real_rename(private):
    port, path = operator(), long_directory(private)
    with port._directory_handle(path, leaf=True):
        with pytest.raises(PermissionError): native_rename(path, path.with_name('moved-long-directory'))
    assert stat.S_ISDIR(native_info(path).st_mode)


def test_protected_scope_accepts_private_long_directory_without_changing_ownership(private):
    port, path = operator(), long_directory(private)
    with port.protected_scope(path):
        assert stat.S_ISDIR(native_info(path).st_mode)
        with pytest.raises(PermissionError): native_rename(path, path.with_name('moved-long-directory'))


def test_private_adapter_write_new_beyond_max_path_is_exclusive_and_readable(private):
    port, directory = operator(), long_directory(private)
    path, payload = directory / 'created-private.data', b'synthetic explicit private payload'
    port._WindowsAdapter().write_new(path, payload)
    assert private_read(path) == payload
    with pytest.raises(port.OperatorError): port._WindowsAdapter().write_new(path, b'overwrite')
    with port._WindowsAdapter().file(path) as stream:
        assert stream.read() == payload


def test_private_adapter_file_beyond_max_path_denies_real_write_and_delete(private):
    port, path = operator(), protected_long_file(private)
    with port._WindowsAdapter().file(path) as stream:
        assert stream.read() == b'synthetic long private data'
        with pytest.raises(PermissionError): native_delete(path)
        with pytest.raises(PermissionError): native_write(path)
    assert private_read(path) == b'synthetic long private data'


def test_reconciliation_long_file_pin_keeps_actual_no_delete_and_no_write(private):
    port = importlib.import_module('secretary.infrastructure.team_restore_reconciliation')
    path = protected_long_file(private)
    before = native_info(path).st_ino
    with port._pin_file(path, deny_write=True):
        with pytest.raises(PermissionError): native_delete(path)
        with pytest.raises(PermissionError): native_write(path)
        assert native_info(path).st_ino == before
    assert private_read(path) == b'synthetic long private data'


def test_reconciliation_long_directory_pin_denies_real_rename(private):
    port = importlib.import_module('secretary.infrastructure.team_restore_reconciliation')
    path = long_directory(private)
    with port._pin_file(path, directory=True):
        with pytest.raises(PermissionError): native_rename(path, path.with_name('moved-long-directory'))
    assert stat.S_ISDIR(native_info(path).st_mode)


@pytest.mark.parametrize('unsafe', (
    r'\\synthetic.invalid\share\file', r'/\synthetic.invalid\share\file',
    r'\\?\C:\synthetic\file', r'\\.\C:\synthetic\file',
    r'C:\synthetic\file:stream', r'C:\synthetic\..\file', 'relative-file',
))
def test_internal_win32_conversion_refuses_unsafe_public_paths_without_io(unsafe, monkeypatch):
    port = operator()
    assert hasattr(port, '_win32_path'), 'Internal safe Win32 conversion is missing'
    def forbidden(*args, **kwargs):
        pytest.fail('Unsafe public path caused filesystem or native access')
    original_stat, original_lstat = Path.stat, Path.lstat
    target = str(Path(unsafe))
    def stat_spy(path, *args, **kwargs):
        if str(path) == target: forbidden()
        return original_stat(path, *args, **kwargs)
    def lstat_spy(path, *args, **kwargs):
        if str(path) == target: forbidden()
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat_spy)
    monkeypatch.setattr(Path, 'stat', stat_spy)
    monkeypatch.setattr(port.ctypes, 'WinDLL', forbidden)
    with pytest.raises(port.OperatorError): port._win32_path(unsafe)


def test_reconciliation_rejects_unsafe_path_before_filesystem_probe(monkeypatch):
    port = importlib.import_module('secretary.infrastructure.team_restore_reconciliation')
    unsafe = Path(r'\\synthetic.invalid\share\file')
    def forbidden(*args, **kwargs):
        pytest.fail('Unsafe reconciliation path caused filesystem or native access')
    original_stat, original_lstat = Path.stat, Path.lstat
    def stat_spy(path, *args, **kwargs):
        if path == unsafe: forbidden()
        return original_stat(path, *args, **kwargs)
    def lstat_spy(path, *args, **kwargs):
        if path == unsafe: forbidden()
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat_spy)
    monkeypatch.setattr(Path, 'stat', stat_spy)
    monkeypatch.setattr(port.ctypes, 'WinDLL', forbidden)
    with pytest.raises(ValueError):
        with port._pin_file(unsafe): pass
