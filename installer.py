"""
Windows Setup Wizard for Smart IPTV Player Pro.
Extracts app_payload.zip, creates Desktop & Start Menu shortcuts, writes uninstaller,
and registers Add/Remove Programs metadata in HKCU.
"""

import os
import sys
import subprocess
import threading
import zipfile
import winreg
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


APP_NAME = "Smart IPTV Player Pro"
APP_VERSION = "2.0.0"
PUBLISHER = "Smart IPTV Pro"
REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SmartIPTVPlayer"


def get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def verify_payload_cli() -> int:
    res_dir = get_resource_dir()
    payload_zip = os.path.join(res_dir, "app_payload.zip")
    if not os.path.exists(payload_zip):
        print(f"MISSING_PAYLOAD: {payload_zip}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(payload_zip, "r") as zf:
        count = len(zf.namelist())
    print(f"PAYLOAD_OK {count} files")
    return 0


class SetupWizardApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.res_dir = get_resource_dir()
        self.payload_zip = os.path.join(self.res_dir, "app_payload.zip")
        self.icon_path = os.path.join(self.res_dir, "app_icon.ico")

        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SmartIPTVPlayer.Pro.Setup.1")
            except Exception:
                pass

        self.title(f"{APP_NAME} — Setup")
        self.configure(bg="#0b1120")
        self.resizable(False, False)

        width, height = 540, 410
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 2}")

        if os.path.exists(self.icon_path):
            try:
                self.iconbitmap(self.icon_path)
            except Exception:
                pass

        default_base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        self.default_dir = os.path.join(default_base, "Programs", "Smart IPTV Player")
        self.dir_var = tk.StringVar(value=self.default_dir)
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_startmenu = tk.BooleanVar(value=True)
        self.var_launch = tk.BooleanVar(value=True)
        self.installed_exe = ""
        self.installing = False

        self._build_ui()

    def _build_ui(self):
        # Top Banner
        header = tk.Frame(self, bg="#111a2e", height=78)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Frame(self, bg="#00d2c4", height=2).pack(fill="x")

        tk.Label(
            header,
            text=f"📺  {APP_NAME}",
            bg="#111a2e",
            fg="#00e5d4",
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        ).pack(fill="x", padx=22, pady=(14, 2))

        tk.Label(
            header,
            text=f"Windows Installer  •  Version {APP_VERSION} (Includes VLC & FFmpeg)",
            bg="#111a2e",
            fg="#94a3b8",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", padx=22)

        body = tk.Frame(self, bg="#0b1120")
        body.pack(fill="both", expand=True, padx=22, pady=16)

        # Install Folder
        tk.Label(
            body,
            text="Destination Folder:",
            bg="#0b1120",
            fg="#e2e8f0",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        dir_row = tk.Frame(body, bg="#0b1120")
        dir_row.pack(fill="x", pady=(0, 14))

        self.dir_entry = tk.Entry(
            dir_row,
            textvariable=self.dir_var,
            bg="#162033",
            fg="#f8fafc",
            insertbackground="#00e5d4",
            relief="flat",
            highlightbackground="#28354f",
            highlightthickness=1,
            font=("Segoe UI", 9),
        )
        self.dir_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))

        self.btn_browse = tk.Button(
            dir_row,
            text="Browse...",
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#334155",
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=14,
            pady=5,
            cursor="hand2",
            command=self._browse_dir,
        )
        self.btn_browse.pack(side="right")

        # Options
        tk.Label(
            body,
            text="Installation Options:",
            bg="#0b1120",
            fg="#e2e8f0",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        cb_opts = dict(
            bg="#0b1120",
            fg="#cbd5e1",
            selectcolor="#162033",
            activebackground="#0b1120",
            activeforeground="#ffffff",
            font=("Segoe UI", 9),
            anchor="w",
            cursor="hand2",
        )
        self.chk_desktop = tk.Checkbutton(body, text="Create Desktop shortcut", variable=self.var_desktop, **cb_opts)
        self.chk_desktop.pack(fill="x", pady=2)

        self.chk_start = tk.Checkbutton(body, text="Create Start Menu shortcut", variable=self.var_startmenu, **cb_opts)
        self.chk_start.pack(fill="x", pady=2)

        self.chk_launch = tk.Checkbutton(body, text=f"Launch {APP_NAME} after installation", variable=self.var_launch, **cb_opts)
        self.chk_launch.pack(fill="x", pady=2)

        # Progress
        self.lbl_status = tk.Label(
            body,
            text="Ready to install.",
            bg="#0b1120",
            fg="#38bdf8",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.lbl_status.pack(fill="x", pady=(16, 6))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Teal.Horizontal.TProgressbar",
            troughcolor="#162033",
            background="#00d2c4",
            bordercolor="#162033",
            lightcolor="#00d2c4",
            darkcolor="#00adb5",
        )
        self.progress = ttk.Progressbar(
            body,
            style="Teal.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
        )
        self.progress.pack(fill="x")

        # Bottom Action Row
        footer = tk.Frame(self, bg="#111a2e", height=58)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        self.btn_cancel = tk.Button(
            footer,
            text="Cancel",
            bg="#1e293b",
            fg="#e2e8f0",
            activebackground="#334155",
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            width=12,
            pady=6,
            cursor="hand2",
            command=self._on_cancel,
        )
        self.btn_cancel.pack(side="right", padx=(6, 20), pady=12)

        self.btn_install = tk.Button(
            footer,
            text="Install",
            bg="#00adb5",
            fg="#ffffff",
            activebackground="#008f96",
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            width=16,
            pady=6,
            cursor="hand2",
            command=self._start_install,
        )
        self.btn_install.pack(side="right", padx=6, pady=12)

    def _browse_dir(self):
        chosen = filedialog.askdirectory(title="Select Installation Folder", initialdir=self.dir_var.get())
        if chosen:
            if os.path.basename(os.path.normpath(chosen)).lower() != "smart iptv player":
                chosen = os.path.join(chosen, "Smart IPTV Player")
            self.dir_var.set(chosen)

    def _on_cancel(self):
        if self.installing:
            return
        self.destroy()

    def _set_status(self, text: str, pct: float = None):
        self.after(0, lambda: self._apply_status(text, pct))

    def _apply_status(self, text: str, pct: float = None):
        self.lbl_status.configure(text=text)
        if pct is not None:
            self.progress["value"] = pct

    def _start_install(self):
        target_dir = self.dir_var.get().strip()
        if not target_dir:
            messagebox.showwarning("Invalid Path", "Please choose an installation folder.", parent=self)
            return
        if not os.path.exists(self.payload_zip):
            messagebox.showerror("Missing Payload", f"Installer payload not found:\n{self.payload_zip}", parent=self)
            return

        self.installing = True
        self.btn_install.configure(state="disabled")
        self.btn_browse.configure(state="disabled")
        self.btn_cancel.configure(state="disabled")
        self.dir_entry.configure(state="disabled")

        threading.Thread(target=self._install_worker, args=(target_dir,), daemon=True).start()

    def _install_worker(self, target_dir: str):
        try:
            self._set_status("Preparing installation directory...", 2)
            os.makedirs(target_dir, exist_ok=True)

            with zipfile.ZipFile(self.payload_zip, "r") as zf:
                members = zf.infolist()
                total = max(1, len(members))
                for idx, member in enumerate(members, start=1):
                    zf.extract(member, target_dir)
                    if idx % 15 == 0 or idx == total:
                        pct = 5 + (idx / total) * 80
                        self._set_status(f"Extracting files ({idx}/{total})...", pct)

            exe_path = os.path.join(target_dir, "SmartIPTVPlayer.exe")
            ico_path = os.path.join(target_dir, "app_icon.ico")
            if not os.path.exists(ico_path) and os.path.exists(self.icon_path):
                with open(self.icon_path, "rb") as src_f, open(ico_path, "wb") as dst_f:
                    dst_f.write(src_f.read())

            self._set_status("Creating uninstaller...", 88)
            uninstall_bat = os.path.join(target_dir, "uninstall.bat")
            desktop_lnk = os.path.join(os.path.expanduser("~"), "Desktop", f"{APP_NAME}.lnk")
            startmenu_dir = os.path.join(
                os.environ.get("APPDATA") or os.path.expanduser("~"),
                "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME
            )

            bat_content = (
                "@echo off\n"
                "taskkill /f /im SmartIPTVPlayer.exe >nul 2>&1\n"
                f'if exist "{desktop_lnk}" del /f /q "{desktop_lnk}"\n'
                f'if exist "{startmenu_dir}" rmdir /s /q "{startmenu_dir}"\n'
                f'reg delete "HKCU\\{REG_SUBKEY}" /f >nul 2>&1\n'
                'start "" cmd /c "timeout /t 2 >nul & rmdir /s /q ""%~dp0"""\n'
            )
            with open(uninstall_bat, "w", encoding="utf-8") as f:
                f.write(bat_content)

            self._set_status("Creating shortcuts...", 92)
            self._create_shortcuts(
                exe_path=exe_path,
                ico_path=ico_path,
                work_dir=target_dir,
                uninstall_bat=uninstall_bat,
                desktop_lnk=desktop_lnk,
                startmenu_dir=startmenu_dir,
                make_desktop=self.var_desktop.get(),
                make_start=self.var_startmenu.get(),
            )

            self._set_status("Registering application...", 96)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
                winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, target_dir)
                winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, ico_path if os.path.exists(ico_path) else exe_path)
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_bat}"')
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)

            self.installed_exe = exe_path
            self.after(0, self._on_install_complete)
        except Exception as exc:
            self.after(0, lambda err=str(exc): self._on_install_error(err))

    @staticmethod
    def _create_shortcuts(
        exe_path: str,
        ico_path: str,
        work_dir: str,
        uninstall_bat: str,
        desktop_lnk: str,
        startmenu_dir: str,
        make_desktop: bool,
        make_start: bool,
    ):
        cmds = ["$ws = New-Object -ComObject WScript.Shell"]
        icon_ref = ico_path if os.path.exists(ico_path) else exe_path

        def ps_esc(s: str) -> str:
            return s.replace("'", "''")

        if make_desktop:
            cmds.append(
                f"$s = $ws.CreateShortcut('{ps_esc(desktop_lnk)}'); "
                f"$s.TargetPath = '{ps_esc(exe_path)}'; "
                f"$s.WorkingDirectory = '{ps_esc(work_dir)}'; "
                f"$s.IconLocation = '{ps_esc(icon_ref)}'; "
                "$s.Save()"
            )

        if make_start:
            os.makedirs(startmenu_dir, exist_ok=True)
            app_lnk = os.path.join(startmenu_dir, f"{APP_NAME}.lnk")
            uninst_lnk = os.path.join(startmenu_dir, f"Uninstall {APP_NAME}.lnk")
            cmds.append(
                f"$s = $ws.CreateShortcut('{ps_esc(app_lnk)}'); "
                f"$s.TargetPath = '{ps_esc(exe_path)}'; "
                f"$s.WorkingDirectory = '{ps_esc(work_dir)}'; "
                f"$s.IconLocation = '{ps_esc(icon_ref)}'; "
                "$s.Save()"
            )
            cmds.append(
                f"$u = $ws.CreateShortcut('{ps_esc(uninst_lnk)}'); "
                f"$u.TargetPath = '{ps_esc(uninstall_bat)}'; "
                f"$u.WorkingDirectory = '{ps_esc(work_dir)}'; "
                "$u.Save()"
            )

        if len(cmds) > 1:
            cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "; ".join(cmds)],
                creationflags=cflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def _on_install_complete(self):
        self.installing = False
        self._apply_status("Installation completed successfully!", 100)
        self.btn_install.configure(text="Finish", state="normal", command=self._on_finish)

    def _on_install_error(self, err: str):
        self.installing = False
        self._apply_status("Installation failed.", 0)
        self.btn_install.configure(state="normal")
        self.btn_browse.configure(state="normal")
        self.btn_cancel.configure(state="normal")
        self.dir_entry.configure(state="normal")
        messagebox.showerror("Setup Error", f"Installation failed:\n{err}", parent=self)

    def _on_finish(self):
        if self.var_launch.get() and self.installed_exe and os.path.exists(self.installed_exe):
            try:
                subprocess.Popen([self.installed_exe], cwd=os.path.dirname(self.installed_exe))
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    if "--verify-payload" in sys.argv:
        sys.exit(verify_payload_cli())
    app = SetupWizardApp()
    app.mainloop()
