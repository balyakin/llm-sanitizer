#!/usr/bin/env python3
import argparse
import errno
import json
import logging
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
import pwd
import grp
import fnmatch

try:
    from fuse import FUSE, FuseOSError, Operations, fuse_get_context  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from fuse import FUSE, FuseOSError, Operations  # type: ignore
        fuse_get_context = None  # type: ignore
    except ImportError:
        print("Missing dependency: fusepy. Install with: pip install fusepy", file=sys.stderr)
        sys.exit(1)


UUID_PATTERN = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)
UUID_TOKEN_PATTERN = re.compile(r"\buuid_[0-9a-f]{32}\b", re.IGNORECASE)


def _build_regex(replacements: Dict[str, str]) -> Optional[re.Pattern]:
    if not replacements:
        return None
    parts = sorted(replacements.keys(), key=len, reverse=True)
    escaped = [re.escape(p) for p in parts]
    return re.compile("|".join(escaped))


def _load_uuid_map(path: Optional[str], logger: logging.Logger) -> Dict[str, str]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read UUID map %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("UUID map file is not a dict: %s", path)
        return {}
    uuid_map = data.get("uuid", {})
    if not isinstance(uuid_map, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for key, value in uuid_map.items():
        if isinstance(key, str) and isinstance(value, str):
            cleaned[key] = value
    return cleaned


def _get_mtime_ns(path: Optional[str]) -> Optional[int]:
    if not path:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _save_uuid_map(path: Optional[str], uuid_map: Dict[str, str], logger: logging.Logger) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"uuid": uuid_map}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("Failed to write UUID map %s: %s", path, exc)


class ReplacementEngine:
    def __init__(
        self,
        pairs: Iterable[Tuple[str, str]],
        logger: logging.Logger,
        uuid_map_path: Optional[str] = None,
    ) -> None:
        self._logger = logger
        self.forward: Dict[str, str] = {}
        self.backward: Dict[str, str] = {}
        for original, placeholder in pairs:
            if not original or not placeholder:
                raise ValueError("Replacement entries must be non-empty strings")
            if os.sep in placeholder:
                raise ValueError("Placeholders must not contain path separators")
            if original in self.forward:
                raise ValueError("Duplicate original value in replacements")
            if placeholder in self.backward:
                raise ValueError("Duplicate placeholder value in replacements")
            self.forward[original] = placeholder
            self.backward[placeholder] = original

        self._forward_re = _build_regex(self.forward)
        self._backward_re = _build_regex(self.backward)
        self._uuid_map_path = uuid_map_path
        self._uuid_map = _load_uuid_map(uuid_map_path, logger)
        self._uuid_reverse_map = {v: k for k, v in self._uuid_map.items()}
        self._uuid_map_mtime = _get_mtime_ns(uuid_map_path)
        self._logger.info("Loaded %d replacement pairs", len(self.forward))
        if self._uuid_map:
            self._logger.info("Loaded %d UUID mappings", len(self._uuid_map))

    def _refresh_uuid_map(self) -> None:
        if not self._uuid_map_path:
            return
        current_mtime = _get_mtime_ns(self._uuid_map_path)
        if current_mtime is None or current_mtime == self._uuid_map_mtime:
            return
        uuid_map = _load_uuid_map(self._uuid_map_path, self._logger)
        self._uuid_map = uuid_map
        self._uuid_reverse_map = {v: k for k, v in uuid_map.items()}
        self._uuid_map_mtime = current_mtime

    def contains_sensitive(self, text: str) -> bool:
        if not text:
            return False
        if self._forward_re and self._forward_re.search(text):
            return True
        return bool(UUID_PATTERN.search(text))

    def obfuscate(self, text: str) -> str:
        result = text
        if self._forward_re:
            result = self._forward_re.sub(lambda m: self.forward[m.group(0)], result)

        if self._uuid_map_path:
            self._refresh_uuid_map()
            changed = False

            def repl_uuid(match: re.Match) -> str:
                nonlocal changed
                original = match.group(0)
                token = self._uuid_map.get(original)
                if not token:
                    token = "uuid_" + uuid.uuid4().hex
                    self._uuid_map[original] = token
                    self._uuid_reverse_map[token] = original
                    changed = True
                return token

            result = UUID_PATTERN.sub(repl_uuid, result)
            if changed:
                _save_uuid_map(self._uuid_map_path, self._uuid_map, self._logger)
                self._uuid_map_mtime = _get_mtime_ns(self._uuid_map_path)

        return result

    def deobfuscate(self, text: str) -> str:
        result = text
        if self._backward_re:
            result = self._backward_re.sub(lambda m: self.backward[m.group(0)], result)

        if self._uuid_map_path:
            self._refresh_uuid_map()

        if self._uuid_reverse_map:

            def restore_uuid(match: re.Match) -> str:
                token = match.group(0)
                original = self._uuid_reverse_map.get(token)
                if original is None:
                    original = self._uuid_reverse_map.get(token.lower())
                return original or token

            result = UUID_TOKEN_PATTERN.sub(restore_uuid, result)

        return result


