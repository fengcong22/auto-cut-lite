from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Mapping, Sequence

if os.name == "nt":
    import msvcrt


_ERROR_ALREADY_EXISTS = 183
_ERROR_INSUFFICIENT_BUFFER = 122
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_TOKEN_QUERY = 0x0008
_TOKEN_IS_APPCONTAINER = 29
_TOKEN_CAPABILITIES = 30
_TOKEN_APPCONTAINER_SID = 31
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x00000002
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_NETWORK_ATTEMPT_COUNT = 12


class OfflineIsolationUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Windows AppContainer isolation is unavailable")


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _LoopbackListener:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(_NETWORK_ATTEMPT_COUNT + 2)
        self._socket.settimeout(0.05)
        self.port = int(self._socket.getsockname()[1])
        self.connection_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _LoopbackListener:
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.connection_count += 1
            try:
                connection.sendall(
                    b"HTTP/1.1 204 No Content\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                connection.close()

    def wait_for_connection(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connection_count:
                return True
            time.sleep(0.01)
        return bool(self.connection_count)

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=1)


class _WindowsApi:
    def __init__(self) -> None:
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
            self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            self.ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise OfflineIsolationUnavailable("windows_appcontainer_api_unavailable") from exc
        self._declare_signatures()

    def _declare_signatures(self) -> None:
        self.userenv.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(_SID_AND_ATTRIBUTES),
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.userenv.CreateAppContainerProfile.restype = ctypes.c_long
        self.userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
        self.userenv.DeleteAppContainerProfile.restype = ctypes.c_long
        self.userenv.GetAppContainerFolderPath.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.userenv.GetAppContainerFolderPath.restype = ctypes.c_long
        self.advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.advapi32.EqualSid.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole32.CoTaskMemFree.restype = None
        self.kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        self.kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        self.kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        self.kernel32.DeleteProcThreadAttributeList.restype = None
        self.kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        self.kernel32.CreateProcessW.restype = wintypes.BOOL
        self.kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel32.ResumeThread.restype = wintypes.DWORD
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel32.TerminateProcess.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESSENTRY32W),
        ]
        self.kernel32.Process32FirstW.restype = wintypes.BOOL
        self.kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESSENTRY32W),
        ]
        self.kernel32.Process32NextW.restype = wintypes.BOOL
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE

    @staticmethod
    def _failed_hresult(value: int) -> bool:
        return ctypes.c_long(value).value < 0

    def create_profile(self, name: str) -> tuple[ctypes.c_void_p, Path]:
        sid = ctypes.c_void_p()
        result = int(
            self.userenv.CreateAppContainerProfile(
                name,
                "Auto-Cut offline release acceptance",
                "Temporary zero-capability offline acceptance profile",
                None,
                0,
                ctypes.byref(sid),
            )
        )
        if self._failed_hresult(result) or not sid.value:
            raise OfflineIsolationUnavailable("appcontainer_profile_create_failed")
        try:
            sid_text_pointer = wintypes.LPWSTR()
            if not self.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text_pointer)):
                raise OfflineIsolationUnavailable("appcontainer_sid_conversion_failed")
            try:
                sid_text = sid_text_pointer.value
                if not sid_text:
                    raise OfflineIsolationUnavailable("appcontainer_sid_conversion_failed")
                folder_pointer = wintypes.LPWSTR()
                folder_result = int(
                    self.userenv.GetAppContainerFolderPath(sid_text, ctypes.byref(folder_pointer))
                )
                if self._failed_hresult(folder_result) or not folder_pointer.value:
                    raise OfflineIsolationUnavailable("appcontainer_storage_unavailable")
                try:
                    storage_root = Path(folder_pointer.value)
                finally:
                    self.ole32.CoTaskMemFree(folder_pointer)
            finally:
                self.kernel32.LocalFree(sid_text_pointer)
        except Exception:
            self.kernel32.LocalFree(sid)
            self.userenv.DeleteAppContainerProfile(name)
            raise
        return sid, storage_root

    def delete_profile(self, name: str, sid: ctypes.c_void_p) -> None:
        self.kernel32.LocalFree(sid)
        result = int(self.userenv.DeleteAppContainerProfile(name))
        if self._failed_hresult(result):
            raise OfflineIsolationUnavailable("appcontainer_profile_cleanup_failed")

    def token_probe(self, process_handle: wintypes.HANDLE) -> dict[str, object]:
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(process_handle, _TOKEN_QUERY, ctypes.byref(token)):
            raise OfflineIsolationUnavailable("appcontainer_token_query_failed")
        try:
            is_appcontainer = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not self.advapi32.GetTokenInformation(
                token,
                _TOKEN_IS_APPCONTAINER,
                ctypes.byref(is_appcontainer),
                ctypes.sizeof(is_appcontainer),
                ctypes.byref(returned),
            ):
                raise OfflineIsolationUnavailable("appcontainer_token_query_failed")

            size = wintypes.DWORD()
            ctypes.set_last_error(0)
            self.advapi32.GetTokenInformation(
                token, _TOKEN_CAPABILITIES, None, 0, ctypes.byref(size)
            )
            if not size.value or ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
                raise OfflineIsolationUnavailable("appcontainer_capability_query_failed")
            capability_buffer = ctypes.create_string_buffer(size.value)
            if not self.advapi32.GetTokenInformation(
                token,
                _TOKEN_CAPABILITIES,
                capability_buffer,
                size.value,
                ctypes.byref(size),
            ):
                raise OfflineIsolationUnavailable("appcontainer_capability_query_failed")
            capability_count = ctypes.cast(
                capability_buffer, ctypes.POINTER(wintypes.DWORD)
            ).contents.value

            sid_size = wintypes.DWORD()
            ctypes.set_last_error(0)
            self.advapi32.GetTokenInformation(
                token, _TOKEN_APPCONTAINER_SID, None, 0, ctypes.byref(sid_size)
            )
            if not sid_size.value or ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
                raise OfflineIsolationUnavailable("appcontainer_token_sid_query_failed")
            sid_buffer = ctypes.create_string_buffer(sid_size.value)
            if not self.advapi32.GetTokenInformation(
                token,
                _TOKEN_APPCONTAINER_SID,
                sid_buffer,
                sid_size.value,
                ctypes.byref(sid_size),
            ):
                raise OfflineIsolationUnavailable("appcontainer_token_sid_query_failed")
            token_sid = ctypes.cast(sid_buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            return {
                "is_appcontainer": bool(is_appcontainer.value),
                "capability_count": int(capability_count),
                "token_sid": token_sid,
            }
        finally:
            self.kernel32.CloseHandle(token)

    def descendant_pids(self, root_pid: int) -> list[int]:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            raise OfflineIsolationUnavailable("appcontainer_child_probe_failed")
        try:
            parents: dict[int, int] = {}
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            present = bool(self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while present:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                present = bool(self.kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
        finally:
            self.kernel32.CloseHandle(snapshot)
        descendants: list[int] = []
        changed = True
        known = {root_pid}
        while changed:
            changed = False
            for pid, parent in parents.items():
                if pid in known or parent not in known:
                    continue
                known.add(pid)
                descendants.append(pid)
                changed = True
        return descendants

    def process_token_probe(self, pid: int) -> dict[str, object] | None:
        handle = self.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE, False, pid
        )
        if not handle:
            return None
        try:
            return self.token_probe(handle)
        except OfflineIsolationUnavailable:
            return None
        finally:
            self.kernel32.CloseHandle(handle)


class WindowsAppContainerIsolation:
    def __init__(self) -> None:
        self._api = _WindowsApi()
        self._profile_name = f"autocut.acceptance.{uuid.uuid4().hex}"
        self._sid: ctypes.c_void_p | None = None
        self._storage_root: Path | None = None
        self.workspace_root: Path | None = None
        self.evidence: dict[str, object] | None = None

    def __enter__(self) -> WindowsAppContainerIsolation:
        try:
            sid, storage_root = self._api.create_profile(self._profile_name)
            self._sid = sid
            self._storage_root = storage_root
            self.workspace_root = storage_root / "AutoCutAcceptance"
            self.workspace_root.mkdir(parents=True, exist_ok=False)
            self.evidence = self._probe_isolation()
            return self
        except OfflineIsolationUnavailable:
            self._cleanup_after_failed_enter()
            raise
        except Exception as exc:
            self._cleanup_after_failed_enter()
            raise OfflineIsolationUnavailable("appcontainer_probe_failed") from exc

    def _cleanup_after_failed_enter(self) -> None:
        if self.workspace_root is not None:
            shutil.rmtree(self.workspace_root, ignore_errors=True)
        if self._sid is not None:
            try:
                self._api.delete_profile(self._profile_name, self._sid)
            except OfflineIsolationUnavailable:
                pass
        self._sid = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        cleanup_error: OfflineIsolationUnavailable | None = None
        if self.workspace_root is not None:
            shutil.rmtree(self.workspace_root, ignore_errors=True)
        if self._sid is not None:
            try:
                self._api.delete_profile(self._profile_name, self._sid)
            except OfflineIsolationUnavailable as error:
                cleanup_error = error
        self._sid = None
        self._storage_root = None
        self.workspace_root = None
        if cleanup_error is not None and exc is None:
            raise cleanup_error

    def _require_active(self) -> tuple[ctypes.c_void_p, Path]:
        if self._sid is None or self.workspace_root is None or self.evidence is None:
            raise RuntimeError("physical offline isolation is not active")
        return self._sid, self.workspace_root

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _verified_token_probe(self, process_handle: wintypes.HANDLE) -> dict[str, object]:
        sid, _ = self._require_profile_started()
        measured = self._api.token_probe(process_handle)
        token_sid = measured.pop("token_sid", None)
        if (
            measured != {"is_appcontainer": True, "capability_count": 0}
            or not token_sid
            or not self._api.advapi32.EqualSid(ctypes.c_void_p(token_sid), sid)
        ):
            raise OfflineIsolationUnavailable("appcontainer_token_identity_failed")
        return measured

    def _require_profile_started(self) -> tuple[ctypes.c_void_p, Path]:
        if self._sid is None or self.workspace_root is None:
            raise RuntimeError("physical offline isolation profile is not active")
        return self._sid, self.workspace_root

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self._require_active()
        completed, _, _ = self._launch(command, cwd=cwd, timeout=timeout, env=env)
        return completed

    def prepare_directory(self, directory: Path) -> Path:
        """Create a workspace directory from inside the container.

        Windows AppContainer storage denies writes to host-created descendants;
        creating each command workspace through the container preserves the
        storage broker's ACL while keeping the host-side extraction readable.
        """
        _, workspace_root = self._require_profile_started()
        target = Path(directory).resolve()
        if not self._is_within(target, workspace_root.resolve()):
            raise RuntimeError("physical offline isolation directory is inaccessible")
        if target.exists():
            if not target.is_dir() or target.is_symlink():
                raise RuntimeError("physical offline isolation directory is invalid")
            return target
        relative = target.relative_to(workspace_root.resolve()).as_posix()
        if any(character.isspace() for character in relative) or '"' in relative:
            raise RuntimeError("physical offline isolation directory name is invalid")
        system_root = os.environ.get("SystemRoot")
        if not isinstance(system_root, str) or not system_root:
            raise OfflineIsolationUnavailable("windows_system_root_unavailable")
        command = [
            str(Path(system_root) / "System32" / "cmd.exe"),
            "/d",
            "/c",
            f"mkdir {relative.replace('/', os.sep)}",
        ]
        completed, _, _ = self._launch(
            command,
            cwd=workspace_root,
            timeout=30,
            env=os.environ,
        )
        if completed.returncode != 0 or not target.is_dir():
            raise RuntimeError("physical offline isolation directory creation failed")
        return target

    def _launch(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: int,
        env: Mapping[str, str],
        observer: Callable[[int], dict[str, object]] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], dict[str, object] | None]:
        sid, workspace_root = self._require_profile_started()
        arguments = [str(part) for part in command]
        if not arguments or timeout <= 0:
            raise ValueError("offline command is invalid")
        executable = Path(arguments[0])
        if not executable.is_absolute() or not executable.is_file():
            raise RuntimeError("physical offline isolation executable is unavailable")
        resolved_cwd = Path(cwd).resolve()
        if not self._is_within(resolved_cwd, workspace_root.resolve()):
            raise RuntimeError("physical offline isolation working directory is inaccessible")

        environment = {str(key): str(value) for key, value in env.items()}
        isolated_temp = workspace_root / "process-temp"
        isolated_temp.mkdir(parents=True, exist_ok=True)
        environment["TEMP"] = str(isolated_temp)
        environment["TMP"] = str(isolated_temp)
        environment_text = "\0".join(
            f"{key}={value}"
            for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())
            if key and "\0" not in key and "=" not in key and "\0" not in value
        )
        environment_buffer = ctypes.create_unicode_buffer(environment_text + "\0\0")
        command_buffer = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))

        attribute_size = ctypes.c_size_t()
        ctypes.set_last_error(0)
        self._api.kernel32.InitializeProcThreadAttributeList(
            None, 1, 0, ctypes.byref(attribute_size)
        )
        if not attribute_size.value or ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise OfflineIsolationUnavailable("appcontainer_attribute_list_failed")
        attribute_backing = ctypes.create_string_buffer(attribute_size.value)
        attribute_list = ctypes.cast(attribute_backing, ctypes.c_void_p)
        if not self._api.kernel32.InitializeProcThreadAttributeList(
            attribute_list, 1, 0, ctypes.byref(attribute_size)
        ):
            raise OfflineIsolationUnavailable("appcontainer_attribute_list_failed")
        capabilities = _SECURITY_CAPABILITIES(sid, None, 0, 0)
        if not self._api.kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            None,
            None,
        ):
            self._api.kernel32.DeleteProcThreadAttributeList(attribute_list)
            raise OfflineIsolationUnavailable("appcontainer_security_attribute_failed")

        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        stdin_fd = os.open(os.devnull, os.O_RDONLY)
        for descriptor in (stdout_write, stderr_write, stdin_fd):
            os.set_inheritable(descriptor, True)
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = wintypes.HANDLE(msvcrt.get_osfhandle(stdin_fd))
        startup.StartupInfo.hStdOutput = wintypes.HANDLE(msvcrt.get_osfhandle(stdout_write))
        startup.StartupInfo.hStdError = wintypes.HANDLE(msvcrt.get_osfhandle(stderr_write))
        startup.lpAttributeList = attribute_list
        process = _PROCESS_INFORMATION()
        process_created = False
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        readers: list[threading.Thread] = []
        deadline = time.monotonic() + timeout
        observer_result: dict[str, object] | None = None
        try:
            created = self._api.kernel32.CreateProcessW(
                str(executable),
                command_buffer,
                None,
                None,
                True,
                _EXTENDED_STARTUPINFO_PRESENT
                | _CREATE_SUSPENDED
                | _CREATE_UNICODE_ENVIRONMENT
                | _CREATE_NO_WINDOW,
                environment_buffer,
                str(resolved_cwd),
                ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_STARTUPINFOW)),
                ctypes.byref(process),
            )
            if not created:
                raise OfflineIsolationUnavailable("appcontainer_process_launch_failed")
            process_created = True
            os.close(stdout_write)
            stdout_write = -1
            os.close(stderr_write)
            stderr_write = -1
            os.close(stdin_fd)
            stdin_fd = -1

            parent_probe = self._verified_token_probe(process.hProcess)

            def read_pipe(descriptor: int, output: list[bytes]) -> None:
                try:
                    while True:
                        block = os.read(descriptor, 65536)
                        if not block:
                            break
                        output.append(block)
                except OSError:
                    pass
                finally:
                    os.close(descriptor)

            readers = [
                threading.Thread(target=read_pipe, args=(stdout_read, stdout_chunks), daemon=True),
                threading.Thread(target=read_pipe, args=(stderr_read, stderr_chunks), daemon=True),
            ]
            stdout_read = -1
            stderr_read = -1
            for reader in readers:
                reader.start()
            if self._api.kernel32.ResumeThread(process.hThread) == 0xFFFFFFFF:
                raise OfflineIsolationUnavailable("appcontainer_process_resume_failed")
            if observer is not None:
                observer_result = observer(int(process.dwProcessId))
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            wait_result = self._api.kernel32.WaitForSingleObject(process.hProcess, remaining_ms)
            if wait_result == _WAIT_TIMEOUT:
                self._api.kernel32.TerminateProcess(process.hProcess, 124)
                self._api.kernel32.WaitForSingleObject(process.hProcess, 5000)
                raise subprocess.TimeoutExpired(arguments, timeout)
            if wait_result != _WAIT_OBJECT_0:
                raise OfflineIsolationUnavailable("appcontainer_process_wait_failed")
            exit_code = wintypes.DWORD(_STILL_ACTIVE)
            if not self._api.kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
                raise OfflineIsolationUnavailable("appcontainer_process_status_failed")
            for reader in readers:
                reader.join(timeout=5)
            completed = subprocess.CompletedProcess(
                arguments,
                int(exit_code.value),
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            )
            return completed, parent_probe, observer_result
        except Exception:
            if process_created:
                self._api.kernel32.TerminateProcess(process.hProcess, 125)
                self._api.kernel32.WaitForSingleObject(process.hProcess, 5000)
            raise
        finally:
            for descriptor in (
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
                stdin_fd,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            for reader in readers:
                reader.join(timeout=1)
            if process_created:
                self._api.kernel32.CloseHandle(process.hThread)
                self._api.kernel32.CloseHandle(process.hProcess)
            self._api.kernel32.DeleteProcThreadAttributeList(attribute_list)

    def _observe_child_token(self, root_pid: int, marker: Path) -> dict[str, object]:
        sid, _ = self._require_profile_started()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if marker.is_file():
                for pid in self._api.descendant_pids(root_pid):
                    measured = self._api.process_token_probe(pid)
                    if measured is None:
                        continue
                    token_sid = measured.pop("token_sid", None)
                    if (
                        measured == {"is_appcontainer": True, "capability_count": 0}
                        and token_sid
                        and self._api.advapi32.EqualSid(ctypes.c_void_p(token_sid), sid)
                    ):
                        return measured
            time.sleep(0.01)
        raise OfflineIsolationUnavailable("appcontainer_child_probe_failed")

    def _probe_isolation(self) -> dict[str, object]:
        _, workspace_root = self._require_profile_started()
        probe_root = workspace_root / "isolation-probe"
        self.prepare_directory(probe_root)
        runtime_root = probe_root / "python"
        self.prepare_directory(runtime_root)
        bootstrap_python = self.prepare_python_runtime(runtime_root)
        with _LoopbackListener() as control_listener:
            try:
                with socket.create_connection(("127.0.0.1", control_listener.port), timeout=1):
                    control_observed = control_listener.wait_for_connection(1)
            except OSError as exc:
                raise OfflineIsolationUnavailable("appcontainer_loopback_control_failed") from exc
            if not control_observed:
                raise OfflineIsolationUnavailable("appcontainer_loopback_control_failed")
        marker = probe_root / "child-ready.txt"
        child_script = probe_root / "child.py"
        with _LoopbackListener() as child_listener:
            child_script.write_text(
                "from pathlib import Path\n"
                "import socket\n"
                "import time\n"
                "Path('child-ready.txt').write_text('ready', encoding='ascii')\n"
                "sock = socket.socket()\n"
                "sock.settimeout(0.2)\n"
                "try:\n"
                f"    sock.connect(('127.0.0.1', {child_listener.port}))\n"
                "except OSError:\n"
                "    pass\n"
                "finally:\n"
                "    sock.close()\n"
                "time.sleep(2)\n",
                encoding="utf-8",
            )
            child_completed, _, child_probe = self._launch(
                [
                    str(bootstrap_python),
                    "-I",
                    "-c",
                    "import subprocess,sys; subprocess.run([sys.executable, 'child.py'], check=False)",
                ],
                cwd=probe_root,
                timeout=10,
                env=os.environ,
                observer=lambda root_pid: self._observe_child_token(root_pid, marker),
            )
            child_network_observed = child_listener.wait_for_connection(0.5)
        if child_completed.returncode != 0 or child_probe is None:
            raise OfflineIsolationUnavailable("appcontainer_child_probe_failed")
        if child_network_observed:
            raise OfflineIsolationUnavailable("appcontainer_network_isolation_failed")

        network_code = (
            "import socket\n"
            f"for _ in range({_NETWORK_ATTEMPT_COUNT}):\n"
            "    sock = socket.socket()\n"
            "    sock.settimeout(0.2)\n"
            "    try:\n"
            "        sock.connect(('127.0.0.1', PORT))\n"
            "    except OSError:\n"
            "        pass\n"
            "    finally:\n"
            "        sock.close()\n"
        )
        with _LoopbackListener() as isolated_listener:
            _, parent_probe, _ = self._launch(
                [
                    str(bootstrap_python),
                    "-I",
                    "-c",
                    network_code.replace("PORT", str(isolated_listener.port)),
                ],
                cwd=probe_root,
                timeout=15,
                env=os.environ,
            )
            time.sleep(0.1)
            isolated_observed = bool(isolated_listener.connection_count)
        if isolated_observed:
            raise OfflineIsolationUnavailable("appcontainer_network_isolation_failed")

        shutil.rmtree(probe_root)
        return {
            "status": "ready",
            "code": "windows_appcontainer_network_isolation_verified",
            "primitive": "windows_appcontainer_zero_capabilities",
            "fail_closed": True,
            "declared_capabilities": [],
            "parent_probe": parent_probe | {"loopback_blocked": True},
            "child_probe": child_probe | {"loopback_blocked": True},
            "listener_control_connection_observed": control_observed,
            "listener_connection_observed": isolated_observed,
            "connection_attempt_count": _NETWORK_ATTEMPT_COUNT,
        }

    def prepare_python_runtime(self, destination: Path) -> Path:
        _, workspace_root = self._require_profile_started()
        target = Path(destination).resolve()
        if not self._is_within(target, workspace_root.resolve()):
            raise RuntimeError("isolated Python destination is invalid")
        if target.exists() and (not target.is_dir() or target.is_symlink()):
            raise RuntimeError("isolated Python destination is invalid")
        if (
            sys.version_info[:2] != (3, 11)
            or platform.python_implementation() != "CPython"
            or ctypes.sizeof(ctypes.c_void_p) != 8
        ):
            raise OfflineIsolationUnavailable("acceptance_python_cp311_x64_required")
        source = Path(sys.base_prefix).resolve()
        required_files = [
            source / "python.exe",
            source / "python3.dll",
            source / "python311.dll",
            source / "vcruntime140.dll",
        ]
        optional_files = [
            source / "pythonw.exe",
            source / "vcruntime140_1.dll",
            source / "LICENSE.txt",
        ]
        if any(path.is_symlink() or not path.is_file() for path in required_files):
            raise OfflineIsolationUnavailable("acceptance_python_runtime_incomplete")
        for tree in (source / "DLLs", source / "Lib"):
            if tree.is_symlink() or not tree.is_dir():
                raise OfflineIsolationUnavailable("acceptance_python_runtime_incomplete")
        target.mkdir(parents=True, exist_ok=True)
        try:
            for path in [*required_files, *optional_files]:
                if path.is_file() and not path.is_symlink():
                    shutil.copy2(path, target / path.name)
            shutil.copytree(source / "DLLs", target / "DLLs")

            def ignore_lib(directory: str, names: list[str]) -> set[str]:
                ignored = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
                if Path(directory).resolve() == (source / "Lib").resolve():
                    ignored.add("site-packages")
                return ignored

            shutil.copytree(source / "Lib", target / "Lib", ignore=ignore_lib)
            python = target / "python.exe"
            completed, _, _ = self._launch(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import platform,sys; assert sys.version_info[:2] == (3, 11); "
                    "assert platform.architecture()[0] == '64bit'",
                ],
                cwd=workspace_root,
                timeout=30,
                env=os.environ,
            )
            if completed.returncode != 0:
                raise OfflineIsolationUnavailable("acceptance_python_runtime_probe_failed")
            return python
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise


def open_offline_isolation() -> WindowsAppContainerIsolation:
    if os.name != "nt":
        raise OfflineIsolationUnavailable("windows_appcontainer_required")
    return WindowsAppContainerIsolation()
