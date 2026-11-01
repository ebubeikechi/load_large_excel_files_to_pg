"""Column-selection popup (tkinter) with import + primary-key checkboxes, terminal fallback."""
from __future__ import annotations


def pick_columns(columns: list[str], console=None, sep: str = "|"):
    """Returns (selected_columns_in_header_order, primary_key_columns_in_click_order),
    or None if cancelled."""
    try:
        return _gui(columns, sep)
    except Exception:       # no display / tkinter missing -> terminal prompt
        return _terminal(columns, console)


def _gui(columns: list[str], sep: str):
    import tkinter as tk
    from tkinter import messagebox, ttk

    n = len(columns)
    root = tk.Tk()
    root.title("Excel2Postgres - choose columns and primary key")
    root.geometry("680x720")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()

    ttk.Label(root, wraplength=640, justify="left", padding=(12, 10),
              text="1) Tick the columns to create and import (unticked columns are ignored).\n"
                   "2) Next to each ticked column, tick 'Primary key' to use it as (part of) the "
                   "primary key. With several, their values are concatenated in the order you "
                   "tick them and stored in one combined key column.").pack(fill="x")

    sel = [tk.BooleanVar(value=True) for _ in columns]
    pk_vars = [tk.BooleanVar(value=(i == 0)) for i in range(n)]
    pk_order: list[int] = [0]
    master = tk.BooleanVar(value=True)
    count = ttk.Label(root, padding=(12, 0))
    pk_lbl = ttk.Label(root, padding=(12, 4), wraplength=640, justify="left",
                       font=("Segoe UI", 9, "bold"))
    pk_btns: list = []

    def refresh():
        count.config(text=f"{sum(v.get() for v in sel)} of {n} columns selected")
        for rank, i in enumerate(pk_order, 1):
            pk_btns[i].config(text=f"Primary key #{rank}")
        for i in range(n):
            if i not in pk_order:
                pk_btns[i].config(text="Primary key")
        names = [columns[i] for i in pk_order]
        if not names:
            pk_lbl.config(text="Primary key: NONE - tick 'Primary key' next to at least one column")
        elif len(names) == 1:
            pk_lbl.config(text=f"Primary key: {names[0]}")
        else:
            pk_lbl.config(text=f"Primary key (composite): column '{'_'.join(names)}'  =  "
                               f"{(' ' + sep + ' ').join(names)}")

    def on_item(i):
        if sel[i].get():
            pk_btns[i].grid()
        else:
            if i in pk_order:
                pk_order.remove(i)
            pk_vars[i].set(False)
            pk_btns[i].grid_remove()
        master.set(all(v.get() for v in sel))
        refresh()

    def on_pk(i):
        if pk_vars[i].get():
            if i not in pk_order:
                pk_order.append(i)
        elif i in pk_order:
            pk_order.remove(i)
        refresh()

    def on_master():
        val = master.get()
        for i in range(n):
            sel[i].set(val)
            if val:
                pk_btns[i].grid()
            else:
                pk_btns[i].grid_remove()
                pk_vars[i].set(False)
        if not val:
            pk_order.clear()
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
    inner.columnconfigure(0, weight=1)

    for i, name in enumerate(columns):
        ttk.Checkbutton(inner, text=f"{i + 1}. {name}", variable=sel[i],
                        command=lambda i=i: on_item(i)).grid(row=i, column=0, sticky="w", pady=1)
        b = ttk.Checkbutton(inner, text="Primary key", variable=pk_vars[i],
                            command=lambda i=i: on_pk(i))
        b.grid(row=i, column=1, sticky="w", padx=(16, 4))
        pk_btns.append(b)

    pk_lbl.pack(fill="x")
    result: dict = {"val": None}

    def ok():
        chosen = [c for c, v in zip(columns, sel) if v.get()]
        if not chosen:
            messagebox.showwarning("Columns", "Select at least one column.", parent=root)
            return
        if not pk_order:
            messagebox.showwarning("Primary key", "Tick 'Primary key' next to at least one "
                                   "selected column.", parent=root)
            return
        result["val"] = (chosen, [columns[i] for i in pk_order])
        root.destroy()

    row = ttk.Frame(root, padding=12)
    row.pack(fill="x")
    ttk.Button(row, text="Cancel", command=root.destroy).pack(side="right", padx=(8, 0))
    ttk.Button(row, text="Import selected columns", command=ok).pack(side="right")
    refresh()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result["val"]


def _parse_numbers(text: str, n: int) -> list[int]:
    out: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    seen, res = set(), []
    for i in out:
        if 1 <= i <= n and i not in seen:
            seen.add(i)
            res.append(i)
    return res


def _terminal(columns: list[str], console=None):
    n = len(columns)
    print("\nColumns found:")
    for i, c in enumerate(columns, 1):
        print(f"  {i:>3}. {c}")
    raw = input("Numbers to EXCLUDE (e.g. 3,5-8), Enter = import all, 'q' = cancel: ").strip()
    if raw.lower() == "q":
        return None
    skip = set(_parse_numbers(raw, n))
    keep = [i for i in range(1, n + 1) if i not in skip]
    raw = input("Primary key column number(s) in concatenation order (e.g. 1 or 2,5) "
                "[Enter = first kept column]: ").strip()
    pk = [i for i in _parse_numbers(raw, n) if i in keep] or keep[:1]
    if not pk:
        return None
    return [columns[i - 1] for i in keep], [columns[i - 1] for i in pk]