@dataclass
class FileState:
    fh: int
    path: str
    full_path: str
    is_text: bool
    obfuscated: bytearray
    dirty: bool
    flags: int


class ObfuscatingFS(Operations):
    def __init__(
        self,
        root: str,
        replacer: ReplacementEngine,
        text_extensions: Iterable[str],
        logger: logging.Logger,
        excludes: Iterable[str] = (),
        strict_paths: bool = False,
    ) -> None:
        self.root = os.path.realpath(root)
        self.replacer = replacer
        self.text_exts = set()
        self.text_names = set()
        for item in text_extensions:
            item = item.strip().lower()
            if not item:
                continue
            if item.startswith("."):
                self.text_exts.add(item)
            else:
                self.text_names.add(item)
        self.excludes = [p.strip() for p in excludes if p.strip()]
        self.logger = logger
        self.open_files: Dict[int, FileState] = {}
        self.cache: Dict[str, Dict[str, object]] = {}
        self._lock = threading.Lock()
        self._xattr_supported = all(
            hasattr(os, name)
            for name in ("getxattr", "listxattr", "setxattr", "removexattr")
        )
        self.strict_paths = strict_paths
        self._blocked_log: Dict[str, Tuple[float, int]] = {}

    def _caller_ids(self) -> Tuple[int, int]:
        if fuse_get_context is None:
            return os.getuid(), os.getgid()
        try:
            uid, gid, _ = fuse_get_context()
        except Exception:
            return os.getuid(), os.getgid()
        return uid, gid

    def _log_blocked_component(self, component: str) -> None:
        now = time.time()
        entry = self._blocked_log.get(component)
        if entry is None:
            self._blocked_log[component] = (now, 0)
            self.logger.info("Blocked non-obfuscated path component: %s", component)
            return
        last_ts, count = entry
        count += 1
        if now - last_ts >= 5.0:
            self.logger.info(
                "Blocked non-obfuscated path component: %s (x%d in last %.1fs)",
                component,
                count,
                now - last_ts,
            )
            self._blocked_log[component] = (now, 0)
        else:
            self._blocked_log[component] = (last_ts, count)

    def _maybe_chown_path(self, real_path: str, follow_symlinks: bool = True) -> None:
        if os.geteuid() != 0:
            return
        uid, gid = self._caller_ids()
        try:
            if follow_symlinks:
                os.chown(real_path, uid, gid)
            else:
                os.lchown(real_path, uid, gid)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP):
                self.logger.debug("Failed to chown %s to %d:%d: %s", real_path, uid, gid, exc)

    def _maybe_chown_fd(self, fd: int) -> None:
        if os.geteuid() != 0:
            return
        uid, gid = self._caller_ids()
        try:
            os.fchown(fd, uid, gid)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP):
                self.logger.debug("Failed to fchown fd=%d to %d:%d: %s", fd, uid, gid, exc)

    def _real_path(self, path: str) -> str:
        if path == "/":
            return self.root
        if path.startswith("/"):
            path = path[1:]
        parts = [p for p in path.split("/") if p]
        if self.strict_paths:
            for part in parts:
                if part in (".", ".."):
                    continue
                if self.replacer.contains_sensitive(part):
                    self._log_blocked_component(part)
                    raise FuseOSError(errno.ENOENT)
        decoded = [self.replacer.deobfuscate(p) for p in parts]
        return os.path.join(self.root, *decoded)

    def _is_excluded(self, real_path: str) -> bool:
        rel = os.path.relpath(real_path, self.root)
        if rel == ".":
            return False
        parts = rel.split(os.sep)
        for part in parts:
            for pat in self.excludes:
                if fnmatch.fnmatch(part, pat):
                    return True
        return False

    def _is_text_path(self, real_path: str) -> bool:
        if self._is_excluded(real_path):
            return False
        base = os.path.basename(real_path).lower()
        if base in self.text_names:
            return True
        _, ext = os.path.splitext(real_path)
        return ext.lower() in self.text_exts

    def _read_real(self, full_path: str) -> bytes:
        with open(full_path, "rb") as f:
            return f.read()

    def _obfuscate_bytes(self, data: bytes, path: str) -> Tuple[bytes, bool]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.logger.debug("Skipping non-utf8 content: %s", path)
            return data, False
        obfuscated_text = self.replacer.obfuscate(text)
        return obfuscated_text.encode("utf-8"), True

    def _get_obfuscated_for_path(self, real_path: str, path: str) -> Tuple[bytes, bool]:
        st = os.stat(real_path)
        cached = self.cache.get(real_path)
        if cached and cached["mtime_ns"] == st.st_mtime_ns and cached["size"] == st.st_size:
            return cached["data"], True  # type: ignore[return-value]

        data = self._read_real(real_path)
        obfuscated, is_text = self._obfuscate_bytes(data, path)
        if is_text:
            self.cache[real_path] = {
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
                "data": obfuscated,
            }
        return obfuscated, is_text

    def _flush_text_state(self, state: FileState) -> None:
        try:
            text = state.obfuscated.decode("utf-8")
        except UnicodeDecodeError:
            self.logger.warning("Non-utf8 content in text file, writing raw bytes: %s", state.path)
            data = bytes(state.obfuscated)
        else:
            data = self.replacer.deobfuscate(text).encode("utf-8")

        os.lseek(state.fh, 0, os.SEEK_SET)
        os.ftruncate(state.fh, 0)
        os.write(state.fh, data)
        os.fsync(state.fh)

        st = os.fstat(state.fh)
        self.cache[state.full_path] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "data": bytes(state.obfuscated),
        }
        self.logger.info(
            "Wrote text file: %s (obfuscated size=%d, real size=%d)",
            state.path,
            len(state.obfuscated),
            len(data),
        )

    def _deobfuscate_file_in_place(self, real_path: str, display_path: str) -> None:
        try:
            data = self._read_real(real_path)
        except OSError:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return

        restored = self.replacer.deobfuscate(text)
        if restored == text:
            return

        try:
            with open(real_path, "wb") as f:
                f.write(restored.encode("utf-8"))
        except OSError:
            return

        self.cache.pop(real_path, None)
        self.logger.info("Deobfuscated renamed text file: %s", display_path)

    def access(self, path: str, mode: int) -> int:
        real_path = self._real_path(path)
        if not os.access(real_path, mode):
            raise FuseOSError(errno.EACCES)
        return 0

    def chmod(self, path: str, mode: int) -> int:
        real_path = self._real_path(path)
        return os.chmod(real_path, mode)

    def chown(self, path: str, uid: int, gid: int) -> int:
        real_path = self._real_path(path)
        return os.chown(real_path, uid, gid)

    def getattr(self, path: str, fh: Optional[int] = None) -> Dict[str, object]:
        real_path = self._real_path(path)
        try:
            st = os.lstat(real_path)
        except FileNotFoundError:
            raise FuseOSError(errno.ENOENT)

        attrs = {
            key: getattr(st, key)
            for key in (
                "st_atime",
                "st_ctime",
                "st_gid",
                "st_mode",
                "st_mtime",
                "st_nlink",
                "st_size",
                "st_uid",
            )
        }

        if self._is_text_path(real_path) and stat.S_ISREG(st.st_mode):
            try:
                data, is_text = self._get_obfuscated_for_path(real_path, path)
            except FileNotFoundError:
                raise FuseOSError(errno.ENOENT)
            if is_text:
                attrs["st_size"] = len(data)
        return attrs

    def readdir(self, path: str, fh: int) -> Iterable[str]:
        real_path = self._real_path(path)
        entries = [".", ".."]
        entries.extend(os.listdir(real_path))
        seen: set = set()
        
        # Check if the current directory itself is excluded (or inside an excluded one)
        # If so, we don't obfuscate any children names
        parent_excluded = self._is_excluded(real_path)

        for entry in entries:
            if entry in (".", ".."):
                yield entry
                continue
            
            # If parent is excluded, or this specific entry matches exclusion pattern
            is_excluded = parent_excluded
            if not is_excluded:
                for pat in self.excludes:
                    if fnmatch.fnmatch(entry, pat):
                        is_excluded = True
                        break

            if is_excluded:
                obfuscated = entry
            else:
                obfuscated = self.replacer.obfuscate(entry)

            if obfuscated in seen:
                # Only warn if it's not an excluded file (collisions in raw files are normal/impossible)
                if not is_excluded:
                    self.logger.warning(
                        "Obfuscated name collision in %s: %s -> %s",
                        path,
                        entry,
                        obfuscated,
                    )
            seen.add(obfuscated)
            yield obfuscated

    def readlink(self, path: str) -> str:
        real_path = self._real_path(path)
        target = os.readlink(real_path)
        return self.replacer.obfuscate(target)

    def mknod(self, path: str, mode: int, dev: int) -> int:
        real_path = self._real_path(path)
        os.mknod(real_path, mode, dev)
        self._maybe_chown_path(real_path)
        return 0

    def mkdir(self, path: str, mode: int) -> int:
        real_path = self._real_path(path)
        os.mkdir(real_path, mode)
        self._maybe_chown_path(real_path)
        return 0

    def rmdir(self, path: str) -> int:
        return os.rmdir(self._real_path(path))

    def statfs(self, path: str) -> Dict[str, int]:
        real_path = self._real_path(path)
        stv = os.statvfs(real_path)
        return {key: getattr(stv, key) for key in ("f_bavail", "f_bfree", "f_blocks", "f_bsize", "f_favail", "f_ffree", "f_files", "f_flag", "f_frsize", "f_namemax")}

    def unlink(self, path: str) -> int:
        return os.unlink(self._real_path(path))

    def symlink(self, target: str, name: str) -> int:
        if self.strict_paths and self.replacer.contains_sensitive(target):
            self._log_blocked_component(target)
            raise FuseOSError(errno.ENOENT)
        real_target = self.replacer.deobfuscate(target)
        real_name = self._real_path(name)
        os.symlink(real_target, real_name)
        self._maybe_chown_path(real_name, follow_symlinks=False)
        return 0

    def rename(self, old: str, new: str) -> int:
        real_old = self._real_path(old)
        real_new = self._real_path(new)
        os.rename(real_old, real_new)
        if self._is_text_path(real_new):
            try:
                st = os.stat(real_new)
            except FileNotFoundError:
                return 0
            if stat.S_ISREG(st.st_mode):
                self._deobfuscate_file_in_place(real_new, new)
        return 0

    def link(self, target: str, name: str) -> int:
        return os.link(self._real_path(target), self._real_path(name))

    def utimens(self, path: str, times: Optional[Tuple[float, float]] = None) -> int:
        return os.utime(self._real_path(path), times)

    def open(self, path: str, flags: int) -> int:
        real_path = self._real_path(path)
        fh = os.open(real_path, flags)
        is_text = self._is_text_path(real_path)

        if is_text:
            if flags & os.O_TRUNC:
                data = b""
            else:
                data, is_text = self._get_obfuscated_for_path(real_path, path)

            if is_text:
                state = FileState(
                    fh=fh,
                    path=path,
                    full_path=real_path,
                    is_text=True,
                    obfuscated=bytearray(data),
                    dirty=bool(flags & os.O_TRUNC),
                    flags=flags,
                )
                with self._lock:
                    self.open_files[fh] = state
                self.logger.debug("Open text file: %s", path)
                return fh

        state = FileState(
            fh=fh,
            path=path,
            full_path=real_path,
            is_text=False,
            obfuscated=bytearray(),
            dirty=False,
            flags=flags,
        )
        with self._lock:
            self.open_files[fh] = state
        return fh

    def create(self, path: str, mode: int, fi: Optional[object] = None) -> int:
        real_path = self._real_path(path)
        fh = os.open(real_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        self._maybe_chown_fd(fh)
        is_text = self._is_text_path(real_path)
        state = FileState(
            fh=fh,
            path=path,
            full_path=real_path,
            is_text=is_text,
            obfuscated=bytearray(),
            dirty=True,
            flags=os.O_WRONLY,
        )
        with self._lock:
            self.open_files[fh] = state
        if is_text:
            self.logger.info("Create text file: %s", path)
        return fh

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        state = self.open_files.get(fh)
        if state and state.is_text:
            data = state.obfuscated
            if offset >= len(data):
                return b""
            return bytes(data[offset : offset + size])

        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        state = self.open_files.get(fh)
        if state and state.is_text:
            if state.flags & os.O_APPEND:
                offset = len(state.obfuscated)
            end = offset + len(data)
            if end > len(state.obfuscated):
                state.obfuscated.extend(b"\x00" * (end - len(state.obfuscated)))
            state.obfuscated[offset:end] = data
            state.dirty = True
            return len(data)

        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, data)

    def truncate(self, path: str, length: int, fh: Optional[int] = None) -> int:
        if fh is not None:
            state = self.open_files.get(fh)
            if state and state.is_text:
                if length < len(state.obfuscated):
                    del state.obfuscated[length:]
                else:
                    state.obfuscated.extend(b"\x00" * (length - len(state.obfuscated)))
                state.dirty = True
                return 0

        real_path = self._real_path(path)
        if self._is_text_path(real_path):
            data, is_text = self._get_obfuscated_for_path(real_path, path)
            if is_text:
                buf = bytearray(data)
                if length < len(buf):
                    del buf[length:]
                else:
                    buf.extend(b"\x00" * (length - len(buf)))
                try:
                    text = buf.decode("utf-8")
                    data_write = self.replacer.deobfuscate(text).encode("utf-8")
                except UnicodeDecodeError:
                    data_write = bytes(buf)
                with open(real_path, "r+b") as f:
                    f.truncate(0)
                    f.write(data_write)
                st = os.stat(real_path)
                self.cache[real_path] = {
                    "mtime_ns": st.st_mtime_ns,
                    "size": st.st_size,
                    "data": bytes(buf),
                }
                return 0

        return os.truncate(real_path, length)

    def flush(self, path: str, fh: int) -> int:
        state = self.open_files.get(fh)
        if state and state.is_text and state.dirty:
            self._flush_text_state(state)
            state.dirty = False
        return os.fsync(fh)

    def fsync(self, path: str, fdatasync: int, fh: int) -> int:
        return self.flush(path, fh)

    def release(self, path: str, fh: int) -> int:
        state = self.open_files.pop(fh, None)
        try:
            if state and state.is_text and state.dirty:
                self._flush_text_state(state)
        finally:
            os.close(fh)
        return 0

    def getxattr(self, path: str, name: str, position: int = 0) -> bytes:
        if not self._xattr_supported:
            return b""
        try:
            return os.getxattr(self._real_path(path), name)
        except OSError:
            return b""

    def listxattr(self, path: str) -> List[str]:
        if not self._xattr_supported:
            return []
        try:
            return os.listxattr(self._real_path(path))
        except OSError:
            return []

    def setxattr(self, path: str, name: str, value: bytes, options: int, position: int = 0) -> int:
        if not self._xattr_supported:
            return 0
        return os.setxattr(self._real_path(path), name, value, options)

    def removexattr(self, path: str, name: str) -> int:
        if not self._xattr_supported:
            return 0
        return os.removexattr(self._real_path(path), name)


