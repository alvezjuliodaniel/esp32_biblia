#!/usr/bin/env python3
"""Interfaz grafica para leer la Biblia RVR1960 alojada en la ESP32.

La ventana habla con la placa por el puerto serie igual que tools/biblia.py,
pero ofrece seleccion de puerto y velocidad, indice de libros y capitulos,
panel de lectura, buscador, marcadores y carga de datos con barra de avance.

Uso:
    python3 tools/biblia_gui.py
    python3 tools/biblia_gui.py --port /dev/ttyACM0 --baud 115200
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import biblia  # noqa: E402  (comparte Device, referencias y marcadores)

APP_NAME = "Biblia RVR1960 - ESP32"
SEARCH_LIMIT = 40
BAUD_RATES = ["115200", "230400", "460800", "921600", "57600", "38400", "19200", "9600"]
THEMES = ("claro", "sepia", "oscuro")
SERIF_FONTS = ("DejaVu Serif", "Georgia", "Liberation Serif", "Noto Serif",
               "FreeSerif", "Times New Roman", "Serif")
SANS_FONTS = ("DejaVu Sans", "Inter", "Noto Sans", "Liberation Sans", "Segoe UI",
              "FreeSans", "Helvetica")

PALETTES = {
    "claro": {
        "bg": "#fbf9f5", "panel": "#f0ebe2", "card": "#ffffff", "fg": "#22262b",
        "dim": "#8a9096", "accent": "#8a5a2b", "accent_fg": "#ffffff",
        "num": "#b0462c", "heading": "#7a4b1e", "sel": "#f4e6d0",
        "border": "#e1d9cc", "ok": "#2e7d4f", "err": "#b3261e",
    },
    "sepia": {
        "bg": "#f2e9d6", "panel": "#e7dcc3", "card": "#faf3e2", "fg": "#3c3023",
        "dim": "#857257", "accent": "#8a5a2b", "accent_fg": "#fdf7ea",
        "num": "#a0431f", "heading": "#6f4520", "sel": "#e5d3ac",
        "border": "#d7c9a9", "ok": "#3f6b45", "err": "#9c2b1e",
    },
    "oscuro": {
        "bg": "#1a1d22", "panel": "#23272e", "card": "#1f232a", "fg": "#d9dee5",
        "dim": "#868e99", "accent": "#d9a066", "accent_fg": "#1a1d22",
        "num": "#e08a5b", "heading": "#e8b87a", "sel": "#303844",
        "border": "#323942", "ok": "#6fbf8f", "err": "#e0726b",
    },
}


# ---------------------------------------------------------------------------
# Preferencias locales
# ---------------------------------------------------------------------------
def prefs_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "biblia" / "gui.json"


def load_prefs():
    path = prefs_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_prefs(data):
    path = prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hilo de trabajo: el puerto serie nunca bloquea la interfaz
# ---------------------------------------------------------------------------
class Worker:
    """Cola de una sola hebra para las operaciones de puerto serie."""

    def __init__(self, root):
        self.root = root
        self.tasks = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, task, on_ok=None, on_error=None):
        self.tasks.put((task, on_ok, on_error))

    def _loop(self):
        while True:
            task, on_ok, on_error = self.tasks.get()
            try:
                result = task()
            except Exception as exc:  # cualquier fallo vuelve a la interfaz
                if on_error is not None:
                    self.root.after(0, lambda e=exc: on_error(e))
            else:
                if on_ok is not None:
                    self.root.after(0, lambda r=result: on_ok(r))


# ---------------------------------------------------------------------------
# Ventana principal
# ---------------------------------------------------------------------------
class BibleApp(tk.Tk):
    def __init__(self, port=None, baud=biblia.DEFAULT_BAUD, theme=None, font_size=None):
        super().__init__()
        prefs = load_prefs()

        self.prefs = prefs
        self.theme_name = theme or prefs.get("theme") or "claro"
        self.font_size = int(font_size or prefs.get("font_size") or 14)
        self.baud = int(baud or prefs.get("baud") or biblia.DEFAULT_BAUD)

        self.device = None
        self.books = []
        self.info = {}
        self.book_index = -1
        self.chapter_no = 1
        self.entries = []
        self.verse_ranges = {}
        self.search_query = ""
        self.search_next = 0
        self.search_rows = []
        self.bookmarks = []
        self.busy = False
        self.syncing = False
        self.chapter_cols = 4
        self.upload_cancel = False

        self.serif = self._pick_font(SERIF_FONTS, "Serif")
        self.sans = self._pick_font(SANS_FONTS, "TkDefaultFont")

        self.title(APP_NAME)
        self.geometry(f"{prefs.get('width', 1180)}x{prefs.get('height', 780)}")
        self.minsize(920, 620)

        self.worker = Worker(self)
        self._build_style()
        self._build_menu()
        self._build_toolbar()
        # La barra de estado se empaqueta antes del cuerpo: si el cuerpo (que
        # expande) fuese primero, se comeria el espacio de la barra.
        self._build_status()
        self._build_body()
        self._bind_keys()

        self.apply_theme(self.theme_name)
        if port:
            self.port_var.set(port)
        self.refresh_ports()
        self.refresh_bookmarks()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Intento silencioso de conexion con la placa conectada.
        self.after(250, lambda: self.connect(loud=False, port_hint=port, baud_hint=baud))

    # -- fuentes ------------------------------------------------------------
    def _pick_font(self, candidates, fallback):
        try:
            available = set(tkfont.families(self))
        except tk.TclError:
            return fallback
        for name in candidates:
            if name in available:
                return name
        return fallback

    # -- estilos ------------------------------------------------------------
    def _build_style(self):
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        st = self.style
        st.configure("TFrame", borderwidth=0)
        st.configure("Toolbar.TFrame", borderwidth=0)
        st.configure("Status.TFrame", borderwidth=0)
        st.configure("Title.TLabel", font=(self.serif, 16, "bold"))
        st.configure("Sub.TLabel", font=(self.sans, 9))
        st.configure("Small.TLabel", font=(self.sans, 9))
        st.configure("Section.TLabel", font=(self.sans, 9, "bold"))
        st.configure("Nav.TButton", font=(self.sans, 11, "bold"), padding=(9, 3))
        st.configure("Chapter.TButton", font=(self.sans, 9), padding=(2, 2), width=3)
        st.configure("ChapterActive.TButton", font=(self.sans, 9, "bold"),
                     padding=(2, 2), width=3)
        st.configure("Accent.TButton", font=(self.sans, 10, "bold"), padding=(12, 5))
        st.configure("TNotebook", borderwidth=0)
        st.configure("TNotebook.Tab", font=(self.sans, 9), padding=(14, 6), borderwidth=0)
        st.configure("Treeview", rowheight=24, borderwidth=0)
        st.configure("Treeview.Heading", font=(self.sans, 9, "bold"), relief="flat")
        st.configure("TEntry", padding=4)
        st.configure("TCombobox", padding=4)
        st.configure("Horizontal.TProgressbar", borderwidth=0, thickness=6)

    def apply_theme(self, name):
        if name not in PALETTES:
            name = "claro"
        self.theme_name = name
        pal = PALETTES[name]
        self.pal = pal
        st = self.style

        self.configure(bg=pal["bg"])
        st.configure("TFrame", background=pal["bg"])
        st.configure("Toolbar.TFrame", background=pal["panel"])
        st.configure("Status.TFrame", background=pal["panel"])
        st.configure("Card.TFrame", background=pal["card"])
        st.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
        st.configure("Toolbar.TLabel", background=pal["panel"], foreground=pal["fg"])
        st.configure("Card.TLabel", background=pal["card"], foreground=pal["fg"])
        st.configure("Title.TLabel", background=pal["bg"], foreground=pal["fg"])
        st.configure("Sub.TLabel", background=pal["bg"], foreground=pal["dim"])
        st.configure("Small.TLabel", background=pal["card"], foreground=pal["dim"])
        st.configure("Section.TLabel", background=pal["card"], foreground=pal["dim"])
        st.configure("Status.TLabel", background=pal["panel"], foreground=pal["dim"])

        st.configure("TButton", background=pal["panel"], foreground=pal["fg"],
                     borderwidth=0, focuscolor=pal["bg"])
        st.map("TButton", background=[("active", pal["sel"])],
               foreground=[("disabled", pal["dim"])])
        st.configure("Nav.TButton", background=pal["panel"], foreground=pal["fg"])
        st.map("Nav.TButton", background=[("active", pal["sel"])],
               foreground=[("disabled", pal["dim"])])
        st.configure("Accent.TButton", background=pal["accent"], foreground=pal["accent_fg"])
        st.map("Accent.TButton",
               background=[("active", pal["accent"]), ("disabled", pal["border"])],
               foreground=[("disabled", pal["dim"])])
        st.configure("Chapter.TButton", background=pal["panel"], foreground=pal["fg"])
        st.map("Chapter.TButton", background=[("active", pal["sel"])])
        st.configure("ChapterActive.TButton", background=pal["accent"],
                     foreground=pal["accent_fg"])
        st.map("ChapterActive.TButton", background=[("active", pal["accent"])])

        st.configure("TNotebook", background=pal["bg"])
        st.configure("TNotebook.Tab", background=pal["panel"], foreground=pal["dim"])
        st.map("TNotebook.Tab", background=[("selected", pal["card"])],
               foreground=[("selected", pal["accent"])])
        st.configure("Treeview", background=pal["card"], fieldbackground=pal["card"],
                     foreground=pal["fg"])
        st.map("Treeview", background=[("selected", pal["sel"])],
               foreground=[("selected", pal["fg"])])
        st.configure("Treeview.Heading", background=pal["panel"], foreground=pal["dim"])
        st.configure("TEntry", fieldbackground=pal["card"], foreground=pal["fg"],
                     insertcolor=pal["fg"], bordercolor=pal["border"],
                     lightcolor=pal["border"], darkcolor=pal["border"])
        st.configure("TCombobox", fieldbackground=pal["card"], background=pal["panel"],
                     foreground=pal["fg"], arrowcolor=pal["fg"],
                     bordercolor=pal["border"], lightcolor=pal["border"],
                     darkcolor=pal["border"])
        st.map("TCombobox", fieldbackground=[("readonly", pal["card"])],
               foreground=[("readonly", pal["fg"])])
        st.configure("Horizontal.TProgressbar", background=pal["accent"],
                     troughcolor=pal["panel"], borderwidth=0)
        st.configure("TCheckbutton", background=pal["card"], foreground=pal["fg"])
        st.map("TCheckbutton", background=[("active", pal["card"])])
        st.configure("Vertical.TScrollbar", background=pal["border"],
                     troughcolor=pal["bg"], bordercolor=pal["bg"],
                     arrowcolor=pal["dim"], lightcolor=pal["border"],
                     darkcolor=pal["border"], relief="flat")
        st.configure("Horizontal.TScrollbar", background=pal["border"],
                     troughcolor=pal["bg"], bordercolor=pal["bg"],
                     arrowcolor=pal["dim"], lightcolor=pal["border"],
                     darkcolor=pal["border"], relief="flat")
        st.map("Vertical.TScrollbar", background=[("active", pal["dim"])])
        st.map("Horizontal.TScrollbar", background=[("active", pal["dim"])])
        st.configure("TSeparator", background=pal["border"])

        for option, value in (
            ("*Menu.background", pal["card"]),
            ("*Menu.foreground", pal["fg"]),
            ("*Menu.activeBackground", pal["sel"]),
            ("*Menu.activeForeground", pal["fg"]),
            ("*TCombobox*Listbox.background", pal["card"]),
            ("*TCombobox*Listbox.foreground", pal["fg"]),
            ("*TCombobox*Listbox.selectBackground", pal["accent"]),
            ("*TCombobox*Listbox.selectForeground", pal["accent_fg"]),
        ):
            self.option_add(option, value)

        self.text.configure(bg=pal["card"], fg=pal["fg"], insertbackground=pal["fg"],
                            selectbackground=pal["sel"], selectforeground=pal["fg"])
        self.info_text.configure(bg=pal["card"], fg=pal["dim"], borderwidth=0,
                                 highlightthickness=0,
                                 font=(self.sans, 9))
        self._configure_text_tags()
        for widget in (self.books_list, self.marks_list):
            widget.configure(bg=pal["card"], fg=pal["fg"],
                             selectbackground=pal["accent"],
                             selectforeground=pal["accent_fg"],
                             highlightthickness=0, borderwidth=0, activestyle="none")
        self.chapter_canvas.configure(bg=pal["card"])
        self.chapter_frame.configure(bg=pal["card"])
        self.search_tree.tag_configure("odd", background=pal["card"])
        self.search_tree.tag_configure("even", background=pal["panel"])
        self.status_dot.configure(bg=pal["panel"])
        self.theme_var.set(name)
        self._draw_dot(self.connected_color() if self.device else pal["dim"])

    def _configure_text_tags(self):
        pal = self.pal
        size = self.font_size
        self.text.tag_configure("body", font=(self.serif, size), spacing3=8,
                                lmargin1=0, lmargin2=28, justify="left",
                                foreground=pal["fg"])
        self.text.tag_configure("num", font=(self.serif, max(7, size - 5), "bold"),
                                offset=5, foreground=pal["num"])
        self.text.tag_configure("heading", font=(self.serif, size + 1, "bold"),
                                foreground=pal["heading"], justify="center",
                                spacing1=16, spacing3=10, lmargin2=0)
        self.text.tag_configure("label", font=(self.serif, max(8, size - 2), "italic"),
                                foreground=pal["dim"], justify="center",
                                spacing1=4, spacing3=8, lmargin2=0)
        self.text.tag_configure("current", background=pal["sel"])

    # -- menus --------------------------------------------------------------
    def _build_menu(self):
        self.menu_file = tk.Menu(self, tearoff=0)
        self.menu_file.add_command(label="Conectar / desconectar", accelerator="Ctrl+K",
                                   command=self.toggle_connection)
        self.menu_file.add_command(label="Enviar datos a la placa...",
                                   command=self.open_upload)
        self.menu_file.add_separator()
        self.menu_file.add_command(label="Salir", accelerator="Ctrl+Q", command=self.on_close)

        menu_go = tk.Menu(self, tearoff=0)
        menu_go.add_command(label="Capitulo anterior", accelerator="Izquierda",
                            command=lambda: self.step_chapter(-1))
        menu_go.add_command(label="Capitulo siguiente", accelerator="Derecha",
                            command=lambda: self.step_chapter(1))
        menu_go.add_separator()
        menu_go.add_command(label="Ir a referencia...", accelerator="Ctrl+G",
                            command=self.ask_reference)
        menu_go.add_command(label="Versiculo aleatorio", accelerator="Ctrl+R",
                            command=self.random_verse)
        self.menu_go = menu_go

        menu_view = tk.Menu(self, tearoff=0)
        menu_view.add_command(label="Letra mayor", accelerator="Ctrl++",
                              command=lambda: self.change_font(1))
        menu_view.add_command(label="Letra menor", accelerator="Ctrl+-",
                              command=lambda: self.change_font(-1))
        menu_view.add_separator()
        for theme in THEMES:
            menu_view.add_command(label=f"Tema {theme}",
                                  command=lambda t=theme: self.apply_theme(t))
        self.menu_view = menu_view

        menu_help = tk.Menu(self, tearoff=0)
        menu_help.add_command(label="Atajos de teclado", command=self.show_shortcuts)
        menu_help.add_command(label="Acerca de", command=self.show_about)
        self.menu_help = menu_help

        self.config(menu=self._menubar([
            ("Archivo", self.menu_file), ("Ir", menu_go),
            ("Ver", menu_view), ("Ayuda", menu_help),
        ]))

    def _menubar(self, entries):
        bar = tk.Menu(self)
        for label, menu in entries:
            bar.add_cascade(label=label, menu=menu)
        return bar

    # -- barra superior -----------------------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Toolbar.TFrame", padding=(14, 9))
        bar.pack(side="top", fill="x")

        ttk.Label(bar, text="Puerto", style="Toolbar.TLabel").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, width=16,
                                       values=self.detect_ports())
        self.port_combo.pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="Detectar", command=self.refresh_ports).pack(side="left")

        ttk.Label(bar, text="  Velocidad", style="Toolbar.TLabel").pack(side="left")
        self.baud_var = tk.StringVar(value=str(self.baud))
        ttk.Combobox(bar, textvariable=self.baud_var, width=8,
                     values=BAUD_RATES).pack(side="left", padx=(6, 8))

        self.connect_btn = ttk.Button(bar, text="Conectar", style="Accent.TButton",
                                      command=self.toggle_connection)
        self.connect_btn.pack(side="left")

        self.status_dot = tk.Canvas(bar, width=10, height=10, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(14, 5))
        self.conn_label = ttk.Label(bar, text="desconectado", style="Toolbar.TLabel")
        self.conn_label.pack(side="left")

        # extremo derecho: tema y tamano de letra
        ttk.Button(bar, text="A+", width=3,
                   command=lambda: self.change_font(1)).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="A-", width=3,
                   command=lambda: self.change_font(-1)).pack(side="right")
        self.theme_var = tk.StringVar(value=self.theme_name)
        theme_box = ttk.Combobox(bar, textvariable=self.theme_var, width=8,
                                 values=list(THEMES), state="readonly")
        theme_box.pack(side="right", padx=(8, 12))
        theme_box.bind("<<ComboboxSelected>>",
                       lambda e: self.apply_theme(self.theme_var.get()))
        ttk.Label(bar, text="Tema", style="Toolbar.TLabel").pack(side="right")

    # -- cuerpo -------------------------------------------------------------
    def _build_body(self):
        body = ttk.Frame(self, padding=(12, 6, 12, 6))
        body.pack(side="top", fill="both", expand=True)

        paned = ttk.Panedwindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, style="Card.TFrame", padding=(10, 8))
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)

        ttk.Label(left, text="LIBROS", style="Section.TLabel").pack(anchor="w")
        books_wrap = ttk.Frame(left, style="Card.TFrame")
        books_wrap.pack(fill="both", expand=True, pady=(4, 8))
        self.books_list = tk.Listbox(books_wrap, width=24, height=14, exportselection=False)
        books_scroll = ttk.Scrollbar(books_wrap, orient="vertical",
                                     command=self.books_list.yview)
        self.books_list.configure(yscrollcommand=books_scroll.set)
        self.books_list.pack(side="left", fill="both", expand=True)
        books_scroll.pack(side="right", fill="y")
        self.books_list.bind("<<ListboxSelect>>", self.on_book_selected)

        ttk.Label(left, text="CAPITULOS", style="Section.TLabel").pack(anchor="w")
        chap_wrap = ttk.Frame(left, style="Card.TFrame")
        # Solo la lista de libros expande: si ambas lo hicieran, la rejilla de
        # capitulos quedaria recortada por falta de espacio.
        chap_wrap.pack(fill="x", expand=False, pady=(4, 0))
        self.chapter_canvas = tk.Canvas(chap_wrap, width=220, height=196,
                                        highlightthickness=0, borderwidth=0)
        chap_scroll = ttk.Scrollbar(chap_wrap, orient="vertical",
                                    command=self.chapter_canvas.yview)
        self.chapter_canvas.configure(yscrollcommand=chap_scroll.set)
        self.chapter_canvas.pack(side="left", fill="both", expand=True)
        chap_scroll.pack(side="right", fill="y")
        self.chapter_frame = tk.Frame(self.chapter_canvas)
        self.chapter_window = self.chapter_canvas.create_window((0, 0), anchor="nw",
                                                               window=self.chapter_frame)
        self.chapter_frame.bind(
            "<Configure>",
            lambda e: self.chapter_canvas.configure(
                scrollregion=self.chapter_canvas.bbox("all")))
        self.chapter_canvas.bind("<Configure>", self._on_chapter_canvas_resize)
        for widget in (self.chapter_canvas, self.chapter_frame):
            widget.bind("<Button-4>", lambda e: self.chapter_canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self.chapter_canvas.yview_scroll(3, "units"))
            widget.bind("<MouseWheel>", self._on_chapter_wheel)
        self.chapter_buttons = {}

        self._build_reader(right)
        self._build_tabs(right)

    def _build_reader(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 6))

        self.title_var = tk.StringVar(value="Conecta la Biblia para comenzar")
        ttk.Label(header, textvariable=self.title_var, style="Title.TLabel").pack(side="left")

        self.next_btn = ttk.Button(header, text=">", width=3, style="Nav.TButton",
                                   command=lambda: self.step_chapter(1))
        self.next_btn.pack(side="right")
        self.prev_btn = ttk.Button(header, text="<", width=3, style="Nav.TButton",
                                   command=lambda: self.step_chapter(-1))
        self.prev_btn.pack(side="right", padx=(0, 4))
        self.random_btn = ttk.Button(header, text="Al azar", style="Nav.TButton",
                                     command=self.random_verse)
        self.random_btn.pack(side="right", padx=(0, 10))

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, wrap="word", borderwidth=0, highlightthickness=0,
                            padx=30, pady=16, cursor="arrow", state="disabled", height=10)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for widget in (self.text, wrap):
            widget.bind("<Button-4>", lambda e: self.text.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self.text.yview_scroll(3, "units"))
            widget.bind("<MouseWheel>", self._on_text_wheel)
        self.text.bind("<Button-3>", self.show_text_menu)

    def _build_tabs(self, parent):
        self.tabs = ttk.Notebook(parent)
        self.tabs.pack(fill="both", expand=False, pady=(8, 0))

        # --- busqueda
        search_tab = ttk.Frame(self.tabs, style="Card.TFrame", padding=(10, 8))
        self.tabs.add(search_tab, text="Buscar")
        row = ttk.Frame(search_tab, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Texto", style="Small.TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(row, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.search_entry.bind("<Return>", lambda e: self.run_search(reset=True))
        ttk.Button(row, text="Buscar", command=lambda: self.run_search(reset=True)).pack(side="left")
        self.more_btn = ttk.Button(row, text="Mas resultados",
                                   command=lambda: self.run_search(reset=False),
                                   state="disabled")
        self.more_btn.pack(side="left", padx=(6, 0))
        self.search_count = ttk.Label(search_tab, text="", style="Small.TLabel")
        self.search_count.pack(anchor="w", pady=(4, 2))

        tree_wrap = ttk.Frame(search_tab, style="Card.TFrame")
        self.search_tree = ttk.Treeview(tree_wrap, columns=("ref", "texto"),
                                        show="headings", selectmode="browse", height=5)
        self.search_tree.heading("ref", text="Referencia")
        self.search_tree.heading("texto", text="Versiculo")
        self.search_tree.column("ref", width=110, stretch=False, anchor="w")
        self.search_tree.column("texto", width=320, stretch=True, anchor="w")
        tree_scroll = ttk.Scrollbar(tree_wrap, orient="vertical",
                                    command=self.search_tree.yview)
        tree_xscroll = ttk.Scrollbar(search_tab, orient="horizontal",
                                     command=self.search_tree.xview)
        self.search_tree.configure(yscrollcommand=tree_scroll.set,
                                   xscrollcommand=tree_xscroll.set)
        # El desplazador horizontal va primero para que reserve su franja.
        tree_xscroll.pack(side="bottom", fill="x")
        tree_wrap.pack(fill="both", expand=True)
        self.search_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.search_tree.bind("<Double-1>", self.on_result_activate)
        self.search_tree.bind("<Return>", self.on_result_activate)

        # --- marcadores
        marks_tab = ttk.Frame(self.tabs, style="Card.TFrame", padding=(10, 8))
        self.tabs.add(marks_tab, text="Marcadores")
        marks_row = ttk.Frame(marks_tab, style="Card.TFrame")
        marks_row.pack(fill="x", pady=(0, 6))
        ttk.Button(marks_row, text="Agregar capitulo actual",
                   command=self.add_bookmark).pack(side="left")
        ttk.Button(marks_row, text="Ir", command=self.open_bookmark).pack(side="left", padx=6)
        ttk.Button(marks_row, text="Quitar", command=self.remove_bookmark).pack(side="left")
        self.marks_list = tk.Listbox(marks_tab, height=6, exportselection=False)
        self.marks_list.pack(fill="both", expand=True)
        self.marks_list.bind("<Double-1>", lambda e: self.open_bookmark())

        # --- informacion
        info_tab = ttk.Frame(self.tabs, style="Card.TFrame", padding=(10, 8))
        self.tabs.add(info_tab, text="Dispositivo")
        self.info_text = tk.Text(info_tab, height=6, wrap="none", borderwidth=0,
                                 highlightthickness=0, state="disabled")
        self.info_text.pack(fill="both", expand=True)

    def _build_status(self):
        status = ttk.Frame(self, style="Status.TFrame", padding=(14, 6))
        status.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Listo.")
        self.status_label = ttk.Label(status, textvariable=self.status_var,
                                      style="Status.TLabel")
        self.status_label.pack(side="left")
        self.spinner = ttk.Progressbar(status, mode="indeterminate", length=110)
        self.spinner.pack(side="left", padx=12)
        self.device_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.device_var, style="Status.TLabel").pack(side="right")

    def _bind_keys(self):
        self.bind("<Left>", lambda e: self._nav_key(-1))
        self.bind("<Right>", lambda e: self._nav_key(1))
        self.bind("<Prior>", lambda e: self._scroll(-12))
        self.bind("<Next>", lambda e: self._scroll(12))
        self.bind("<Up>", lambda e: self._scroll(-2))
        self.bind("<Down>", lambda e: self._scroll(2))
        self.bind("<Control-f>", lambda e: self.focus_search())
        self.bind("<Control-g>", self.ask_reference)
        self.bind("<Control-r>", lambda e: self.random_verse())
        self.bind("<Control-k>", lambda e: self.toggle_connection())
        self.bind("<Control-q>", lambda e: self.on_close())
        self.bind("<Control-plus>", lambda e: self.change_font(1))
        self.bind("<Control-equal>", lambda e: self.change_font(1))
        self.bind("<Control-minus>", lambda e: self.change_font(-1))
        self.bind("<F5>", lambda e: self.refresh_ports())

    def _typing(self):
        focus = self.focus_get()
        return isinstance(focus, (tk.Entry, ttk.Entry, ttk.Combobox))

    def _nav_key(self, delta):
        if self._typing():
            return
        self.step_chapter(delta)

    def _scroll(self, units):
        if self._typing():
            return
        self.text.yview_scroll(units, "units")

    def _on_text_wheel(self, event):
        self.text.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _on_chapter_wheel(self, event):
        self.chapter_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    # -- puertos ------------------------------------------------------------
    def detect_ports(self):
        ports = []
        if biblia.list_ports is not None:
            for item in biblia.list_ports.comports():
                ports.append(item.device)
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbserial*"):
            for path in sorted(Path("/").glob(pattern[1:])):
                if str(path) not in ports:
                    ports.append(str(path))
        return ports

    def refresh_ports(self):
        ports = self.detect_ports()
        # Las placas suelen aparecer como ACM/USB; el resto son puertos serie
        # del propio equipo y solo estorban al elegir.
        preferred = [p for p in ports if "ACM" in p or "USB" in p or "cu." in p]
        ordered = preferred + [p for p in ports if p not in preferred]
        self.port_combo.configure(values=ordered)
        saved = self.prefs.get("port")
        if not self.port_var.get():
            if saved and saved in ordered:
                self.port_var.set(saved)
            elif ordered:
                self.port_var.set(ordered[0])
        self.set_status(f"{len(ports)} puerto(s) serie disponibles." if ports
                        else "No hay puertos serie; conecta la placa por USB.")

    # -- conexion -----------------------------------------------------------
    def connected_color(self):
        return self.pal["ok"]

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 9, 9, fill=color, outline="")

    def toggle_connection(self):
        if self.device is not None:
            self.disconnect()
        else:
            self.connect(loud=True)

    def connect(self, loud=True, port_hint=None, baud_hint=None):
        if self.device is not None or self.busy:
            return
        port = (port_hint or self.port_var.get().strip() or None)
        try:
            baud = int(baud_hint or self.baud_var.get() or biblia.DEFAULT_BAUD)
        except ValueError:
            baud = biblia.DEFAULT_BAUD
        self.baud = baud
        self.baud_var.set(str(baud))
        self.set_busy(True, "Conectando con la placa...")

        def task():
            device = biblia.Device(port, baud)
            device.open()
            return device, device.books(), device.info()

        self.worker.submit(task, self._on_connected,
                           lambda exc: self._on_connect_error(exc, loud))

    def _on_connected(self, payload):
        device, books, info = payload
        self.device = device
        self.books = books
        self.info = info
        self.port_var.set(device.port)
        self.connect_btn.configure(text="Desconectar")
        self.conn_label.configure(text=f"conectado · {device.baud} baud")
        self._draw_dot(self.pal["ok"])
        self.device_var.set(
            f"{info.get('libros', len(books))} libros · "
            f"{info.get('versiculos', '?')} versiculos · {info.get('almacenamiento', '?')}")
        self._fill_books()
        self._fill_info()
        self.set_busy(False, f"Conectado en {device.port}.")
        self.prefs["port"] = device.port
        self.prefs["baud"] = device.baud

        usfm = self.prefs.get("last_usfm")
        chapter = int(self.prefs.get("last_chapter") or 1)
        index = 0
        if usfm:
            for i, book in enumerate(self.books):
                if book["usfm"] == usfm:
                    index = i
                    break
        self.goto(index, chapter)

    def _on_connect_error(self, exc, loud):
        self.set_busy(False)
        self._draw_dot(self.pal["err"])
        self.connect_btn.configure(text="Conectar")
        self.conn_label.configure(text="desconectado")
        message = str(exc)
        self.set_status(message, error=True)
        if loud:
            messagebox.showerror(APP_NAME, message)

    def disconnect(self):
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        self.connect_btn.configure(text="Conectar")
        self.conn_label.configure(text="desconectado")
        self._draw_dot(self.pal["dim"])
        self.device_var.set("")
        self.set_status("Desconectado.")

    # -- libros y capitulos -------------------------------------------------
    def _fill_books(self):
        self.books_list.delete(0, "end")
        for number, book in enumerate(self.books, start=1):
            self.books_list.insert("end", f"{number:>2}. {book['name']}")
        self.books_list.configure(height=min(len(self.books), 14))

    def _fill_info(self):
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")
        for key, value in self.info.items():
            self.info_text.insert("end", f"{key:<20}{value}\n")
        self.info_text.configure(state="disabled")

    def on_book_selected(self, event=None):
        if self.syncing or not self.books:
            return
        selection = self.books_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index == self.book_index:
            return
        self.goto(index, 1)

    def _build_chapter_buttons(self, index):
        for button in self.chapter_buttons.values():
            button.destroy()
        self.chapter_buttons = {}
        book = self.books[index]
        for chapter in range(1, book["chapters"] + 1):
            button = ttk.Button(self.chapter_frame, text=str(chapter),
                                style="Chapter.TButton", width=3,
                                command=lambda c=chapter, i=index: self.goto(i, c))
            self.chapter_buttons[chapter] = button
        self._layout_chapter_buttons()
        for column in range(8):
            weight = 1 if column < self.chapter_cols else 0
            self.chapter_frame.columnconfigure(column, weight=weight)

    def _layout_chapter_buttons(self):
        for position, chapter in enumerate(sorted(self.chapter_buttons)):
            button = self.chapter_buttons[chapter]
            button.grid(row=position // self.chapter_cols,
                        column=position % self.chapter_cols,
                        padx=1, pady=1, sticky="ew")

    def _on_chapter_canvas_resize(self, event):
        columns = max(3, (event.width - 10) // 46)
        if columns != self.chapter_cols:
            self.chapter_cols = columns
            for column in range(8):
                self.chapter_frame.columnconfigure(
                    column, weight=1 if column < columns else 0)
            self._layout_chapter_buttons()
        self.chapter_canvas.itemconfigure(self.chapter_window, width=event.width)

    def _mark_active_chapter(self):
        for chapter, button in self.chapter_buttons.items():
            style = "ChapterActive.TButton" if chapter == self.chapter_no else "Chapter.TButton"
            button.configure(style=style)
        button = self.chapter_buttons.get(self.chapter_no)
        if button is not None:
            self.chapter_canvas.update_idletasks()
            top = button.winfo_y()
            total = max(1, self.chapter_frame.winfo_height())
            self.chapter_canvas.yview_moveto(max(0.0, (top - 40) / total))

    # -- navegacion ---------------------------------------------------------
    def goto(self, index, chapter, verse=None):
        if self.device is None or not self.books:
            self.set_status("Conecta la placa para leer.", error=True)
            return
        if self.busy:
            return
        book = self.books[index]
        chapter = max(1, min(int(chapter), book["chapters"]))
        self.set_busy(True, f"Leyendo {book['short']} {chapter}...")
        self.worker.submit(
            lambda: self.device.chapter(book, chapter),
            lambda entries: self._chapter_ready(index, chapter, entries, verse),
            self._on_error)

    def _chapter_ready(self, index, chapter, entries, verse=None):
        book = self.books[index]
        rebuilt = index != self.book_index
        self.book_index = index
        self.chapter_no = chapter
        self.entries = entries
        if rebuilt or not self.chapter_buttons:
            self._build_chapter_buttons(index)

        self.syncing = True
        self.books_list.selection_clear(0, "end")
        self.books_list.selection_set(index)
        self.books_list.see(index)
        self.syncing = False

        self._mark_active_chapter()
        self.render_chapter(book, chapter, entries, verse)
        self.set_busy(False)
        self.prefs["last_usfm"] = book["usfm"]
        self.prefs["last_chapter"] = chapter
        self.set_status(f"{book['name']} {chapter} · {sum(1 for e in entries if e[0] == 'v')} versiculos")

    def render_chapter(self, book, chapter, entries, focus_verse=None):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.verse_ranges = {}
        for kind, number, body in entries:
            if kind == "h":
                self.text.insert("end", body + "\n", "heading")
            elif kind == "~":
                self.text.insert("end", body + "\n", "label")
            else:
                start = self.text.index("end-1c")
                self.text.insert("end", str(number), "num")
                self.text.insert("end", "  " + body + "\n", "body")
                self.verse_ranges[number] = (start, self.text.index("end-1c"))
        self.text.configure(state="disabled")
        self.text.tag_remove("current", "1.0", "end")
        self.title_var.set(f"{book['name']} {chapter}")

        if focus_verse in self.verse_ranges:
            start, end = self.verse_ranges[focus_verse]
            self.text.tag_add("current", start, end)
            self.after(30, lambda s=start: self.text.see(s))
        else:
            self.text.yview_moveto(0)

    def step_chapter(self, delta):
        if self.device is None or not self.books or self.busy:
            return
        index, chapter = self.book_index, self.chapter_no + delta
        if chapter < 1:
            if index == 0:
                chapter = 1
            else:
                index -= 1
                chapter = self.books[index]["chapters"]
        elif chapter > self.books[index]["chapters"]:
            if index == len(self.books) - 1:
                chapter = self.books[index]["chapters"]
            else:
                index += 1
                chapter = 1
        self.goto(index, chapter)

    def ask_reference(self, event=None):
        if self.device is None:
            self.set_status("Conecta la placa para leer.", error=True)
            return
        dialog = tk.Toplevel(self)
        dialog.title("Ir a referencia")
        dialog.configure(bg=self.pal["card"])
        dialog.transient(self)
        frame = ttk.Frame(dialog, style="Card.TFrame", padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Escribe por ejemplo: juan 3:16, sal 119, 1 corintios 13",
                  style="Small.TLabel").pack(anchor="w")
        value = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=value, width=34)
        entry.pack(fill="x", pady=(8, 10))
        entry.focus_set()

        def accept(event=None):
            text = value.get().strip()
            reference = biblia.parse_reference(text, self.books)
            if reference is None and self.book_index >= 0:
                reference = biblia.parse_local_reference(text, self.books[self.book_index])
            if reference is None:
                self.set_status(f"No entiendo la referencia «{text}».", error=True)
                return
            index = self.books.index(reference.book)
            dialog.destroy()
            self.goto(index, reference.chapter, reference.verse)

        entry.bind("<Return>", accept)
        ttk.Button(frame, text="Ir", style="Accent.TButton", command=accept).pack(anchor="e")
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + 120
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    def random_verse(self):
        if self.device is None or self.busy:
            return
        self.set_busy(True, "Buscando un versiculo al azar...")
        self.worker.submit(self.device.random, self._on_random, self._on_error)

    def _on_random(self, payload):
        ref_label, number, _text = payload
        reference = biblia.parse_reference(ref_label, self.books)
        self.set_busy(False)
        if reference is None:
            self.set_status(f"Referencia inesperada: {ref_label}", error=True)
            return
        index = self.books.index(reference.book)
        self.goto(index, reference.chapter, number)

    # -- busqueda -----------------------------------------------------------
    def focus_search(self):
        self.tabs.select(0)
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def run_search(self, reset=True):
        if self.device is None or self.busy:
            return
        query = self.search_var.get().strip()
        if not query:
            self.set_status("Escribe algo para buscar.", error=True)
            return
        if reset:
            self.search_query = query
            self.search_next = 0
            self.search_rows = []
            self.search_tree.delete(*self.search_tree.get_children())
        elif self.search_query != query:
            self.search_query = query
            self.search_next = 0
            self.search_rows = []
            self.search_tree.delete(*self.search_tree.get_children())

        start = self.search_next
        self.set_busy(True, f"Buscando «{query}» desde el capitulo {start}...")
        self.worker.submit(
            lambda: self.device.search(query, start=start, limit=SEARCH_LIMIT),
            self._on_search, self._on_error)

    def _on_search(self, payload):
        matches, next_chapter = payload
        self.search_rows.extend(matches)
        for row, (ref, text) in enumerate(matches):
            self.search_tree.insert("", "end", values=(ref, text),
                                    tags=("even" if row % 2 else "odd",))
        total = len(self.search_rows)
        self.search_next = next_chapter
        has_more = len(matches) >= SEARCH_LIMIT and next_chapter > 0
        self.more_btn.configure(state="normal" if has_more else "disabled")
        self.set_busy(False)
        if total == 0:
            self.search_count.configure(text="Sin resultados.")
            self.set_status("Sin resultados.", error=True)
        else:
            suffix = " (hay mas: pulsa «Mas resultados»)" if has_more else ""
            self.search_count.configure(text=f"{total} coincidencia(s){suffix}")
            self.set_status(f"{total} coincidencia(s) de «{self.search_query}».")

    def on_result_activate(self, event=None):
        selection = self.search_tree.selection()
        if not selection:
            return
        ref = self.search_tree.item(selection[0], "values")[0]
        reference = biblia.parse_reference(ref, self.books)
        if reference is None:
            return
        index = self.books.index(reference.book)
        self.goto(index, reference.chapter, reference.verse)

    # -- marcadores ---------------------------------------------------------
    def refresh_bookmarks(self):
        self.bookmarks = biblia.load_bookmarks()
        self.marks_list.delete(0, "end")
        for item in self.bookmarks:
            added = item.get("added", "")
            self.marks_list.insert("end", f"{item['ref']:<14} {added}")

    def add_bookmark(self):
        if self.book_index < 0:
            self.set_status("Abre un capitulo antes de marcarlo.", error=True)
            return
        book = self.books[self.book_index]
        label = f"{book['short']} {self.chapter_no}"
        for item in self.bookmarks:
            if item["ref"] == label:
                self.set_status(f"{label} ya estaba en los marcadores.")
                return
        self.bookmarks.append({"ref": label, "usfm": book["usfm"],
                               "chapter": self.chapter_no,
                               "added": time.strftime("%Y-%m-%d %H:%M")})
        biblia.save_bookmarks(self.bookmarks)
        self.refresh_bookmarks()
        self.set_status(f"Marcador guardado: {label}")

    def open_bookmark(self):
        selection = self.marks_list.curselection()
        if not selection or self.device is None:
            return
        item = self.bookmarks[selection[0]]
        for index, book in enumerate(self.books):
            if book["usfm"] == item.get("usfm"):
                self.goto(index, int(item.get("chapter", 1)))
                return
        self.set_status(f"No encuentro {item['ref']} en el dispositivo.", error=True)

    def remove_bookmark(self):
        selection = self.marks_list.curselection()
        if not selection:
            return
        item = self.bookmarks.pop(selection[0])
        biblia.save_bookmarks(self.bookmarks)
        self.refresh_bookmarks()
        self.set_status(f"Marcador borrado: {item['ref']}")

    # -- copiar / menu contextual ------------------------------------------
    def _verse_at(self, index):
        for number, (start, end) in self.verse_ranges.items():
            if self.text.compare(start, "<=", index) and self.text.compare(index, "<", end):
                return number
        return None

    def show_text_menu(self, event):
        index = self.text.index(f"@{event.x},{event.y}")
        number = self._verse_at(index)
        menu = tk.Menu(self, tearoff=0)
        if number is not None:
            menu.add_command(label=f"Copiar versiculo {number}",
                             command=lambda n=number: self.copy_verse(n))
        menu.add_command(label="Copiar capitulo", command=self.copy_chapter)
        menu.add_separator()
        menu.add_command(label="Ir a referencia...", command=self.ask_reference)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def copy_verse(self, number):
        for kind, verse_number, body in self.entries:
            if kind == "v" and verse_number == number:
                self.clipboard_clear()
                self.clipboard_append(body)
                self.set_status(f"Versiculo {number} copiado.")
                return

    def copy_chapter(self):
        lines = [f"{number} {body}" for kind, number, body in self.entries if kind == "v"]
        self.clipboard_clear()
        self.clipboard_append(self.title_var.get() + "\n" + "\n".join(lines))
        self.set_status("Capitulo copiado al portapapeles.")

    # -- carga de datos -----------------------------------------------------
    def open_upload(self):
        if self.device is None:
            messagebox.showinfo(APP_NAME, "Conecta primero con la placa.")
            return
        if self.busy:
            return
        initial = biblia.DEFAULT_DATA
        path = filedialog.askopenfilename(
            title="Elegir el archivo bible.dat del dispositivo",
            initialdir=str(initial.parent),
            filetypes=[("Datos de la Biblia", "*.dat"), ("Todos los archivos", "*")])
        if not path:
            return

        dialog = tk.Toplevel(self)
        dialog.title("Enviar datos a la placa")
        dialog.configure(bg=self.pal["card"])
        dialog.transient(self)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, style="Card.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=os.path.basename(path), style="Card.TLabel",
                  font=(self.sans, 11, "bold")).pack(anchor="w")
        note = ttk.Label(frame, text="La placa no responde mientras se cargan los datos.",
                         style="Small.TLabel")
        note.pack(anchor="w", pady=(2, 10))
        bar = ttk.Progressbar(frame, mode="determinate", maximum=100, length=360)
        bar.pack(fill="x")
        detail = ttk.Label(frame, text="0 %", style="Small.TLabel")
        detail.pack(anchor="w", pady=(6, 10))
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.pack(fill="x")
        cancel_btn = ttk.Button(buttons, text="Cancelar")
        cancel_btn.pack(side="right")

        self.upload_cancel = False
        started = time.time()
        last_percent = [-1]

        def cancel():
            self.upload_cancel = True
            cancel_btn.configure(state="disabled")
            detail.configure(text="Cancelando...")

        cancel_btn.configure(command=cancel)

        def progress(sent, total):
            if self.upload_cancel:
                raise biblia.DeviceError("carga cancelada")
            percent = int(sent * 100 / total) if total else 100
            # Un aviso por cada punto porcentual: el envio hace miles de
            # bloques y no conviene inundar la cola de eventos de tkinter.
            if percent == last_percent[0]:
                return
            last_percent[0] = percent
            elapsed = max(0.001, time.time() - started)
            speed = sent / elapsed / 1024
            text = f"{percent} % · {sent / 1048576:.2f} MB · {speed:.0f} KB/s"
            self.after(0, lambda: (bar.configure(value=percent),
                                   detail.configure(text=text)))

        def finished(sent):
            dialog.destroy()
            self.set_busy(False, "Datos cargados. Releyendo el indice...")
            self.refresh_after_upload()

        def failed(exc):
            dialog.destroy()
            self.set_busy(False)
            if "cancelada" in str(exc):
                self.set_status("Carga cancelada por el usuario.", error=True)
                return
            messagebox.showerror(APP_NAME, f"No se pudieron enviar los datos:\n{exc}")

        self.set_busy(True, "Enviando datos a la placa...")
        self.worker.submit(
            lambda: self.device.upload(path, progress=False, on_progress=progress),
            finished, failed)

    def refresh_after_upload(self):
        if self.device is None:
            return

        def task():
            return self.device.books(), self.device.info()

        self.worker.submit(task, self._on_reloaded, self._on_error)

    def _on_reloaded(self, payload):
        books, info = payload
        self.books = books
        self.info = info
        self._fill_books()
        self._fill_info()
        self.device_var.set(
            f"{info.get('libros', len(books))} libros · "
            f"{info.get('versiculos', '?')} versiculos · {info.get('almacenamiento', '?')}")
        self.set_busy(False, "Datos actualizados en la placa.")
        current = self.book_index if 0 <= self.book_index < len(self.books) else 0
        self.goto(current, 1)

    # -- estado -------------------------------------------------------------
    def set_status(self, message, error=False):
        self.status_var.set(message)
        self.status_label.configure(foreground=self.pal["err"] if error else self.pal["dim"])

    def set_busy(self, flag, message=None):
        self.busy = flag
        if message is not None:
            self.status_var.set(message)
        if flag:
            self.spinner.start(12)
            self.connect_btn.state(["disabled"])
        else:
            self.spinner.stop()
            self.connect_btn.state(["!disabled"])
        state = "disabled" if flag else "normal"
        for widget in (self.prev_btn, self.next_btn, self.random_btn, self.more_btn):
            widget.state(["disabled"] if flag else ["!disabled"])

    def _on_error(self, exc):
        self.set_busy(False)
        message = str(exc)
        self.set_status(message, error=True)
        if isinstance(exc, biblia.DeviceError) and "no tiene datos" in message:
            messagebox.showinfo(
                APP_NAME,
                "La placa no tiene la Biblia cargada.\n\n"
                "Usa Archivo > Enviar datos a la placa... o\n"
                "python3 tools/biblia.py upload")

    # -- letra y ayuda ------------------------------------------------------
    def change_font(self, delta):
        self.font_size = max(9, min(30, self.font_size + delta))
        self._configure_text_tags()
        if self.entries:
            self.render_chapter(self.books[self.book_index], self.chapter_no,
                                self.entries)
        self.set_status(f"Tamano de letra: {self.font_size}")

    def show_shortcuts(self):
        messagebox.showinfo(
            "Atajos",
            "Izquierda / derecha   capitulo anterior / siguiente\n"
            "Arriba / abajo        desplazar el texto\n"
            "RePag / AvPag         pagina anterior / siguiente\n"
            "Ctrl+F                buscar\n"
            "Ctrl+G                ir a una referencia\n"
            "Ctrl+R                versiculo al azar\n"
            "Ctrl+K                conectar o desconectar\n"
            "Ctrl++ / Ctrl+-       tamano de letra\n"
            "F5                    detectar puertos\n"
            "Ctrl+Q                salir\n\n"
            "Clic derecho sobre el texto: copiar versiculo o capitulo.")

    def show_about(self):
        messagebox.showinfo(
            APP_NAME,
            "Lector grafico de la Biblia Reina-Valera 1960 alojada en una ESP32.\n\n"
            "El texto viaja comprimido (zlib) y se descomprime en la placa;\n"
            "esta ventana solo envia comandos de texto por el puerto serie.\n\n"
            "Comparte protocolo y marcadores con tools/biblia.py.")

    # -- cierre -------------------------------------------------------------
    def on_close(self):
        try:
            size = {}
            # Si la ventana nunca llego a mostrarse, winfo_width vale 1 y no
            # conviene guardar ese tamano para la proxima vez.
            if self.winfo_width() > 200 and self.winfo_height() > 200:
                size = {"width": self.winfo_width(), "height": self.winfo_height()}
            self.prefs.update({
                "theme": self.theme_name,
                "font_size": self.font_size,
                "port": self.port_var.get(),
                "baud": self.baud,
                **size,
            })
            save_prefs(self.prefs)
        finally:
            if self.device is not None:
                try:
                    self.device.close()
                except Exception:
                    pass
            self.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lector grafico de la Biblia en la ESP32")
    parser.add_argument("--port", help="puerto serie (por defecto, autodeteccion)")
    parser.add_argument("--baud", type=int, default=None,
                        help="velocidad del puerto (por defecto 115200 o la ultima usada)")
    parser.add_argument("--theme", choices=THEMES, help="tema de colores")
    parser.add_argument("--font-size", type=int, help="tamano de la letra de lectura")
    args = parser.parse_args(argv)

    try:
        app = BibleApp(port=args.port, baud=args.baud, theme=args.theme,
                       font_size=args.font_size)
    except tk.TclError as exc:
        print(f"No se pudo abrir la interfaz grafica: {exc}", file=sys.stderr)
        print("Necesitas un entorno grafico (DISPLAY) y el paquete python3-tk.",
              file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
