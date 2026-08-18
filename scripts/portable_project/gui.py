from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import PortableProjectError
from .importer import import_project
from .manifest import read_manifest
from .validator import validate_package
from .version_policy import evaluate_version_policy

EnvironmentDetector = Callable[..., Mapping[str, Any]]
Importer = Callable[..., dict[str, Any]]


def importer_self_check() -> dict[str, str]:
    import tkinter  # noqa: F401

    return {"status": "ready", "code": "importer_self_check_ready"}


def format_non_file_dependencies(rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return ""
    rendered = ["需要目标账号或本机资源确认："]
    for raw_row in rows:
        row = raw_row if isinstance(raw_row, dict) else {}
        kind = str(row.get("kind") or "unknown").strip()
        identity = str(
            row.get("name") or row.get("resource_id") or row.get("id") or "未命名资源"
        ).strip()
        rendered.append(f"- {kind}: {identity}")
    return "\n".join(rendered)


def import_completion_copy(result: Mapping[str, Any]) -> tuple[str, str]:
    status = str(result.get("status") or "")
    draft_path = str(result.get("draft_path") or "")
    if status == "diagnostic_imported_unverified":
        return (
            "诊断导入完成（未验证）",
            f"状态：{status}\n工程：{draft_path}\n"
            "如单个素材仍异常，可在剪映中使用“链接素材”诊断处理；"
            "该结果不代表完整工程验收。",
        )
    return "导入完成", f"状态：{status}\n工程：{draft_path}\n请在剪映中打开、预览并确认可继续编辑。"


def default_package_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def preferred_import_app(context: Mapping[str, Any], *, diagnostic_override: bool) -> str:
    if diagnostic_override and bool(context.get("diagnostic_override_available")):
        diagnostic_environment = context.get("diagnostic_target_environment")
        if isinstance(diagnostic_environment, Mapping):
            diagnostic_app = str(diagnostic_environment.get("app_name") or "").strip()
            if diagnostic_app:
                return diagnostic_app
    return str(context.get("source_app") or "").strip()


def inspect_import_context(
    package_dir: Path,
    *,
    detector: EnvironmentDetector,
    manifest_reader: Callable[[Path], Mapping[str, Any]] = read_manifest,
    package_validator: Callable[[Path], Mapping[str, Any]] = validate_package,
) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    if package.suffix.casefold() == ".zip" or not package.is_dir():
        raise PortableProjectError("package_not_extracted", "请先完整解压工程包，再运行导入工具")
    static_report = dict(package_validator(package))
    manifest = dict(manifest_reader(package))
    project = manifest.get("project")
    if not isinstance(project, dict):
        raise PortableProjectError("invalid_manifest", "工程包缺少项目身份信息")
    source_app = str(project.get("source_app") or "")
    environment = dict(
        detector(
            force_refresh=True,
            include_process=True,
            preferred_app=source_app or None,
        )
    )
    diagnostic_environment: dict[str, Any] = {}
    if not bool(environment.get("found")):
        candidate = dict(
            detector(
                force_refresh=True,
                include_process=True,
                preferred_app=None,
            )
        )
        candidate_app = str(candidate.get("app_name") or "").strip()
        if (
            bool(candidate.get("found"))
            and candidate_app.casefold()
            in {
                "jianyingpro",
                "capcut",
            }
            and candidate_app.casefold() != source_app.casefold()
        ):
            diagnostic_environment = candidate
    version_policy = evaluate_version_policy(
        str(project.get("source_app") or ""),
        str(project.get("source_version") or ""),
        str(environment.get("app_name") or ""),
        str(environment.get("app_version") or ""),
    )
    return {
        "package_dir": str(package),
        "project_name": str(project.get("name") or package.name),
        "source_app": source_app,
        "source_version": str(project.get("source_version") or ""),
        "target_environment": environment,
        "diagnostic_target_environment": diagnostic_environment,
        "diagnostic_override_available": bool(diagnostic_environment),
        "version_policy": version_policy,
        "static_report": static_report,
        "non_file_dependencies": list(manifest.get("non_file_dependencies") or []),
        "non_file_dependency_count": len(manifest.get("non_file_dependencies") or []),
    }


def perform_import(
    package_dir: Path,
    *,
    project_name: str | None,
    detector: EnvironmentDetector,
    importer: Importer = import_project,
    diagnostic_override: bool = False,
    preferred_app: str | None = None,
) -> dict[str, Any]:
    environment = dict(
        detector(
            force_refresh=True,
            include_process=True,
            preferred_app=preferred_app or None,
        )
    )
    drafts_root = str(environment.get("drafts_root") or "").strip()
    if not drafts_root:
        raise PortableProjectError("target_drafts_root_missing", "未能检测到剪映草稿目录")
    return importer(
        Path(package_dir).resolve(),
        Path(drafts_root),
        environment,
        project_name=project_name,
        diagnostic_override=diagnostic_override,
    )


def run_importer_gui(
    package_dir: Path | None = None,
    *,
    detector: EnvironmentDetector,
    importer: Importer = import_project,
) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    package = Path(package_dir or default_package_dir()).resolve()
    root = tk.Tk()
    root.title("AutoCut 完整剪映工程导入工具")
    root.geometry("640x430")
    root.minsize(640, 430)
    root.maxsize(820, 560)

    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    title = ttk.Label(frame, text="完整剪映工程导入", font=("Microsoft YaHei UI", 15, "bold"))
    title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))

    ttk.Label(frame, text="工程包").grid(row=1, column=0, sticky="w", pady=5)
    package_value = ttk.Label(frame, text=str(package), wraplength=470)
    package_value.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)

    ttk.Label(frame, text="来源版本").grid(row=2, column=0, sticky="w", pady=5)
    source_value = ttk.Label(frame, text="-")
    source_value.grid(row=2, column=1, sticky="w", pady=5)

    ttk.Label(frame, text="本机版本").grid(row=3, column=0, sticky="w", pady=5)
    target_value = ttk.Label(frame, text="-")
    target_value.grid(row=3, column=1, sticky="w", pady=5)

    ttk.Label(frame, text="工程名称").grid(row=4, column=0, sticky="w", pady=5)
    project_name = tk.StringVar()
    name_entry = ttk.Entry(frame, textvariable=project_name)
    name_entry.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)

    policy_value = ttk.Label(frame, text="正在检查...", wraplength=520)
    policy_value.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 5))

    diagnostic = tk.BooleanVar(value=False)
    diagnostic_check = ttk.Checkbutton(
        frame,
        text="诊断导入（结果不代表版本兼容）",
        variable=diagnostic,
    )
    diagnostic_check.grid(row=6, column=0, columnspan=3, sticky="w", pady=5)
    diagnostic_check.state(["disabled"])

    dependencies_value = ttk.Label(frame, text="", wraplength=520)
    dependencies_value.grid(row=7, column=0, columnspan=3, sticky="w", pady=5)

    status_value = ttk.Label(frame, text="", wraplength=520)
    status_value.grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 8))

    actions = ttk.Frame(frame)
    actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    actions.columnconfigure(0, weight=1)

    context: dict[str, Any] = {}

    def set_busy(busy: bool) -> None:
        if busy:
            import_button.state(["disabled"])
            refresh_button.state(["disabled"])
            name_entry.state(["disabled"])
            diagnostic_check.state(["disabled"])
        else:
            refresh_button.state(["!disabled"])
            name_entry.state(["!disabled"])
            if context:
                import_button.state(["!disabled"])
            policy_code = str(context.get("version_policy", {}).get("code") or "")
            if policy_code in {"target_app_mismatch", "unknown_app_version"} or bool(
                context.get("diagnostic_override_available")
            ):
                diagnostic_check.state(["!disabled"])
            else:
                diagnostic_check.state(["disabled"])

    def finish_refresh(
        refreshed_context: dict[str, Any] | None,
        error: BaseException | None,
    ) -> None:
        nonlocal context
        if error is None:
            assert refreshed_context is not None
            context = refreshed_context
            project_name.set(context["project_name"])
            source_value.configure(text=f'{context["source_app"]} {context["source_version"]}')
            environment = context["target_environment"]
            if not environment.get("found") and context.get("diagnostic_override_available"):
                environment = context["diagnostic_target_environment"]
            target_value.configure(
                text=f'{environment.get("app_name") or "未检测到"} '
                f'{environment.get("app_version") or "未知版本"}'
            )
            policy = context["version_policy"]
            policy_value.configure(text=f'兼容性：{policy["code"]}')
            override_allowed = policy["code"] in {
                "target_app_mismatch",
                "unknown_app_version",
            } or bool(context.get("diagnostic_override_available"))
            diagnostic.set(False)
            diagnostic_check.state(["!disabled"] if override_allowed else ["disabled"])
            dependencies_value.configure(
                text=format_non_file_dependencies(context["non_file_dependencies"])
            )
            status_value.configure(text="工程包静态验证通过")
        elif isinstance(error, PortableProjectError):
            context = {}
            status_value.configure(text=f"{error.code}：{error.reason}")
        else:
            context = {}
            status_value.configure(text="portable_project_operation_failed：工程包检查未完成")
        set_busy(False)

    def refresh() -> None:
        set_busy(True)
        status_value.configure(text="正在验证工程包和本机剪映环境...")

        def worker() -> None:
            try:
                refreshed_context = inspect_import_context(package, detector=detector)
            except BaseException as exc:  # delivered to the UI thread
                root.after(0, finish_refresh, None, exc)
            else:
                root.after(0, finish_refresh, refreshed_context, None)

        threading.Thread(target=worker, daemon=True).start()

    def finish_import(result: dict[str, Any] | None, error: BaseException | None) -> None:
        set_busy(False)
        if error is not None:
            if isinstance(error, PortableProjectError):
                status_value.configure(text=f"{error.code}：{error.reason}")
            else:
                status_value.configure(text="import_failed：导入未完成")
            return
        assert result is not None
        title, body = import_completion_copy(result)
        status_value.configure(text=body)
        if result.get("status") == "diagnostic_imported_unverified":
            messagebox.showwarning(title, body)
        else:
            messagebox.showinfo(title, body)
        executable = str(context.get("target_environment", {}).get("executable_path") or "")
        if executable and Path(executable).is_file():
            launch_button.configure(command=lambda: subprocess.Popen([executable]))
            launch_button.grid()

    def start_import() -> None:
        selected_project_name = project_name.get()
        selected_diagnostic = diagnostic.get()
        set_busy(True)
        status_value.configure(text="正在复制、重绑定并验证工程...")

        def worker() -> None:
            try:
                result = perform_import(
                    package,
                    project_name=selected_project_name,
                    detector=detector,
                    importer=importer,
                    diagnostic_override=selected_diagnostic,
                    preferred_app=preferred_import_app(
                        context,
                        diagnostic_override=selected_diagnostic,
                    )
                    or None,
                )
            except BaseException as exc:  # delivered to the UI thread
                root.after(0, finish_import, None, exc)
            else:
                root.after(0, finish_import, result, None)

        threading.Thread(target=worker, daemon=True).start()

    refresh_button = ttk.Button(actions, text="重新检测", command=refresh)
    refresh_button.grid(row=0, column=1, padx=(0, 8))
    import_button = ttk.Button(actions, text="导入工程", command=start_import)
    import_button.grid(row=0, column=2)
    import_button.state(["disabled"])
    launch_button = ttk.Button(actions, text="打开剪映")
    launch_button.grid(row=0, column=3, padx=(8, 0))
    launch_button.grid_remove()

    root.after(0, refresh)
    root.mainloop()
    return 0