def _load_replacements(path: str, logger: logging.Logger) -> List[Tuple[str, str]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    replacements = data.get("replacements")
    if not isinstance(replacements, list):
        raise ValueError("Dictionary JSON must contain a 'replacements' list")

    pairs = []
    for entry in replacements:
        if not isinstance(entry, dict):
            raise ValueError("Each replacement entry must be an object")
        original = entry.get("original")
        placeholder = entry.get("placeholder")
        if not isinstance(original, str) or not isinstance(placeholder, str):
            raise ValueError("Replacement 'original' and 'placeholder' must be strings")
        pairs.append((original, placeholder))
    logger.info("Loaded dictionary file: %s (entries=%d)", path, len(pairs))
    return pairs


def _ensure_mountpoint(
    path: str,
    logger: logging.Logger,
    run_as_uid: Optional[int] = None,
    run_as_gid: Optional[int] = None,
) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
        logger.info("Created mount directory: %s", path)
    elif not os.path.isdir(path):
        raise ValueError(f"Mount point is not a directory: {path}")
    if os.geteuid() == 0 and run_as_uid is not None:
        gid = run_as_gid if run_as_gid is not None else run_as_uid
        os.chown(path, run_as_uid, gid)
        os.chmod(path, 0o755)


def _resolve_run_as_ids(args: argparse.Namespace) -> Tuple[Optional[int], Optional[int]]:
    run_as_uid = args.run_as_uid
    run_as_gid = args.run_as_gid
    if run_as_uid is None:
        env_uid = os.environ.get("SUDO_UID")
        if env_uid:
            run_as_uid = int(env_uid)
    if run_as_gid is None:
        env_gid = os.environ.get("SUDO_GID")
        if env_gid:
            run_as_gid = int(env_gid)
    if run_as_uid is not None and run_as_gid is None:
        run_as_gid = pwd.getpwuid(run_as_uid).pw_gid
    return run_as_uid, run_as_gid


def _drop_privileges(uid: int, gid: int, logger: logging.Logger) -> None:
    username = pwd.getpwuid(uid).pw_name
    groups = [g.gr_gid for g in grp.getgrall() if username in g.gr_mem]
    if gid not in groups:
        groups.append(gid)
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)
    logger.info("Dropped privileges to uid=%d gid=%d", uid, gid)


