"""Column-selection popup (tkinter checkboxes) with a terminal fallback."""
from __future__ import annotations


def pick_columns(columns: list[str], console=None) -> list[str] | None:
    """Returns the chosen column names (header order), or None if cancelled.
    The first column is the primary key and is always included."""
    try:
        return _gui(columns)
    except Exception:       # no display / tkinter missing -> terminal prompt
        return _terminal(columns, console)


def _gui(columns: list[str]) -> list[str] | None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Excel2Postgres - choose columns to import")
    root.geometry("560x680")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()

    ttk.Label(root, wraplength=520, justify="left", padding=(12, 10),
              text="Tick the columns to create and import. Unticked columns are ignored "
                   "(not created in PostgreSQL, not read). The first column is the primary "
                   "key and is always included.").pack(fill="x")

    vars_ = [tk.BooleanVar(value=True) for _ in columns]
    master = tk.BooleanVar(value=True)
    count = ttk.Label(root, padding=(12, 0))

    def refresh():
        count.config(text=f"{sum(v.get() for v in vars_)} of {len(columns)} columns selected")

    def on_master():
        val = master.get()
        for i, v in enumerate(vars_):
            v.set(True if i == 0 else val)
        refresh()

    def on_item():
        master.set(all(v.get() for v in vars_))
        refresh()

    ttk.Checkbutton(root, text="Select / unselect ALL columns", variable=master,
                    command=on_master).pack(anchor="w", padx=12, pady=(0, 4))
    count.pack(anchor="w")

    box = ttk.Frame(root)
    box.pack(fill="both", expand=True, padx=12, pady=8)
    canvas = tk.Canvas(box, highlightthickness=0)
    bar = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

    for i, (name, var) in enumerate(zip(columns, vars_)):
        label = f"{i + 1}. {name}" + ("   (primary key - always included)" if i == 0 else "")
        cb = ttk.Checkbutton(inner, text=label, variable=var, command=on_item)
        cb.grid(row=i, column=0, sticky="w", pady=1)
        if i == 0:
            cb.state(["disabled"])

    result: dict = {"cols": None}

    def ok():
        result["cols"] = [c for c, v in zip(columns, vars_) if v.get()]
        root.destroy()

    row = ttk.Frame(root, padding=12)
    row.pack(fill="x")
    ttk.Button(row, text="Cancel", command=root.destroy).pack(side="right", padx=(8, 0))
    ttk.Button(row, text="Import selected columns", command=ok).pack(side="right")
    refresh()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result["cols"]


def _parse_numbers(text: str, n: int) -> set[int]:
    out: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return {i for i in out if 1 <= i <= n}


def _terminal(columns: list[str], console=None) -> list[str] | None:
    say = console.print if console else print
    say("\nColumns found (the first column is the primary key and is always imported):")
    for i, c in enumerate(columns, 1):
        say(f"  {i:>3}. {c}", markup=False) if console else say(f"  {i:>3}. {c}")
    raw = input("Numbers to EXCLUDE (e.g. 3,5-8), Enter = import all, 'q' = cancel: ").strip()
    if raw.lower() == "q":
        return None
    skip = _parse_numbers(raw, len(columns)) - {1}
    return [c for i, c in enumerate(columns, 1) if i not in skip]
