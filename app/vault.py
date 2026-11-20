"""OS-protected model credentials. No credential is returned through the HTTP API."""

import base64
import ctypes
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from .core import STATE


def _dpapi(data, decrypt):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("ptr", ctypes.c_void_p)]

    buf = ctypes.create_string_buffer(data)
    source, dest = Blob(len(data), ctypes.cast(buf, ctypes.c_void_p)), Blob()
    fn = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dest.ptr, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(dest.ptr))


def _cipher():
    path = STATE / "master.key"
    # The owner-only key can be supplied separately from server backups.
    if os.getenv("DANGJIAN_MASTER_KEY"):
        return Fernet(os.environ["DANGJIAN_MASTER_KEY"].encode())
    if not path.exists():
        try:
            with os.fdopen(
                os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
            ) as out:
                out.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(path.read_bytes())


def store_secret(value, label=None):
    directory = STATE / "vault"
    directory.mkdir(parents=True, exist_ok=True)
    name = label or uuid4().hex
    if os.name == "nt":
        wrapped = b"W" + _dpapi(value.encode(), False)
    else:
        wrapped = b"F" + _cipher().encrypt(value.encode())
    temp = directory / (uuid4().hex + ".tmp")
    with os.fdopen(
        os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
    ) as out:
        out.write(wrapped)
    os.replace(temp, directory / name)
    return name


def read_secret(ref):
    if ref == "deepseek" and os.getenv("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    if not ref or Path(ref).name != ref:
        return None
    path = STATE / "vault" / ref
    if not path.is_file():
        return None
    wrapped = path.read_bytes()
    return (
        _dpapi(wrapped[1:], True)
        if wrapped[:1] == b"W"
        else _cipher().decrypt(wrapped[1:])
    ).decode()