def _make_preexec_drop_privileges(uid: int, gid: int) -> object:
    username = pwd.getpwuid(uid).pw_name
    groups = [g.gr_gid for g in grp.getgrall() if username in g.gr_mem]
    if gid not in groups:
        groups.append(gid)

    def _preexec() -> None:
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)

    return _preexec


def _wait_for_mount(mount_dir: str, timeout: float, logger: logging.Logger) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.ismount(mount_dir):
            return True
        time.sleep(0.1)
    logger.error("Mount did not become active within %.1f seconds", timeout)
    return False


def _run_unmount(cmd: List[str], logger: logging.Logger, timeout: float) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("Unmount command timed out: %s", " ".join(cmd))
        return False
    except FileNotFoundError:
        logger.warning("Unmount command not found: %s", " ".join(cmd))
        return False

    if result.returncode == 0:
        logger.info("Unmounted via %s", " ".join(cmd))
        return True

    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    detail = stderr or stdout or f"exit={result.returncode}"
    logger.warning("Unmount command failed: %s (%s)", " ".join(cmd), detail)
    return False


def _unmount(mount_dir: str, logger: logging.Logger) -> None:
    umount_cmds = [
        ["fusermount", "-u", mount_dir],        # Linux standard
        ["fusermount", "-u", "-z", mount_dir],  # Linux lazy
        ["/sbin/umount", mount_dir],
        ["/sbin/umount", "-f", mount_dir],
        ["diskutil", "unmount", mount_dir],
        ["diskutil", "unmount", "force", mount_dir],
    ]
    timeout = 5.0
    for cmd in umount_cmds:
        if _run_unmount(cmd, logger, timeout):
            return
        if not os.path.ismount(mount_dir):
            logger.info("Unmounted %s", mount_dir)
            return
    logger.error("Failed to unmount %s", mount_dir)


