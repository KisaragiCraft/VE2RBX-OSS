import datetime
import os
import re
import sys
from pathlib import Path


def setup_logger(log_path: str):
    if not log_path:
        return
    try:
        # Signature
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("Created by KisaragiKoubou\n")

        log_file = open(log_path, 'a', encoding='utf-8')

        class DualWriter:
            def __init__(self, file, original):
                self.file = file
                self.original = original

            def write(self, text):
                # original が None のケース（windowed exe 等）を許容
                try:
                    if self.original and hasattr(self.original, "write"):
                        self.original.write(text)
                except Exception:
                    pass

                try:
                    self.file.write(text)
                    self.file.flush()
                except Exception:
                    pass

            def flush(self):
                try:
                    if self.original and hasattr(self.original, "flush"):
                        self.original.flush()
                except Exception:
                    pass

                try:
                    self.file.flush()
                except Exception:
                    pass

        # None を握らない: フォールバックとして __stdout__/__stderr__ を使う
        stdout_orig = sys.stdout if sys.stdout is not None else sys.__stdout__
        stderr_orig = sys.stderr if sys.stderr is not None else sys.__stderr__

        sys.stdout = DualWriter(log_file, stdout_orig)
        sys.stderr = DualWriter(log_file, stderr_orig)

    except Exception as e:
        try:
            if sys.__stderr__ and hasattr(sys.__stderr__, "write"):
                sys.__stderr__.write(f"Failed to setup logging: {e}\n")
                sys.__stderr__.flush()
        except Exception:
            pass


def sanitize_name_local(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', '_', name)
    safe = safe.rstrip(". ")
    if not safe: safe = "Unnamed"
    return safe


def create_edit_dir_local(project_dir: Path) -> Path:
    user_docs = Path(os.path.expandvars(r"%USERPROFILE%\Documents"))
    base_root = user_docs / "VE2RBX"
    name_safe = sanitize_name_local(project_dir.name)
    pj_root = base_root / name_safe
    if not pj_root.exists(): pj_root.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now()
    timestamp = now.strftime("%Y.%m.%d.%H%M%S")
    run_name = f"{name_safe} {timestamp}"
    edit_dir = pj_root / run_name

    if edit_dir.exists():
        for i in range(1, 100):
            sf = f"_{i:02d}"
            cand = pj_root / f"{run_name}{sf}"
            if not cand.exists():
                edit_dir = cand
                break
    edit_dir.mkdir(parents=True, exist_ok=True)
    return edit_dir
