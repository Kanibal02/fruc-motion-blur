from __future__ import annotations

import logging
import os
import queue
import threading
import time
import webbrowser
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import Menu, filedialog, messagebox, ttk

import customtkinter as ctk

try:
    from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD
except ImportError:
    COPY = None
    DND_FILES = None
    REFUSE_DROP = None
    TkinterDnD = None

from .ffmpeg import (
    VIDEO_EXTENSIONS,
    Capabilities,
    build_render_command,
    command_text,
    detect_capabilities,
    filter_chain,
    output_paths,
    probe_media,
)
from .models import JobStatus, RenderJob, RenderSettings, format_time
from .paths import LOG_DIR, ensure_app_dirs, find_binary
from .renderer import Renderer
from .settings import load_settings, save_settings


PRESETS = {
    "Clean": (4, "linear"),
    "Extra smooth": (6, "linear"),
    "Insane": (8, "linear"),
    "Soft": (4, "hermite"),
}
MIXER_LABELS = {
    "linear": "Linear — more motion blur (default)",
    "hermite": "Hermite — less motion blur",
}
MIXERS_BY_LABEL = {label: mixer for mixer, label in MIXER_LABELS.items()}
TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


def setup_logging() -> logging.Logger:
    ensure_app_dirs()
    logger = logging.getLogger("fruc_motion_blur")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "fruc-motion-blur.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class FRUCApp(ctk.CTk):
    def __init__(self) -> None:
        self.settings = load_settings()
        ctk.set_appearance_mode(self.settings.appearance)
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title("FRUC Motion Blur")
        self.geometry("1280x820")
        self.minsize(1040, 680)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.logger = setup_logging()
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self.ffmpeg = find_binary("ffmpeg")
        self.ffprobe = find_binary("ffprobe")
        self.capabilities: Capabilities | None = None
        self.jobs: dict[str, RenderJob] = {}
        self.active_job_ids: list[str] = []
        self.log_lines: deque[str] = deque(maxlen=4000)
        self.current_job_id: str | None = None
        self.renderer = Renderer(self.ffmpeg, self.ffprobe, self.events) if self.ffmpeg and self.ffprobe else None
        self.setting_controls: list[ctk.CTkBaseClass] = []
        self._closing = False

        self._make_variables()
        self._configure_tree_style()
        self._build_ui()
        self._configure_drop()
        self._sync_output_controls()
        self._toggle_advanced(force=self.settings.advanced_open)
        self._append_log("INFO", "Application started")
        self.after(100, self._poll_events)
        self.after(150, self._start_capability_check)

    def _make_variables(self) -> None:
        s = self.settings
        self.preset_var = ctk.StringVar(value="Custom")
        self.multiplier_var = ctk.StringVar(value=f"{s.multiplier}×")
        self.performance_var = ctk.StringVar(value=s.performance.title())
        self.grid_var = ctk.StringVar(value=str(s.grid))
        self.mixer_var = ctk.StringVar(value=MIXER_LABELS.get(s.frame_mixer, MIXER_LABELS["linear"]))
        self.qp_var = ctk.IntVar(value=s.qp)
        self.auto_mp4_var = ctk.BooleanVar(value=s.auto_mp4)
        self.keep_ts_var = ctk.BooleanVar(value=s.keep_ts)
        self.same_output_var = ctk.BooleanVar(value=s.output_same_as_source)
        self.output_dir_var = ctk.StringVar(value=s.output_directory)
        self.appearance_var = ctk.StringVar(value=s.appearance)
        self.device_var = ctk.StringVar(value=str(s.device_index))
        self.capability_var = ctk.StringVar(value="Checking FFmpeg capabilities…")
        self.selection_var = ctk.StringVar(value="No file selected")
        self.stage_var = ctk.StringVar(value="Idle")
        self.progress_var = ctk.StringVar(value="0%  •  0.00×  •  ETA --:--:--")
        self.overall_var = ctk.StringVar(value="Queue 0%")
        self.diagnostics_var = ctk.StringVar(value="Capabilities are being checked…")
        self.advanced_visible = False

    def _configure_tree_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Queue.Treeview", background="#17191d", foreground="#e8e8e8",
            fieldbackground="#17191d", borderwidth=0, rowheight=36,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Queue.Treeview.Heading", background="#24272d", foreground="#b9bec8",
            borderwidth=0, font=("Segoe UI Semibold", 10), relief="flat",
        )
        style.map("Queue.Treeview", background=[("selected", "#255b8e")])

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, corner_radius=0, height=66)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="FRUC Motion Blur", font=ctk.CTkFont(size=23, weight="bold")).grid(
            row=0, column=0, padx=(22, 8), pady=18
        )
        ctk.CTkLabel(header, text="Vulkan FRUC + temporal mixing", text_color="#8f98a6").grid(
            row=0, column=1, sticky="w"
        )
        self.capability_label = ctk.CTkLabel(header, textvariable=self.capability_var, text_color="#e7b75f")
        self.capability_label.grid(row=0, column=2, padx=12)
        ctk.CTkComboBox(
            header, values=["Dark", "Light", "System"], variable=self.appearance_var,
            command=self._change_appearance, width=105,
        ).grid(row=0, column=3, padx=(0, 22))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(14, 16))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)

        self.drop_zone = ctk.CTkButton(
            left, text="Drop video files here\n—or click to browse—",
            command=self._pick_files, height=76, fg_color="transparent",
            border_width=2, border_color="#376d9e", hover_color=("#e8f2fa", "#202b35"),
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.drop_zone.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(left, textvariable=self.selection_var, anchor="w", text_color="#9da5b1").grid(
            row=1, column=0, sticky="ew", pady=(7, 7)
        )

        queue_card = ctk.CTkFrame(left)
        queue_card.grid(row=2, column=0, sticky="nsew")
        queue_card.grid_columnconfigure(0, weight=1)
        queue_card.grid_rowconfigure(1, weight=1)
        queue_header = ctk.CTkFrame(queue_card, fg_color="transparent")
        queue_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 5))
        queue_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(queue_header, text="Queue", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="w")
        self.add_button = ctk.CTkButton(queue_header, text="Add files", width=86, command=self._pick_files)
        self.add_button.grid(row=0, column=1, padx=4)
        self.remove_button = ctk.CTkButton(queue_header, text="Remove", width=78, fg_color="#4a4e57", command=self._remove_selected)
        self.remove_button.grid(row=0, column=2, padx=4)
        self.clear_button = ctk.CTkButton(queue_header, text="Clear completed", width=118, fg_color="#4a4e57", command=self._clear_completed)
        self.clear_button.grid(row=0, column=3, padx=4)

        self.tree = ttk.Treeview(
            queue_card, style="Queue.Treeview", columns=("media", "samples", "status"),
            show="tree headings", selectmode="browse",
        )
        self.tree.heading("#0", text="File")
        self.tree.heading("media", text="Resolution / FPS / Duration")
        self.tree.heading("samples", text="Samples")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=260, minwidth=150)
        self.tree.column("media", width=245, minwidth=180)
        self.tree.column("samples", width=68, minwidth=58, anchor="center")
        self.tree.column("status", width=130, minwidth=100, anchor="center")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.tree.tag_configure("done", foreground="#65d79a")
        self.tree.tag_configure("failed", foreground="#ff7272")
        self.tree.tag_configure("active", foreground="#6eb8ff")
        self.tree.tag_configure("cancelled", foreground="#d5a960")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._open_selected_folder)
        self.tree.bind("<Button-3>", self._show_context_menu)

        progress_card = ctk.CTkFrame(left)
        progress_card.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        progress_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(progress_card, textvariable=self.stage_var, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=14, pady=(11, 3)
        )
        self.current_progress = ctk.CTkProgressBar(progress_card)
        self.current_progress.set(0)
        self.current_progress.grid(row=1, column=0, sticky="ew", padx=14)
        ctk.CTkLabel(progress_card, textvariable=self.progress_var, anchor="w", text_color="#9da5b1").grid(
            row=2, column=0, sticky="ew", padx=14
        )
        self.overall_progress = ctk.CTkProgressBar(progress_card, progress_color="#6c7ee1")
        self.overall_progress.set(0)
        self.overall_progress.grid(row=3, column=0, sticky="ew", padx=14, pady=(5, 0))
        ctk.CTkLabel(progress_card, textvariable=self.overall_var, anchor="w", text_color="#9da5b1").grid(
            row=4, column=0, sticky="ew", padx=14, pady=(0, 7)
        )
        controls = ctk.CTkFrame(progress_card, fg_color="transparent")
        controls.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 11))
        controls.grid_columnconfigure(0, weight=1)
        self.start_button = ctk.CTkButton(controls, text="Start Queue", height=38, command=self._start_queue)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=4)
        self.cancel_button = ctk.CTkButton(controls, text="Cancel Current", width=116, state="disabled", fg_color="#905d2d", command=self._cancel_current)
        self.cancel_button.grid(row=0, column=1, padx=4)
        self.stop_button = ctk.CTkButton(controls, text="Stop Queue", width=100, state="disabled", fg_color="#8d3d47", command=self._stop_queue)
        self.stop_button.grid(row=0, column=2, padx=4)
        self.open_button = ctk.CTkButton(controls, text="Open Output", width=104, state="disabled", fg_color="#4a4e57", command=self._open_selected_folder)
        self.open_button.grid(row=0, column=3, padx=4)

        self.log_toggle = ctk.CTkButton(left, text="Show render log ▾", height=29, fg_color="transparent", command=self._toggle_log)
        self.log_toggle.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.log_box = ctk.CTkTextbox(left, height=150, font=("Consolas", 10), wrap="none")
        self.log_box.configure(state="disabled")
        self.log_visible = False

        self._build_settings(body)

    def _build_settings(self, body: ctk.CTkFrame) -> None:
        panel = ctk.CTkScrollableFrame(body, width=345, label_text="Render settings")
        panel.grid(row=0, column=1, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        row = 0

        def title(text: str) -> None:
            nonlocal row
            ctk.CTkLabel(panel, text=text, anchor="w", font=ctk.CTkFont(size=14, weight="bold")).grid(
                row=row, column=0, sticky="ew", padx=8, pady=(14, 5)
            )
            row += 1

        title("Preset")
        preset = ctk.CTkComboBox(panel, values=list(PRESETS), variable=self.preset_var, command=self._apply_preset)
        preset.grid(row=row, column=0, sticky="ew", padx=8)
        self.setting_controls.append(preset)
        row += 1

        title("Temporal sampling")
        multiplier = ctk.CTkSegmentedButton(
            panel, values=["2×", "3×", "4×", "6×", "8×"], variable=self.multiplier_var,
            command=lambda _: self._settings_changed(),
        )
        multiplier.grid(row=row, column=0, sticky="ew", padx=8)
        self.setting_controls.append(multiplier)
        row += 1

        title("FRUC optical flow")
        flow = ctk.CTkFrame(panel, fg_color="transparent")
        flow.grid(row=row, column=0, sticky="ew", padx=8)
        flow.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(flow, text="Performance", text_color="#8f98a6").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(flow, text="Flow grid", text_color="#8f98a6").grid(row=0, column=1, sticky="w", padx=(4, 0))
        performance = ctk.CTkComboBox(
            flow, values=["Fast", "Medium", "Slow"], variable=self.performance_var,
            command=lambda _: self._settings_changed(),
        )
        performance.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        grid = ctk.CTkComboBox(
            flow, values=["1", "2", "4"], variable=self.grid_var,
            command=lambda _: self._settings_changed(),
        )
        grid.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        self.setting_controls += [performance, grid]
        row += 1
        ctk.CTkLabel(
            panel,
            text="Fast = quickest; Slow = best matching\nGrid 1 = finest detail; 4 = faster/coarser",
            justify="left", text_color="#8f98a6", anchor="w",
        ).grid(
            row=row, column=0, sticky="ew", padx=8
        )
        row += 1

        title("Motion mixer")
        self.mixer_combo = ctk.CTkComboBox(
            panel, values=[self.mixer_var.get()], variable=self.mixer_var,
            command=lambda _: self._settings_changed(),
        )
        self.mixer_combo.grid(row=row, column=0, sticky="ew", padx=8)
        self.setting_controls.append(self.mixer_combo)
        row += 1
        ctk.CTkLabel(
            panel, text="Detected libplacebo temporal mixers only.", text_color="#8f98a6", anchor="w"
        ).grid(row=row, column=0, sticky="ew", padx=8)
        row += 1

        title("H.264 Vulkan quality")
        quality = ctk.CTkFrame(panel, fg_color="transparent")
        quality.grid(row=row, column=0, sticky="ew", padx=8)
        quality.grid_columnconfigure(0, weight=1)
        self.qp_label = ctk.CTkLabel(quality, text=f"QP {self.qp_var.get()}")
        self.qp_label.grid(row=0, column=1, padx=(8, 0))
        qp = ctk.CTkSlider(quality, from_=18, to=40, number_of_steps=22, variable=self.qp_var, command=self._qp_changed)
        qp.grid(row=0, column=0, sticky="ew")
        self.setting_controls.append(qp)
        row += 1

        title("Output")
        same = ctk.CTkSwitch(panel, text="Save beside source", variable=self.same_output_var, command=self._output_mode_changed)
        same.grid(row=row, column=0, sticky="w", padx=8)
        self.setting_controls.append(same)
        row += 1
        out = ctk.CTkFrame(panel, fg_color="transparent")
        out.grid(row=row, column=0, sticky="ew", padx=8, pady=5)
        out.grid_columnconfigure(0, weight=1)
        self.output_entry = ctk.CTkEntry(out, textvariable=self.output_dir_var, placeholder_text="Custom output folder")
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.output_browse = ctk.CTkButton(out, text="…", width=34, command=self._pick_output_directory)
        self.output_browse.grid(row=0, column=1)
        self.setting_controls += [self.output_entry, self.output_browse]
        row += 1
        auto = ctk.CTkCheckBox(panel, text="Automatic MP4 remux", variable=self.auto_mp4_var, command=self._settings_changed)
        auto.grid(row=row, column=0, sticky="w", padx=8, pady=3)
        self.setting_controls.append(auto)
        row += 1
        keep = ctk.CTkCheckBox(panel, text="Keep TS", variable=self.keep_ts_var, command=self._settings_changed)
        keep.grid(row=row, column=0, sticky="w", padx=8, pady=3)
        self.setting_controls.append(keep)
        row += 1

        self.advanced_button = ctk.CTkButton(panel, text="Advanced ▾", fg_color="#454a54", command=self._toggle_advanced)
        self.advanced_button.grid(row=row, column=0, sticky="ew", padx=8, pady=(16, 5))
        self.setting_controls.append(self.advanced_button)
        row += 1
        self.advanced_frame = ctk.CTkFrame(panel)
        self.advanced_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.advanced_frame, text="Vulkan device index", anchor="w").grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        device = ctk.CTkComboBox(
            self.advanced_frame, values=[str(i) for i in range(8)], variable=self.device_var,
            command=self._device_changed,
        )
        device.grid(row=1, column=0, sticky="ew", padx=8)
        self.setting_controls.append(device)
        ctk.CTkLabel(
            self.advanced_frame, textvariable=self.diagnostics_var, justify="left", anchor="w",
            wraplength=305, text_color="#aeb6c2",
        ).grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkButton(self.advanced_frame, text="Copy Command", command=self._copy_command).grid(
            row=3, column=0, sticky="ew", padx=8, pady=(0, 5)
        )
        ctk.CTkButton(self.advanced_frame, text="FFmpeg help", fg_color="transparent", command=lambda: webbrowser.open("https://ffmpeg.org/ffmpeg-filters.html")).grid(
            row=4, column=0, sticky="ew", padx=8, pady=(0, 8)
        )
        self.advanced_grid_row = row

    def _configure_drop(self) -> None:
        if not (TkinterDnD and DND_FILES and COPY and REFUSE_DROP):
            self._append_log("WARNING", "Drag-and-drop unavailable; install tkinterdnd2")
            self.drop_zone.configure(text="Click to add video files\n(install tkinterdnd2 for drag-and-drop)")
            return
        try:
            TkinterDnD._require(self)
            widgets = list(self.winfo_children())
            for widget in widgets:
                widgets.extend(widget.winfo_children())
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DropEnter>>", self._drop_action)
                widget.dnd_bind("<<DropPosition>>", self._drop_action)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:
            self._append_log("WARNING", f"Drag-and-drop unavailable: {exc}")

    def _start_capability_check(self) -> None:
        if not self.ffmpeg or not self.ffprobe:
            missing = "ffmpeg.exe" if not self.ffmpeg else "ffprobe.exe"
            self.capability_var.set(f"Missing {missing}")
            self.capability_label.configure(text_color="#ff7272")
            self.start_button.configure(state="disabled")
            self._append_log("ERROR", f"{missing} not found in ffmpeg/bin or PATH")
            return
        self._append_log("INFO", f"FFmpeg: {self.ffmpeg}")
        self._append_log("INFO", f"FFprobe: {self.ffprobe}")
        device = int(self.device_var.get())

        def check() -> None:
            try:
                caps = detect_capabilities(self.ffmpeg, device)
                self.events.put({"event": "capabilities", "capabilities": caps})
            except Exception as exc:
                self.events.put({"event": "capability_error", "error": str(exc)})

        threading.Thread(target=check, daemon=True).start()

    def _pick_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Add video files",
            filetypes=[("Video files", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))), ("All files", "*.*")],
        )
        if selected:
            self._add_paths([Path(path) for path in selected])

    def _on_drop(self, event: object) -> str:
        if self.renderer and self.renderer.running:
            return str(REFUSE_DROP)
        data = getattr(event, "data", "")
        try:
            paths = [Path(value) for value in self.tk.splitlist(data)]
        except Exception:
            paths = [Path(data.strip("{}"))] if data else []
        self._add_paths(paths)
        return str(COPY)

    def _drop_action(self, _event: object) -> str:
        return str(REFUSE_DROP if self.renderer and self.renderer.running else COPY)

    def _add_paths(self, paths: list[Path]) -> None:
        expanded: list[Path] = []
        for path in paths:
            if path.is_dir():
                try:
                    expanded.extend(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS)
                except OSError as exc:
                    self._append_log("ERROR", f"Could not scan {path}: {exc}")
            elif path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                expanded.append(path)
        existing = {os.path.normcase(str(job.input_path.resolve())) for job in self.jobs.values()}
        added: list[RenderJob] = []
        for path in expanded:
            resolved = path.resolve()
            key = os.path.normcase(str(resolved))
            if key in existing:
                continue
            existing.add(key)
            job = RenderJob(resolved, status=JobStatus.PROBING)
            self.jobs[job.id] = job
            self.tree.insert("", "end", iid=job.id, text=resolved.name, values=("Inspecting…", self.multiplier_var.get(), JobStatus.PROBING.value), tags=("active",))
            added.append(job)
        if added:
            self.tree.selection_set(added[0].id)
            threading.Thread(target=self._probe_jobs, args=(added,), daemon=True).start()
        elif paths:
            self._append_log("WARNING", "No new supported top-level video files were found")

    def _probe_jobs(self, jobs: list[RenderJob]) -> None:
        if not self.ffprobe:
            return
        for job in jobs:
            try:
                info = probe_media(self.ffprobe, job.input_path)
                self.events.put({"event": "probe_ready", "job_id": job.id, "probe": info})
            except Exception as exc:
                self.events.put({"event": "probe_failed", "job_id": job.id, "error": str(exc)})

    def _remove_selected(self) -> None:
        if self.renderer and self.renderer.running:
            return
        for job_id in self.tree.selection():
            self.jobs.pop(job_id, None)
            self.tree.delete(job_id)
        self.selection_var.set("No file selected")

    def _clear_completed(self) -> None:
        if self.renderer and self.renderer.running:
            return
        for job_id, job in list(self.jobs.items()):
            if job.status in TERMINAL_STATUSES:
                self.tree.delete(job_id)
                del self.jobs[job_id]

    def _on_select(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected:
            self.selection_var.set("No file selected")
            self.open_button.configure(state="disabled")
            return
        job = self.jobs[selected[0]]
        self.selection_var.set(f"{job.input_path}  •  {job.details}")
        self.open_button.configure(state="normal" if job.output_path else "disabled")
        self._update_diagnostics()

    def _show_context_menu(self, event: object) -> None:
        row = self.tree.identify_row(getattr(event, "y", 0))
        if not row:
            return
        self.tree.selection_set(row)
        menu = Menu(self, tearoff=False)
        menu.add_command(label="Open input folder", command=lambda: self._open_path(self.jobs[row].input_path.parent))
        if self.jobs[row].output_path:
            menu.add_command(label="Open output folder", command=self._open_selected_folder)
            menu.add_command(label="Copy output path", command=lambda: self.clipboard_append(str(self.jobs[row].output_path)))
        menu.add_separator()
        menu.add_command(label="Remove", command=self._remove_selected, state="disabled" if self.renderer and self.renderer.running else "normal")
        menu.tk_popup(getattr(event, "x_root", 0), getattr(event, "y_root", 0))

    def _start_queue(self) -> None:
        if not self.renderer or not self.capabilities or not self.capabilities.ready:
            messagebox.showerror("Cannot render", "Required FFmpeg/Vulkan capabilities are not ready.")
            return
        settings = self._collect_settings()
        if not settings.output_same_as_source:
            if not settings.output_directory:
                messagebox.showerror("Output folder", "Choose a custom output folder or save beside the source.")
                return
            try:
                Path(settings.output_directory).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Output folder", str(exc))
                return
        candidates = [job for job in self.jobs.values() if job.status in {JobStatus.WAITING, JobStatus.FAILED, JobStatus.CANCELLED}]
        if not candidates:
            messagebox.showinfo("Queue", "Add at least one video or retry a failed/cancelled item.")
            return
        for job in candidates:
            job.progress = 0
            job.error = ""
            job.output_path = None
            job.status = JobStatus.WAITING
            self._update_row(job)
        self.settings = settings
        save_settings(settings)
        self.active_job_ids = [job.id for job in candidates]
        if self.renderer.start(candidates, settings):
            self._set_rendering_ui(True)

    def _cancel_current(self) -> None:
        if self.renderer:
            self.renderer.cancel_current()
            self.stage_var.set("Cancelling current job…")

    def _stop_queue(self) -> None:
        if self.renderer:
            self.renderer.stop_queue()
            self.stage_var.set("Stopping queue…")

    def _set_rendering_ui(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.add_button.configure(state="disabled" if running else "normal")
        self.remove_button.configure(state="disabled" if running else "normal")
        self.clear_button.configure(state="disabled" if running else "normal")
        self.drop_zone.configure(state="disabled" if running else "normal")
        for control in self.setting_controls:
            try:
                control.configure(state="disabled" if running else "normal")
            except (ValueError, TypeError):
                pass
        if not running:
            self._sync_output_controls()

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        if not self._closing:
            self.after(100, self._poll_events)

    def _handle_event(self, event: dict[str, object]) -> None:
        kind = event["event"]
        if kind == "capabilities":
            caps = event["capabilities"]
            assert isinstance(caps, Capabilities)
            self.capabilities = caps
            if caps.ready:
                self.capability_var.set("Vulkan ready")
                self.capability_label.configure(text_color="#65d79a")
                self.mixer_combo.configure(values=[MIXER_LABELS[mixer] for mixer in caps.mixers])
                if MIXERS_BY_LABEL.get(self.mixer_var.get()) not in caps.mixers:
                    self.mixer_var.set(MIXER_LABELS[caps.mixers[0]])
                self._append_log("INFO", f"{caps.version}; mixers: {', '.join(caps.mixers)}")
            else:
                self.capability_var.set("Missing: " + ", ".join(caps.missing or ["frame mixer"]))
                self.capability_label.configure(text_color="#ff7272")
                self.start_button.configure(state="disabled")
                self._append_log("ERROR", self.capability_var.get())
            self._update_diagnostics()
        elif kind == "capability_error":
            self.capability_var.set("Capability check failed")
            self.capability_label.configure(text_color="#ff7272")
            self.start_button.configure(state="disabled")
            self._append_log("ERROR", str(event["error"]))
        elif kind in {"probe_ready", "probed"}:
            job = self.jobs.get(str(event["job_id"]))
            if job:
                job.probe = event["probe"]  # type: ignore[assignment]
                if kind == "probe_ready" and job.status == JobStatus.PROBING:
                    job.status = JobStatus.WAITING
                self._update_row(job)
                self._on_select()
        elif kind == "probe_failed":
            job = self.jobs.get(str(event["job_id"]))
            if job:
                job.status = JobStatus.FAILED
                job.error = str(event["error"])
                self._update_row(job)
                self._append_log("ERROR", f"{job.input_path.name}: {job.error}")
        elif kind == "queue_started":
            self.stage_var.set("Queue started")
        elif kind == "status":
            job = self.jobs.get(str(event["job_id"]))
            if job:
                job.status = event["status"]  # type: ignore[assignment]
                if "error" in event:
                    job.error = str(event["error"])
                if "output_path" in event:
                    job.output_path = Path(event["output_path"])  # type: ignore[arg-type]
                if job.status in {JobStatus.RENDERING, JobStatus.REMUXING, JobStatus.PROBING}:
                    self.current_job_id = job.id
                    self.stage_var.set(f"{job.status.value}: {job.input_path.name}")
                self._update_row(job)
                self._update_overall()
                self._on_select()
        elif kind == "progress":
            job = self.jobs.get(str(event["job_id"]))
            if job:
                stage_fraction = float(event["fraction"])
                job.progress = max(job.progress, stage_fraction)
                self.current_progress.set(stage_fraction)
                eta = format_time(event.get("eta") if isinstance(event.get("eta"), (int, float)) else None)
                self.progress_var.set(f"{stage_fraction * 100:.1f}%  •  {float(event['speed']):.2f}×  •  ETA {eta if event.get('eta') is not None else '--:--:--'}")
                self.stage_var.set(f"{event['stage']}: {job.input_path.name}")
                self._update_row(job)
                self._update_overall()
        elif kind == "command":
            self._append_log("INFO", f"Filter: {event['filter']}")
            self._append_log("INFO", str(event["command"]))
            self._update_diagnostics(str(event["command"]), str(event["filter"]))
        elif kind == "log":
            self._append_log(str(event.get("level", "INFO")), str(event["message"]))
        elif kind == "queue_finished":
            self._set_rendering_ui(False)
            self.current_job_id = None
            self.stage_var.set("Queue stopped" if event.get("stopped") else "Queue finished")
            self.current_progress.set(0)
            self.progress_var.set("0%  •  0.00×  •  ETA --:--:--")
            self._update_overall()

    def _update_row(self, job: RenderJob) -> None:
        if not self.tree.exists(job.id):
            return
        media = job.details if job.probe else (job.error or "Inspecting…")
        status = job.status.value
        if job.status in {JobStatus.RENDERING, JobStatus.REMUXING}:
            status = f"{status} {job.progress * 100:.0f}%"
        tag = "done" if job.status == JobStatus.DONE else "failed" if job.status == JobStatus.FAILED else "cancelled" if job.status == JobStatus.CANCELLED else "active" if job.status in {JobStatus.PROBING, JobStatus.RENDERING, JobStatus.REMUXING} else ""
        self.tree.item(job.id, values=(media, self.multiplier_var.get(), status), tags=(tag,) if tag else ())

    def _update_overall(self) -> None:
        jobs = [self.jobs[job_id] for job_id in self.active_job_ids if job_id in self.jobs]
        if not jobs:
            self.overall_progress.set(0)
            self.overall_var.set("Queue 0%")
            return
        weights = [job.probe.duration if job.probe else 1.0 for job in jobs]
        done = sum(weight * (1.0 if job.status in TERMINAL_STATUSES else job.progress) for job, weight in zip(jobs, weights))
        fraction = done / sum(weights)
        self.overall_progress.set(fraction)
        self.overall_var.set(f"Queue {fraction * 100:.1f}%")

    def _settings_changed(self) -> None:
        self.preset_var.set("Custom")
        for job in self.jobs.values():
            self._update_row(job)
        self._update_diagnostics()

    def _apply_preset(self, name: str) -> None:
        if name not in PRESETS:
            return
        multiplier, mixer = PRESETS[name]
        if self.capabilities and mixer not in self.capabilities.mixers:
            mixer = self.capabilities.mixers[0]
        self.multiplier_var.set(f"{multiplier}×")
        self.mixer_var.set(MIXER_LABELS[mixer])
        for job in self.jobs.values():
            self._update_row(job)
        self._update_diagnostics()

    def _qp_changed(self, value: float) -> None:
        self.qp_var.set(round(value))
        self.qp_label.configure(text=f"QP {self.qp_var.get()}")
        self._settings_changed()

    def _output_mode_changed(self) -> None:
        self._sync_output_controls()
        self._settings_changed()

    def _sync_output_controls(self) -> None:
        enabled = not self.same_output_var.get() and not (self.renderer and self.renderer.running)
        self.output_entry.configure(state="normal" if enabled else "disabled")
        self.output_browse.configure(state="normal" if enabled else "disabled")

    def _pick_output_directory(self) -> None:
        selected = filedialog.askdirectory(title="Choose output folder")
        if selected:
            self.output_dir_var.set(selected)
            self._settings_changed()

    def _change_appearance(self, value: str) -> None:
        ctk.set_appearance_mode(value)
        self.settings.appearance = value
        save_settings(self._collect_settings())

    def _device_changed(self, _value: str) -> None:
        if self.renderer and self.renderer.running:
            return
        self.capability_var.set("Checking Vulkan device…")
        self.capability_label.configure(text_color="#e7b75f")
        self.start_button.configure(state="disabled")
        self._start_capability_check()
        self._settings_changed()

    def _collect_settings(self) -> RenderSettings:
        return RenderSettings(
            multiplier=int(self.multiplier_var.get().rstrip("×")),
            performance=self.performance_var.get().lower(),
            grid=int(self.grid_var.get()),
            frame_mixer=MIXERS_BY_LABEL.get(self.mixer_var.get(), "linear"),
            qp=int(self.qp_var.get()),
            auto_mp4=self.auto_mp4_var.get(),
            keep_ts=self.keep_ts_var.get(),
            output_same_as_source=self.same_output_var.get(),
            output_directory=self.output_dir_var.get().strip(),
            appearance=self.appearance_var.get(),
            device_index=int(self.device_var.get()),
            advanced_open=self.advanced_visible,
        ).validate()

    def _toggle_advanced(self, force: bool | None = None) -> None:
        show = (not self.advanced_visible) if force is None else force
        self.advanced_visible = show
        if show:
            self.advanced_frame.grid(row=self.advanced_grid_row, column=0, sticky="ew", padx=8, pady=(0, 8))
            self.advanced_button.configure(text="Advanced ▴")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text="Advanced ▾")

    def _toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_box.grid(row=5, column=0, sticky="ew", pady=(4, 0))
            self.log_toggle.configure(text="Hide render log ▴")
        else:
            self.log_box.grid_remove()
            self.log_toggle.configure(text="Show render log ▾")

    def _append_log(self, level: str, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {level}: {message}"
        self.logger.log(logging.ERROR if level == "ERROR" else logging.WARNING if level == "WARNING" else logging.INFO, message)
        remove_first = len(self.log_lines) == self.log_lines.maxlen
        self.log_lines.append(line)
        if not hasattr(self, "log_box"):
            return
        self.log_box.configure(state="normal")
        if remove_first:
            self.log_box.delete("1.0", "2.0")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _update_diagnostics(self, exact_command: str | None = None, exact_filter: str | None = None) -> None:
        selected = self.tree.selection() if hasattr(self, "tree") else ()
        job = self.jobs.get(selected[0]) if selected else None
        settings = self._collect_settings()
        source_fps = job.probe.fps_rational if job and job.probe else "—"
        generated = f"{source_fps} × {settings.multiplier}" if job and job.probe else "—"
        active_filter = exact_filter or (filter_chain(job.probe, settings) if job and job.probe else "—")
        version = self.capabilities.version if self.capabilities else "Checking…"
        text = (
            f"FFmpeg: {self.ffmpeg or 'not found'}\n"
            f"FFprobe: {self.ffprobe or 'not found'}\n"
            f"Version: {version}\n"
            f"Source FPS: {source_fps}\n"
            f"Generated internal FPS: {generated}\n"
            f"Vulkan device: {settings.device_index}\n"
            f"Filter: {active_filter}"
        )
        if exact_command:
            text += f"\nCommand: {exact_command}"
        self.diagnostics_var.set(text)

    def _copy_command(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Copy Command", "Select a probed queue item first.")
            return
        job = self.jobs[selected[0]]
        if not job.probe or not self.ffmpeg:
            messagebox.showinfo("Copy Command", "The selected item has not been probed yet.")
            return
        settings = self._collect_settings()
        try:
            ts_path, _ = output_paths(job.input_path, job.probe, settings)
            text = command_text(build_render_command(self.ffmpeg, job.input_path, ts_path, job.probe, settings))
        except Exception as exc:
            messagebox.showerror("Copy Command", str(exc))
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.stage_var.set("Command copied to clipboard")

    def _open_selected_folder(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        job = self.jobs[selected[0]]
        self._open_path(job.output_path.parent if job.output_path else job.input_path.parent)

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Open folder", str(exc))

    def _on_close(self) -> None:
        if self.renderer and self.renderer.running:
            if not messagebox.askyesno("Exit", "Stop the active FFmpeg job and exit?"):
                return
            self._closing = True
            save_settings(self._collect_settings())
            self.renderer.stop_queue()
            deadline = time.monotonic() + 5

            def finish() -> None:
                if self.renderer and self.renderer.running and time.monotonic() < deadline:
                    self.after(100, finish)
                else:
                    self.destroy()

            finish()
            return
        save_settings(self._collect_settings())
        self.destroy()


def run() -> None:
    FRUCApp().mainloop()