def _setup_logging(log_file: str, log_level: str) -> logging.Logger:
    logger = logging.getLogger("obfuscate")
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mount obfuscating FUSE FS and run a command.")
    parser.add_argument("--source-dir", required=True, help="Real project directory")
    parser.add_argument("--mount-dir", required=True, help="Mount point for obfuscated view")
    parser.add_argument("--dictionary", required=True, help="JSON dictionary with replacements")
    parser.add_argument("--command", required=True, help="Command to run inside obfuscated FS")
    parser.add_argument(
        "--text-extensions",
        default=".py,.json,.md,.txt,.toml,postinst",
        help="Comma-separated list of text extensions to obfuscate",
    )
    parser.add_argument(
        "--exclude",
        default=".git,.venv,__pycache__,.idea,.DS_Store",
        help="Comma-separated list of names/patterns to exclude from obfuscation",
    )
    parser.add_argument("--log-file", default="/tmp/obfuscation.log", help="Log file path")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument("--allow-other", action="store_true", help="Allow other users to access the mount")
    parser.add_argument("--mount-timeout", type=float, default=5.0, help="Seconds to wait for mount")
    parser.add_argument("--run-as-uid", type=int, default=None, help="Drop privileges to this UID after mount")
    parser.add_argument("--run-as-gid", type=int, default=None, help="Drop privileges to this GID after mount")
    parser.add_argument(
        "--strict-paths",
        action="store_true",
        help="Block access when a path contains non-obfuscated sensitive tokens",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logger = _setup_logging(args.log_file, args.log_level)

    source_dir = os.path.abspath(args.source_dir)
    mount_dir = os.path.abspath(args.mount_dir)
    dict_path = os.path.abspath(args.dictionary)

    if not os.path.isdir(source_dir):
        logger.error("Source directory not found: %s", source_dir)
        return 1

    run_as_uid, run_as_gid = _resolve_run_as_ids(args)
    _ensure_mountpoint(mount_dir, logger, run_as_uid, run_as_gid)

    try:
        pairs = _load_replacements(dict_path, logger)
    except (OSError, ValueError) as exc:
        logger.error("Failed to load dictionary: %s", exc)
        return 1

    uuid_map_path = os.path.join(os.path.dirname(dict_path), ".obfuscation_map.json")
    replacer = ReplacementEngine(pairs, logger, uuid_map_path=uuid_map_path)
    text_exts = [ext.strip() for ext in args.text_extensions.split(",") if ext.strip()]
    excludes = [pat.strip() for pat in args.exclude.split(",") if pat.strip()]
    fs = ObfuscatingFS(
        source_dir,
        replacer,
        text_exts,
        logger,
        excludes=excludes,
        strict_paths=args.strict_paths,
    )

    logger.info("Mounting obfuscating FS from %s to %s", source_dir, mount_dir)
    allow_other = args.allow_other or (os.geteuid() == 0 and run_as_uid is not None)
    if allow_other and not args.allow_other:
        logger.info("Enabling allow_other because mount runs as root and command runs as user")
    fuse_thread = threading.Thread(
        target=FUSE,
        args=(fs, mount_dir),
        kwargs={"foreground": True, "nothreads": True, "allow_other": allow_other},
        daemon=True,
    )
    try:
        fuse_thread.start()
    except Exception as exc:
        logger.error("Failed to start FUSE thread: %s", exc)
        return 1

    if not fuse_thread.is_alive():
        logger.error("FUSE thread exited before mount became active")
        return 1

    if not _wait_for_mount(mount_dir, args.mount_timeout, logger):
        _unmount(mount_dir, logger)
        return 1

    command = shlex.split(args.command)
    env = os.environ.copy()
    env["OBFUSCATE_DICT"] = dict_path
    env["OBFUSCATE_UUID_MAP"] = uuid_map_path
    logger.info("Launching command in obfuscated FS: %s", " ".join(command))
    preexec_fn = None
    if os.geteuid() == 0 and run_as_uid is not None and run_as_gid is not None:
        logger.info("Launching command as uid=%d gid=%d", run_as_uid, run_as_gid)
        preexec_fn = _make_preexec_drop_privileges(run_as_uid, run_as_gid)
    process = subprocess.Popen(command, cwd=mount_dir, env=env, preexec_fn=preexec_fn)

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: Optional[object]) -> None:
        logger.warning("Received signal %s, terminating child process", signum)
        stop_event.set()
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while process.poll() is None:
        if stop_event.is_set():
            break
        time.sleep(0.2)

    if process.poll() is None:
        process.wait()

    exit_code = process.returncode or 0
    logger.info("Command exited with code %d", exit_code)

    _unmount(mount_dir, logger)
    fuse_thread.join(timeout=2.0)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
