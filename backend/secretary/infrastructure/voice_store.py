"""Current-user DPAPI only; bounded private material, never a plaintext cache."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import stat
import threading
from pathlib import Path
from uuid import uuid4

from secretary.domain.voice import (Excerpt, MAX_SAMPLES, ModelStamp, PreparedAudio,
                                    PrivateMaterial, TECHNICAL_POLICY_REVISION, VoiceError, canonical_uuid, valid_vector)

MAX_ENVELOPE = 1400000
MAX_CIPHERTEXT = MAX_ENVELOPE + 65536
REVIEW_REASONS = ("listening_required", "single_speaker_review_required", "energy_screening_only")


def _current_user_owned(path: Path):
    """Fail closed on owner mismatch; never edit ACLs or trust a shared path."""
    if os.name != "nt":
        if path.stat().st_uid != os.getuid():
            raise VoiceError("private_path_owner_mismatch")
        return
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p,ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    owner, descriptor, token = ctypes.c_void_p(), ctypes.c_void_p(), wintypes.HANDLE()
    try:
        if advapi.GetNamedSecurityInfoW(str(path),1,1,ctypes.byref(owner),None,None,None,ctypes.byref(descriptor)):
            raise VoiceError("private_owner_check_failed")
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):
            raise VoiceError("private_owner_check_failed")
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token,1,None,0,ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise VoiceError("private_owner_check_failed")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token,1,buffer,size,ctypes.byref(size)):
            raise VoiceError("private_owner_check_failed")
        user_sid = ctypes.cast(buffer,ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.EqualSid(owner,user_sid):
            # Elevated Windows tokens commonly create files owned by their
            # TokenOwner (Administrators). Accept that exact default owner, not
            # arbitrary group membership. Encryption remains user-scoped DPAPI.
            size = wintypes.DWORD()
            advapi.GetTokenInformation(token,4,None,0,ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise VoiceError("private_owner_check_failed")
            owner_buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token,4,owner_buffer,size,ctypes.byref(size)):
                raise VoiceError("private_owner_check_failed")
            default_owner = ctypes.cast(owner_buffer,ctypes.POINTER(ctypes.c_void_p))[0]
            if not advapi.EqualSid(owner,default_owner):
                raise VoiceError("private_path_owner_mismatch")
    finally:
        if token:
            kernel.CloseHandle(token)
        if descriptor:
            kernel.LocalFree(descriptor)


class DPAPICipher:
    def _crypt(self, value: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise VoiceError("dpapi_unavailable")
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        source = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        incoming = Blob(len(value), source)
        outgoing = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise VoiceError("dpapi_failed")
        try:
            if outgoing.size > MAX_CIPHERTEXT:
                raise VoiceError("private_material_limit")
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            kernel.LocalFree(outgoing.data)

    def protect(self, value: bytes) -> bytes:
        return self._crypt(value, decrypt=False)

    def unprotect(self, value: bytes) -> bytes:
        return self._crypt(value, decrypt=True)


def _revision(value):
    if type(value) is not int or not 0 <= value <= 2**53:
        raise VoiceError("invalid_material_revision")
    return value


def validate_material(material: PrivateMaterial) -> None:
    canonical_uuid(material.profile_id)
    canonical_uuid(material.enrollment_id)
    _revision(material.material_revision)
    audio = material.audio
    if (not isinstance(audio.pcm16, bytes) or not 0 < len(audio.pcm16) <= MAX_SAMPLES*2
            or len(audio.pcm16) % 2 or audio.review_reasons != REVIEW_REASONS
            or not 1 <= len(audio.excerpts) <= 3
            or len(material.embeddings) not in (0,len(audio.excerpts))):
        raise VoiceError("invalid_private_material")
    previous = 0
    for e in audio.excerpts:
        if (type(e.start_sample) is not int or type(e.end_sample) is not int
                or e.start_sample < previous or e.end_sample > len(audio.pcm16)//2
                or not 80000 <= e.end_sample-e.start_sample <= 160000):
            raise VoiceError("invalid_private_material")
        previous = e.end_sample
    if any(not valid_vector(v) for v in material.embeddings):
        raise VoiceError("invalid_private_material")
    total = sum(e.end_sample-e.start_sample for e in audio.excerpts)
    if type(audio.usable_samples) is not int or not 240000 <= audio.usable_samples <= total:
        raise VoiceError("invalid_private_material")


class VoiceStore:
    def __init__(self, data_dir: Path, *, cipher=None, partial_observer=None):
        self.root = Path(data_dir).absolute() / "voice" / "profiles"
        self.cipher = cipher if cipher is not None else DPAPICipher()
        self._lock = threading.RLock()
        # Only files exclusively created by this instance may be repaired.
        # Never discover/allow arbitrary hardlinks by name or directory scan.
        self._pending_cleanup = {}
        self.partial_observer = partial_observer

    def _safe(self, path: Path, *, directory=False, own_finalize_link=False):
        # Inspect every existing ancestor, including the caller's data directory.
        for item in [*reversed(path.parents), path]:
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise VoiceError("unsafe_private_path")
            if item != path or directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise VoiceError("unsafe_private_path")
            elif not stat.S_ISREG(info.st_mode):
                raise VoiceError("unsafe_private_path")
            elif info.st_nlink != 1 and not (own_finalize_link and info.st_nlink == 2):
                raise VoiceError("unsafe_private_path")
            if item == self.root or self.root in item.parents or item in [self.root.parent,self.root.parent.parent]:
                _current_user_owned(item)
        if path != self.root and self.root not in path.parents:
            raise VoiceError("unsafe_private_path")

    def _path(self, profile_id, enrollment_id, *, create=False):
        path = self.root / canonical_uuid(profile_id) / (canonical_uuid(enrollment_id) + ".dpapi")
        self._retry_cleanup(path)
        self._safe(path)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._safe(path)
        return path

    def _retry_cleanup(self, final_path: Path):
        pending = self._pending_cleanup.get(final_path)
        if pending is None:
            return
        temporary, identity, finalized = pending
        try:
            self._safe(temporary,own_finalize_link=finalized)
            if temporary.exists():
                info = temporary.stat()
                if (info.st_dev,info.st_ino) != identity:
                    raise VoiceError("unsafe_private_path")
                if info.st_nlink == 2:
                    # An exception is valid only for the exact two names that
                    # this write created, with the originally captured identity.
                    self._safe(final_path,own_finalize_link=True)
                    final_info = final_path.stat()
                    if (final_info.st_dev,final_info.st_ino) != identity or final_info.st_nlink != 2:
                        raise VoiceError("unsafe_private_path")
                elif finalized and final_path.exists():
                    final_info = final_path.stat()
                    if (final_info.st_dev,final_info.st_ino) != identity:
                        raise VoiceError("unsafe_private_path")
                temporary.unlink(missing_ok=True)
            del self._pending_cleanup[final_path]
        except OSError:
            raise VoiceError("private_cleanup_pending") from None

    def write(self, material: PrivateMaterial) -> None:
        validate_material(material)
        content = {"version": 1, "technical_policy_revision": TECHNICAL_POLICY_REVISION,
                   "profile_id": material.profile_id, "enrollment_id": material.enrollment_id,
                   "material_revision": material.material_revision,
                   "model": {"model_id": material.model.model_id, "revision": material.model.revision,
                             "weights_sha256": material.model.weights_sha256},
                   "pcm16": base64.b64encode(material.audio.pcm16).decode("ascii"),
                   "excerpts": [[e.start_sample, e.end_sample] for e in material.audio.excerpts],
                   "usable_samples": material.audio.usable_samples,
                   "review_reasons": list(material.audio.review_reasons),
                   "embeddings": material.embeddings}
        plain = json.dumps(content, separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(plain) > MAX_ENVELOPE:
            raise VoiceError("private_material_limit")
        try:
            encrypted = self.cipher.protect(plain)
        except VoiceError:
            raise
        except Exception:
            raise VoiceError("dpapi_failed") from None
        if not isinstance(encrypted, bytes) or not 0 < len(encrypted) <= MAX_CIPHERTEXT or encrypted == plain:
            raise VoiceError("dpapi_failed")
        with self._lock:
            path = self._path(material.profile_id, material.enrollment_id, create=True)
            if path.exists():
                raise VoiceError("material_already_exists")
            temporary = path.parent / ("." + str(uuid4()) + ".cipherpart")
            created_identity = None
            finalized = False
            try:
                with temporary.open("xb") as handle:
                    info = os.fstat(handle.fileno())
                    created_identity = (info.st_dev,info.st_ino)
                    if self.partial_observer is not None:
                        # Durable caller ledger records exclusively created file identity
                        # before payload write/finalize; never an audio/path public DTO.
                        self.partial_observer(material.profile_id, material.enrollment_id, temporary.name, created_identity)
                    handle.write(encrypted)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._safe(path)
                # Hard-link finalize is atomic and cannot silently overwrite an
                # existing immutable enrollment. Both names contain ciphertext.
                os.link(temporary, path)
                finalized = True
            except OSError:
                raise VoiceError("private_store_write_failed") from None
            finally:
                if created_identity is not None:
                    self._pending_cleanup[path] = (temporary,created_identity,finalized)
                    self._retry_cleanup(path)

    def read(self, profile_id: str, enrollment_id: str) -> PrivateMaterial:
        with self._lock:
            path = self._path(profile_id, enrollment_id)
            try:
                with path.open("rb") as handle:
                    if os.fstat(handle.fileno()).st_size > MAX_CIPHERTEXT:
                        raise VoiceError("private_material_limit")
                    encrypted = handle.read(MAX_CIPHERTEXT + 1)
                    self._safe(path)
                    if (os.fstat(handle.fileno()).st_ino, os.fstat(handle.fileno()).st_dev) != (path.stat().st_ino,path.stat().st_dev):
                        raise VoiceError("unsafe_private_path")
            except FileNotFoundError:
                raise VoiceError("private_material_missing") from None
            except OSError:
                raise VoiceError("private_store_read_failed") from None
            if len(encrypted) > MAX_CIPHERTEXT:
                raise VoiceError("private_material_limit")
            try:
                plain = self.cipher.unprotect(encrypted)
                if not isinstance(plain, bytes) or len(plain) > MAX_ENVELOPE:
                    raise VoiceError("private_material_limit")
                data = json.loads(plain)
                if set(data) != {"version", "technical_policy_revision", "profile_id", "enrollment_id", "material_revision", "model", "pcm16",
                                 "excerpts", "usable_samples", "review_reasons", "embeddings"} or type(data["version"]) is not int or data["version"] != 1 or data["technical_policy_revision"] != TECHNICAL_POLICY_REVISION:
                    raise ValueError
                material = PrivateMaterial(data["profile_id"], data["enrollment_id"], data["material_revision"],
                    ModelStamp(**data["model"]), PreparedAudio(base64.b64decode(data["pcm16"], validate=True),
                    tuple(Excerpt(*e) for e in data["excerpts"]), data["usable_samples"], tuple(data["review_reasons"])),
                    tuple(tuple(v) for v in data["embeddings"]))
                validate_material(material)
                if material.profile_id != profile_id or material.enrollment_id != enrollment_id:
                    raise VoiceError("private_material_owner_mismatch")
                return material
            except VoiceError:
                raise
            except Exception:
                raise VoiceError("invalid_private_material") from None

    def delete(self, profile_id: str, enrollment_id: str) -> bool:
        with self._lock:
            path = self._path(profile_id, enrollment_id)
            if not path.exists():
                return False
            # Verify encrypted envelope ownership before a scoped unlink.
            self.read(profile_id, enrollment_id)
            self._safe(path)
            try:
                path.unlink()
            except OSError:
                raise VoiceError("private_cleanup_pending") from None
            return True

    def cleanup_tracked_partial(self, profile_id, generation, basename, identity):
        """Restart cleanup using a durable exclusive-create receipt, never filename discovery.

        Callers must supply a trusted local ledger receipt captured by partial_observer.
        The two-link exception requires both originally owned filenames and identity;
        foreign/replaced/extra hardlinks still fail closed.
        """
        if not isinstance(basename, str) or not basename.startswith('.') or not basename.endswith('.cipherpart'):
            raise VoiceError('unsafe_private_path')
        canonical_uuid(basename[1:-11])
        final = self.root / canonical_uuid(profile_id) / (canonical_uuid(generation)+'.dpapi')
        temporary = final.parent / basename
        with self._lock:
            self._safe(temporary, own_finalize_link=True)
            if not temporary.exists():
                return
            info = temporary.stat()
            if (info.st_dev,info.st_ino) != tuple(identity):
                raise VoiceError('unsafe_private_path')
            if info.st_nlink == 2:
                self._safe(final, own_finalize_link=True)
                try:
                    other = final.stat()
                except OSError:
                    raise VoiceError('unsafe_private_path') from None
                if (other.st_dev,other.st_ino) != tuple(identity) or other.st_nlink != 2:
                    raise VoiceError('unsafe_private_path')
            elif final.exists():
                try:
                    other = final.stat()
                except OSError:
                    raise VoiceError('unsafe_private_path') from None
                if (other.st_dev,other.st_ino) != tuple(identity):
                    raise VoiceError('unsafe_private_path')
            try:
                temporary.unlink()
            except OSError:
                raise VoiceError('private_cleanup_pending') from None
