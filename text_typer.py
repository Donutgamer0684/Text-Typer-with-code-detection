"""
Text Typer - paste text, then have it typed into any other app with global hotkeys.

Install:  pip install pynput pygments
          (pygments provides the syntax colors; without it the app still works,
           just in a single color)

Hotkeys:  Start = Ctrl + Alt + Shift + T      Stop = Ctrl + Alt + Shift + X
"""

import json
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox
from pynput import keyboard

try:
    from pygments.lexers import get_lexer_by_name
    from pygments.token import Token
    from pygments.util import ClassNotFound
    HAVE_PYGMENTS = True
except ImportError:
    HAVE_PYGMENTS = False

# Global hotkeys:
# Start: Ctrl + Alt + Shift + T
# Stop:  Ctrl + Alt + Shift + X
START_HOTKEY = "<ctrl>+<alt>+<shift>+t"
STOP_HOTKEY = "<ctrl>+<alt>+<shift>+x"


# ============================================================================
#  COLORS  (edit these to tweak the look)
# ============================================================================

# App chrome (window, buttons, entry, dropdown)
UI = {
    "bg": "#1b1b2b",
    "fg": "#d8d9ea",
    "muted": "#8a8eb0",
    "field": "#13131f",
    "button": "#2d2e4a",
    "button_hover": "#3b3d63",
    "button_pressed": "#252640",
    "border": "#34364f",
    "scroll": "#2f3150",
    "scroll_hover": "#44476e",
}

# Code editor
EDITOR = {
    "bg": "#13131f",
    "fg": "#e8e8f4",
    "gutter_bg": "#10101a",
    "gutter_fg": "#7d819e",
    "gutter_fg_active": "#e8e8f4",
    "current_line": "#23233a",
    "selection": "#3b4278",
    "cursor": "#ffffff",
}

# Syntax colors:  tag name -> (color, style)   style is None, "bold" or "italic"
SYNTAX = {
    "keyword":   ("#f06be0", "bold"),    # local, function, return, if, class ...
    "constant":  ("#ff9e64", None),      # True, False, None, nil, null, CONSTANTS
    "type":      ("#ffd479", None),      # class names, int, string ...
    "builtin":   ("#4dd8f0", None),      # print, len, self, game, console ...
    "function":  ("#8ab4ff", None),      # function names and calls
    "decorator": ("#c792ea", None),      # @decorators
    "preproc":   ("#c792ea", None),      # #include, #define
    "tag":       ("#f06be0", None),      # HTML tags, JSON keys
    "attribute": ("#8ab4ff", None),      # HTML attributes
    "string":    ("#a9f08a", None),
    "escape":    ("#5de4c7", None),      # \n inside strings, {x} in f-strings
    "number":    ("#ffc66d", None),
    "operator":  ("#c0c4de", None),
    "comment":   ("#7c85ab", "italic"),
}

# Biggest text we will try to color (keeps the UI snappy on huge pastes)
MAX_HIGHLIGHT_CHARS = 400_000


# ============================================================================
#  LANGUAGES
# ============================================================================

AUTO = "Auto-detect"

# Language selected when the app starts: AUTO, or any name from LANGUAGES
# (for example "Python" if that is what you paste most of the time).
DEFAULT_LANGUAGE = AUTO

# Display name -> Pygments lexer aliases (the first one that exists is used)
LANGUAGES = {
    "Plain text": ["text"],
    "Python": ["python"],
    "JavaScript": ["javascript"],
    "TypeScript": ["typescript"],
    "Java": ["java"],
    "C#": ["csharp"],
    "C": ["c"],
    "C++": ["cpp"],
    "Go": ["go"],
    "Rust": ["rust"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "PHP": ["php"],
    "Ruby": ["ruby"],
    "Lua": ["lua"],
    "Luau (Roblox)": ["luau", "lua"],
    "SQL": ["sql"],
    "HTML": ["html"],
    "CSS": ["css"],
    "JSON": ["json"],
    "YAML": ["yaml"],
    "XML": ["xml"],
    "Bash / Shell": ["bash"],
    "PowerShell": ["powershell"],
}

# Pygments token type -> color tag. A token falls back to its parent type
# (Name.Builtin.Pseudo -> Name.Builtin) until one of these matches.
if HAVE_PYGMENTS:
    TOKEN_TAGS = {
        Token.Keyword: "keyword",
        Token.Keyword.Constant: "constant",
        Token.Keyword.Type: "type",
        Token.Operator.Word: "keyword",
        Token.Operator: "operator",
        Token.Name.Builtin: "builtin",
        Token.Name.Function: "function",
        Token.Name.Class: "type",
        Token.Name.Exception: "type",
        Token.Name.Decorator: "decorator",
        Token.Name.Constant: "constant",
        Token.Name.Tag: "tag",
        Token.Name.Attribute: "attribute",
        Token.Literal.String: "string",
        Token.Literal.String.Doc: "comment",
        Token.Literal.String.Escape: "escape",
        Token.Literal.String.Interpol: "escape",
        Token.Literal.Number: "number",
        Token.Comment: "comment",
        Token.Comment.Preproc: "preproc",
        Token.Comment.PreprocFile: "string",
    }
else:
    TOKEN_TAGS = {}


# Roblox globals, colored like built-ins when the language is Lua / Luau
ROBLOX_GLOBALS = {
    "game", "script", "workspace", "Instance", "Vector3", "Vector2", "CFrame",
    "Color3", "UDim2", "Enum", "task",
}


def make_lexer(aliases):
    for alias in aliases:
        try:
            # stripnl=False is essential: it keeps token offsets identical to the
            # text in the box (otherwise leading blank lines would shift the colors).
            return get_lexer_by_name(alias, stripnl=False, ensurenl=True, startinline=True)
        except ClassNotFound:
            continue
    return get_lexer_by_name("text", stripnl=False, ensurenl=True)


def tag_for_token(ttype, next_value):
    # An ordinary identifier directly followed by "(" is a function call.
    if (ttype is Token.Name or ttype is Token.Name.Other) and next_value.startswith("("):
        return "function"
    while ttype is not None:
        tag = TOKEN_TAGS.get(ttype)
        if tag:
            return tag
        ttype = ttype.parent
    return None


def compute_token_ranges(text, lexer):
    """Lex `text` and return {tag: [start, end, start, end, ...]} as Tk "line.col" indexes."""
    ranges = {}
    tokens = list(lexer.get_tokens(text))
    extra_builtins = ROBLOX_GLOBALS if lexer.name in ("Lua", "Luau") else ()
    line, col = 1, 0
    for i, (ttype, value) in enumerate(tokens):
        if not value:
            continue
        start = f"{line}.{col}"
        newlines = value.count("\n")
        if newlines:
            line += newlines
            col = len(value) - value.rfind("\n") - 1
        else:
            col += len(value)
        next_value = tokens[i + 1][1] if i + 1 < len(tokens) else ""
        if extra_builtins and value in extra_builtins and ttype in Token.Name:
            tag = "builtin"
        else:
            tag = tag_for_token(ttype, next_value)
        if tag:
            spans = ranges.setdefault(tag, [])
            spans.append(start)
            spans.append(f"{line}.{col}")
    return ranges


# ---------------------------------------------------------------------------
#  Auto-detect: weighted regex hints (best effort - use the dropdown to override)
# ---------------------------------------------------------------------------

_HINTS = {
    "Python": [
        (r"^\s*def\s+\w+\s*\(.*\)\s*(->.*)?:\s*(#.*)?$", 4),
        (r"^\s*class\s+\w+\s*(\(.*\))?:\s*(#.*)?$", 4),
        (r"^\s*(from\s+[\w.]+\s+import\s+\S|import\s+[\w.]+(\s+as\s+\w+)?\s*(,.*)?$)", 3),
        (r"^\s*(if|elif|else|for|while|try|except|finally|with)\b[^{};]*:\s*(#.*)?$", 2),
        (r"\bself\.\w+", 2),
        (r"\b(True|False|None)\b", 1),
        (r"^\s*print\(", 1),
        (r"\bif\s+__name__\s*==", 4),
    ],
    "JavaScript": [
        (r"\b(const|let|var)\s+[\w$]+\s*(=|;|,)", 2),
        (r"=>", 2),
        (r"\bfunction\s*[\w$]*\s*\(", 2),
        (r"\bconsole\.\w+\(", 4),
        (r"\brequire\(|\bmodule\.exports\b|\bexport\s+(default|const|function|class|async)\b"
         r"|\bimport\s+[^;\n]+\s+from\s+['\"]", 3),
        (r"[!=]==", 2),
        (r"\b(document|window)\.\w+", 2),
        (r"\b(async|await)\b", 1),
    ],
    "_TS_EXTRA": [
        (r"[\w)\]]\s*:\s*(string|number|boolean|any|void|unknown|never)\b", 3),
        (r"^\s*(export\s+)?(interface|enum)\s+\w+", 3),
        (r"^\s*(export\s+)?type\s+\w+\s*(<.*>)?\s*=", 3),
        (r"\bas\s+(string|number|any|const|unknown)\b", 2),
    ],
    "Java": [
        (r"\bSystem\.out\.print(ln)?\(", 5),
        (r"^\s*import\s+javax?\.", 5),
        (r"^\s*package\s+[\w.]+;", 3),
        (r"\bpublic\s+static\s+void\s+main\b", 5),
        (r"\b(public|private|protected)\s+(static\s+)?(final\s+)?(class|interface|enum|void|int|String|boolean)\b", 2),
        (r"@Override|@Autowired|@Test\b", 3),
        (r"\bnew\s+\w+(<.*?>)?\(", 1),
    ],
    "C#": [
        (r"^\s*using\s+System", 5),
        (r"\bnamespace\s+[\w.]+", 3),
        (r"\bConsole\.(Write|Read)", 5),
        (r"\{\s*get;\s*(private\s+)?set;\s*\}", 5),
        (r"\b(public|private|internal|protected)\s+(static\s+)?(async\s+)?(class|void|Task|string|int|bool)\b", 1),
        (r"\b(async\s+Task|List<\w+>|var\s+\w+\s*=\s*new)\b", 2),
        (r"^\s*\[\w+(\(.*\))?\]\s*$", 2),
    ],
    "C": [
        (r"^\s*#\s*include\s*[<\"]", 4),
        (r"\bint\s+main\s*\(", 4),
        (r"\b(printf|scanf|malloc|calloc|free|sizeof|strlen|fprintf)\s*\(", 2),
        (r"^\s*#\s*(define|ifdef|ifndef|endif|pragma)\b", 2),
        (r"\b(struct|typedef|unsigned)\b", 1),
    ],
    "_CPP_EXTRA": [
        (r"\bstd::", 4),
        (r"\bcout\s*<<|\bcin\s*>>", 4),
        (r"#\s*include\s*<(iostream|vector|string|map|set|algorithm|memory)>", 4),
        (r"\btemplate\s*<", 3),
        (r"\b(nullptr|constexpr)\b|^\s*(public|private):", 2),
    ],
    "Go": [
        (r"^\s*package\s+\w+\s*$", 4),
        (r"\bfunc\s+(\([^)]*\)\s*)?\w+\s*\(", 3),
        (r"\w\s*:=\s*", 2),
        (r"\bfmt\.\w+\(", 4),
        (r"^\s*import\s*\(\s*$|^\s*import\s+\"", 2),
        (r"\b(go\s+func|defer\s+|chan\s+\w+|interface\{\})", 2),
    ],
    "Rust": [
        (r"\bfn\s+\w+\s*(<[^>]*>)?\s*\(", 4),
        (r"\blet\s+mut\b", 4),
        (r"\b(println|print|format|vec|panic|assert(_eq)?)!\s*[(\[]", 4),
        (r"^\s*use\s+[\w:]+(::\{.*\}|::\*)?;", 3),
        (r"\b(impl|trait)\b|\bpub\s+(fn|struct|enum|mod)\b|&mut\s|&self\b|\b(Option|Result|Vec)<", 2),
    ],
    "Lua": [
        (r"^\s*local\s+(function\s+)?[\w.]+", 3),
        (r"^\s*end\s*[),;]*\s*$", 3),
        (r"\bthen\s*$", 3),
        (r"\belseif\b", 4),
        (r"~=", 3),
        (r"^\s*--", 2),
        (r"\bnil\b", 2),
        (r"\bi?pairs\s*\(", 2),
        (r"\bfunction\b[^{\n]*\)\s*$", 2),
    ],
    "Ruby": [
        (r"^\s*def\s+\w+[?!]?(\(.*\))?\s*$", 3),
        (r"^\s*(class|module)\s+[A-Z]\w*(\s*<\s*[\w:]+)?\s*$", 3),
        (r"\b(puts|attr_accessor|attr_reader|require_relative)\b", 3),
        (r"\bdo\s*\|[\w, ]+\|", 4),
        (r"\belsif\b", 4),
        (r"@\w+\s*=", 2),
        (r"^\s*end\s*$", 1),
    ],
    "SQL": [
        (r"(?is)\bselect\b.+?\bfrom\b", 5),
        (r"(?i)\b(insert\s+into|update\s+\w+\s+set|delete\s+from|create\s+(or\s+replace\s+)?"
         r"(table|view|index|database)|alter\s+table|drop\s+table)\b", 5),
        (r"(?i)\b(where|group\s+by|order\s+by|inner\s+join|left\s+join|having|limit\s+\d+)\b", 2),
    ],
    "HTML": [
        (r"(?i)<!doctype\s+html", 6),
        (r"(?i)<\s*/?\s*(html|head|body|div|span|script|style|title|meta|link|a|p|ul|ol|li|table|tr|td"
         r"|form|input|button|img|h[1-6])\b[^>]*>", 2),
    ],
    "CSS": [
        (r"^\s*[\w-]+\s*:\s*[^;{}\n]+;\s*$", 2),
        (r"^\s*@(media|import|keyframes|font-face)\b", 4),
        (r"\b\d+(px|em|rem|vh|vw)\b|#[0-9a-fA-F]{3,8}\b", 1),
    ],
    "Bash / Shell": [
        (r"^#!.*\b(ba|z|da)?sh\b", 6),
        (r"^\s*(fi|esac|done)\s*$", 3),
        (r"^\s*(if|while|until)\s+\[\[?\s", 3),
        (r"\b(echo|export|sudo|chmod|chown|grep|sed|awk|apt(-get)?|curl|mkdir)\s+[-\w$\"'./~]", 1),
        (r"\$\{?\w+\}?", 1),
    ],
    "PHP": [
        (r"<\?php", 8),
        (r"\$this->", 4),
        (r"\$\w+\s*=", 1),
        (r"\b(echo|foreach\s*\()", 1),
    ],
    "PowerShell": [
        (r"\b(Write-(Host|Output)|Get-[A-Z]\w+|Set-[A-Z]\w+|New-Object|Import-Module)\b", 5),
        (r"-(eq|ne|gt|lt|like|match)\b", 3),
        (r"\$\w+\s*=\s", 1),
    ],
}

_HINTS = {
    name: [(re.compile(pattern, re.MULTILINE), weight) for pattern, weight in hints]
    for name, hints in _HINTS.items()
}


def _score(name, sample):
    total = 0
    for regex, weight in _HINTS[name]:
        total += weight * min(len(regex.findall(sample)), 3)  # a pattern counts at most 3 times
    return total


def detect_language(text):
    """Best-effort guess of the language, returned as a name from LANGUAGES."""
    stripped = text.strip()
    if stripped and stripped[0] in "{[" and stripped[-1] in "}]":
        try:
            json.loads(stripped)
            return "JSON"
        except ValueError:
            pass

    sample = text[:8000]
    scores = {}
    for name in _HINTS:
        if not name.startswith("_"):
            scores[name] = _score(name, sample)

    ts_extra = _score("_TS_EXTRA", sample)
    if ts_extra:
        scores["TypeScript"] = scores["JavaScript"] + ts_extra
    cpp_extra = _score("_CPP_EXTRA", sample)
    if cpp_extra:
        scores["C++"] = scores["C"] + cpp_extra

    # max() keeps the first of equal scores, so earlier (more common) languages win ties
    best_name = max(scores, key=scores.get)
    return best_name if scores[best_name] >= 3 else "Plain text"


# ============================================================================
#  WIDGETS
# ============================================================================

# Tcl-side proxy for CodeText. It is written in Tcl (not Python) on purpose: when the
# real widget command raises an error, the error must travel back to Tk untouched so
# Tk's own scripts can catch it (they probe for the selection that way all the time).
_PROXY_TCL = """
proc ::_codetext_proxy {widget orig args} {
    set result [$orig {*}$args]
    set sub [lindex $args 0]
    set two [lrange $args 0 1]
    if {$sub in {insert replace delete} || $two in {{edit undo} {edit redo}}} {
        catch {event generate $widget <<TextChanged>> -when tail}
    } elseif {[lrange $args 0 2] eq {mark set insert} || $sub eq {see}
              || $two in {{xview moveto} {xview scroll} {yview moveto} {yview scroll}}} {
        catch {event generate $widget <<ViewChanged>> -when tail}
    }
    return $result
}
"""


class CodeText(tk.Text):
    """A Text widget that announces edits and view changes via virtual events:
         <<TextChanged>>  text was inserted / deleted / undone / redone
         <<ViewChanged>>  cursor moved or the view scrolled
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig = self._w + "_orig"
        self.tk.eval(_PROXY_TCL)
        self.tk.call("rename", self._w, self._orig)
        self.tk.call("interp", "alias", "", self._w, "", "::_codetext_proxy", self._w, self._orig)


class LineNumbers(tk.Canvas):
    """Line-number gutter that stays in sync with a Text widget."""

    def __init__(self, master, text_widget, font, colors):
        super().__init__(master, width=48, bg=colors["gutter_bg"], highlightthickness=0, bd=0)
        self.text = text_widget
        self.font = font
        self.colors = colors
        self._width = 48

    def redraw(self):
        try:
            self._redraw()
        except tk.TclError:  # window is closing
            pass

    def _redraw(self):
        self.delete("all")
        text = self.text

        last_line = int(text.index("end-1c").split(".")[0])
        digits = max(3, len(str(last_line)))
        width = self.font.measure("0") * digits + 26
        if width != self._width:
            self._width = width
            self.configure(width=width)

        current = int(text.index("insert").split(".")[0])
        index = text.index("@0,0")
        previous = 0
        while True:
            info = text.dlineinfo(index)
            if info is None:
                break
            line = int(index.split(".")[0])
            if line == previous or line > last_line:  # past the last line
                break
            previous = line
            color = self.colors["gutter_fg_active"] if line == current else self.colors["gutter_fg"]
            self.create_text(width - 14, info[1], anchor="ne", text=str(line),
                             font=self.font, fill=color)
            index = text.index(f"{index}+1line")


# ============================================================================
#  APP
# ============================================================================

def pick_code_font_family(root):
    available = set(tkfont.families(root))
    for name in ("Consolas", "Cascadia Mono", "JetBrains Mono", "Menlo", "DejaVu Sans Mono", "Courier New"):
        if name in available:
            return name
    return "Courier"


class TextTyperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Text Typer     Refined by superdonutr8 | Donutgamer0684")
        self.root.geometry("900x660")
        self.root.minsize(640, 460)

        self.controller = keyboard.Controller()
        self.stop_event = threading.Event()
        self.typing_thread = None
        self.is_typing = False

        # Syntax highlighting state
        self._highlight_job = None
        self._auto_language = None   # cached result of Auto-detect
        self._auto_length = 1        # text length when it was detected
        self._lexers = {}

        family = pick_code_font_family(self.root)
        self.font_regular = tkfont.Font(root=self.root, family=family, size=12)
        self.font_bold = tkfont.Font(root=self.root, family=family, size=12, weight="bold")
        self.font_italic = tkfont.Font(root=self.root, family=family, size=12, slant="italic")

        self._apply_theme()
        self._build_ui()
        self.listener = keyboard.GlobalHotKeys({
            START_HOTKEY: self.start_from_hotkey,
            STOP_HOTKEY: self.stop_from_hotkey,
        })
        self.listener.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ theme
    def _apply_theme(self):
        root = self.root
        root.configure(bg=UI["bg"])

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=UI["bg"], foreground=UI["fg"], fieldbackground=UI["field"],
                        bordercolor=UI["border"], lightcolor=UI["bg"], darkcolor=UI["bg"],
                        troughcolor=UI["bg"], focuscolor=UI["bg"], insertcolor=UI["fg"])

        style.configure("TButton", background=UI["button"], foreground=UI["fg"],
                        lightcolor=UI["button"], darkcolor=UI["button"], padding=(12, 6))
        style.map("TButton",
                  background=[("pressed", UI["button_pressed"]), ("active", UI["button_hover"])],
                  lightcolor=[("pressed", UI["button_pressed"]), ("active", UI["button_hover"])],
                  darkcolor=[("pressed", UI["button_pressed"]), ("active", UI["button_hover"])])

        style.configure("TEntry", fieldbackground=UI["field"], foreground=UI["fg"],
                        lightcolor=UI["field"], darkcolor=UI["field"], padding=4)

        style.configure("TCombobox", fieldbackground=UI["field"], background=UI["button"],
                        foreground=UI["fg"], arrowcolor=UI["fg"],
                        lightcolor=UI["field"], darkcolor=UI["field"],
                        selectbackground=UI["field"], selectforeground=UI["fg"], padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", UI["field"]), ("disabled", UI["bg"])],
                  foreground=[("readonly", UI["fg"]), ("disabled", UI["muted"])],
                  selectbackground=[("readonly", UI["field"])],
                  selectforeground=[("readonly", UI["fg"])],
                  background=[("pressed", UI["button_pressed"]), ("active", UI["button_hover"])])
        # The dropdown list of a Combobox is a plain Tk Listbox
        root.option_add("*TCombobox*Listbox.background", UI["field"])
        root.option_add("*TCombobox*Listbox.foreground", UI["fg"])
        root.option_add("*TCombobox*Listbox.selectBackground", EDITOR["selection"])
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        for orient in ("Vertical", "Horizontal"):
            name = f"{orient}.TScrollbar"
            style.configure(name, background=UI["scroll"], troughcolor=EDITOR["bg"],
                            bordercolor=EDITOR["bg"], lightcolor=UI["scroll"],
                            darkcolor=UI["scroll"], arrowcolor=UI["fg"], gripcount=0)
            style.map(name,
                      background=[("pressed", UI["scroll_hover"]), ("active", UI["scroll_hover"])],
                      lightcolor=[("pressed", UI["scroll_hover"]), ("active", UI["scroll_hover"])],
                      darkcolor=[("pressed", UI["scroll_hover"]), ("active", UI["scroll_hover"])])

    # --------------------------------------------------------------------- UI
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text="Text Typer v2 with code detection",
            font=("Segoe UI", 18, "bold")
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text="Paste or type text below. Focus the app where you want it entered, then use the start hotkey."
        ).pack(anchor="w", pady=(4, 10))

        # --- language row
        self.lang_var = tk.StringVar(value=DEFAULT_LANGUAGE)
        self.detected_var = tk.StringVar(value="")
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="Language:").pack(side="left")
        self.lang_combo = ttk.Combobox(
            toolbar, textvariable=self.lang_var, values=[AUTO] + list(LANGUAGES),
            state="readonly", width=18
        )
        self.lang_combo.pack(side="left", padx=(6, 12))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_selected)
        ttk.Label(toolbar, textvariable=self.detected_var, foreground=UI["muted"]).pack(side="left")

        # --- editor: line numbers | text | scrollbar
        border = tk.Frame(outer, bg=UI["border"])
        border.pack(fill="both", expand=True)
        editor = tk.Frame(border, bg=EDITOR["bg"])
        editor.pack(fill="both", expand=True, padx=1, pady=1)
        editor.grid_rowconfigure(0, weight=1)
        editor.grid_columnconfigure(1, weight=1)

        self.text_box = CodeText(
            editor, wrap="none", undo=True, font=self.font_regular, height=18,
            bg=EDITOR["bg"], fg=EDITOR["fg"],
            insertbackground=EDITOR["cursor"], insertwidth=2,
            selectbackground=EDITOR["selection"], selectforeground="#ffffff",
            inactiveselectbackground=EDITOR["selection"],
            relief="flat", borderwidth=0, highlightthickness=0, padx=10, pady=8,
            tabs=(self.font_regular.measure(" " * 4),),
        )
        self.line_numbers = LineNumbers(editor, self.text_box, self.font_regular, EDITOR)
        self.v_scroll = ttk.Scrollbar(editor, orient="vertical", command=self.text_box.yview)
        self.h_scroll = ttk.Scrollbar(editor, orient="horizontal", command=self.text_box.xview)
        self.text_box.configure(yscrollcommand=self._on_yscroll, xscrollcommand=self.h_scroll.set)

        self.line_numbers.grid(row=0, column=0, sticky="ns")
        self.text_box.grid(row=0, column=1, sticky="nsew")
        self.v_scroll.grid(row=0, column=2, sticky="ns")
        self.h_scroll.grid(row=1, column=0, columnspan=3, sticky="ew")

        self._configure_tags()
        self.text_box.bind("<<TextChanged>>", self._on_text_changed)
        self.text_box.bind("<<ViewChanged>>", self._on_view_changed)
        self.text_box.bind("<<Paste>>", self._on_paste, add="+")
        self.text_box.bind("<Configure>", lambda _e: self.line_numbers.redraw(), add="+")

        # --- controls
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 0))

        ttk.Label(controls, text="Delay between characters (ms):").pack(side="left")
        self.delay_var = tk.StringVar(value="15")
        ttk.Entry(controls, textvariable=self.delay_var, width=8).pack(side="left", padx=(6, 14))

        self.start_button = ttk.Button(controls, text="Start (Ctrl+Alt+Shift+T)", command=self.start_typing)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop (Ctrl+Alt+Shift+X)", command=self.stop_typing)
        self.stop_button.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(10, 0))

        ttk.Label(
            outer,
            text="Tip: After pressing Start, you have 3 seconds to click the destination app. Press the stop hotkey to cancel.",
            foreground=UI["muted"]
        ).pack(anchor="w", pady=(5, 0))

        if not HAVE_PYGMENTS:
            self.lang_combo.configure(state="disabled")
            self.detected_var.set("Syntax colors need Pygments:  pip install pygments")

        self.text_box.focus_set()
        self.root.after_idle(self._refresh_view)

    # ------------------------------------------------- editor look & behavior
    def _configure_tags(self):
        box = self.text_box
        fonts = {"bold": self.font_bold, "italic": self.font_italic}
        for tag, (color, style) in SYNTAX.items():
            options = {"foreground": color}
            if style:
                options["font"] = fonts[style]
            box.tag_configure(tag, **options)
        box.tag_configure("current_line", background=EDITOR["current_line"])
        box.tag_lower("current_line")
        box.tag_raise("sel")  # selection must win over every other tag

    def _on_yscroll(self, first, last):
        self.v_scroll.set(first, last)
        self.line_numbers.redraw()

    def _on_text_changed(self, _event=None):
        self._schedule_highlight()
        self._refresh_view()

    def _on_view_changed(self, _event=None):
        self._refresh_view()

    def _on_paste(self, _event=None):
        self._auto_language = None  # re-detect the language for freshly pasted code

    def _refresh_view(self):
        """Move the current-line band and redraw the line numbers."""
        box = self.text_box
        box.tag_remove("current_line", "1.0", "end")
        box.tag_add("current_line", "insert linestart", "insert lineend+1c")
        self.line_numbers.redraw()

    def _on_language_selected(self, _event=None):
        self._auto_language = None
        self.lang_combo.selection_clear()
        self.text_box.focus_set()
        self._schedule_highlight(0)

    # -------------------------------------------------------- syntax coloring
    def _schedule_highlight(self, delay_ms=120):
        if self._highlight_job is not None:
            self.root.after_cancel(self._highlight_job)
        self._highlight_job = self.root.after(delay_ms, self._highlight)

    def _language_for(self, text):
        choice = self.lang_var.get()
        if choice != AUTO:
            return choice
        # Detect on paste / first use, and again if the text has more than doubled.
        if self._auto_language is None or len(text) > 2 * self._auto_length:
            self._auto_language = detect_language(text)
            self._auto_length = max(len(text), 1)
        return self._auto_language

    def _lexer_for(self, language):
        if language not in self._lexers:
            self._lexers[language] = make_lexer(LANGUAGES.get(language, ["text"]))
        return self._lexers[language]

    def _highlight(self):
        self._highlight_job = None
        if not HAVE_PYGMENTS:
            return

        box = self.text_box
        for tag in SYNTAX:
            box.tag_remove(tag, "1.0", "end")

        text = box.get("1.0", "end-1c")
        if not text.strip():
            self._auto_language = None
            self.detected_var.set("")
            return
        if len(text) > MAX_HIGHLIGHT_CHARS:
            self.detected_var.set("Text is too large for syntax colors")
            return

        language = self._language_for(text)
        self.detected_var.set(f"Detected: {language}" if self.lang_var.get() == AUTO else "")

        try:
            ranges = compute_token_ranges(text, self._lexer_for(language))
        except Exception:
            return  # never let a lexer problem get in the way of the app
        for tag, spans in ranges.items():
            for i in range(0, len(spans), 4000):
                box.tag_add(tag, *spans[i:i + 4000])

    # ---------------------------------------------------------------- hotkeys
    def start_from_hotkey(self):
        self.root.after(0, self.start_typing)

    def stop_from_hotkey(self):
        self.root.after(0, self.stop_typing)

    # ----------------------------------------------------------------- typing
    def start_typing(self):
        if self.is_typing:
            return

        content = self.text_box.get("1.0", "end-1c")
        if not content:
            self.status_var.set("Nothing to type — paste some text first.")
            return

        try:
            delay_ms = float(self.delay_var.get())
            if delay_ms < 0 or delay_ms > 5000:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid delay", "Enter a delay from 0 to 5000 milliseconds.")
            return

        self.stop_event.clear()
        self.is_typing = True
        self.status_var.set("Starting in 3 seconds… focus the destination app now.")
        self.typing_thread = threading.Thread(
            target=self._type_worker,
            args=(content, delay_ms / 1000.0),
            daemon=True
        )
        self.typing_thread.start()

    def _type_worker(self, content, delay):
        for remaining in (3, 2, 1):
            if self.stop_event.wait(1):
                self.root.after(0, self._finished, "Stopped before typing.")
                return
            self.root.after(0, self.status_var.set, f"Typing starts in {remaining - 1}…")

        try:
            for char in content:
                if self.stop_event.is_set():
                    break
                self.controller.type(char)
                if delay:
                    time.sleep(delay)
            result = "Stopped." if self.stop_event.is_set() else "Finished typing."
        except Exception as exc:
            result = f"Typing error: {exc}"
        self.root.after(0, self._finished, result)

    def _finished(self, message):
        self.is_typing = False
        self.status_var.set(message)

    def stop_typing(self):
        if self.is_typing:
            self.stop_event.set()
            self.status_var.set("Stopping…")

    def close(self):
        self.stop_event.set()
        if self._highlight_job is not None:
            try:
                self.root.after_cancel(self._highlight_job)
            except Exception:
                pass
        try:
            self.listener.stop()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = TextTyperApp(root)
    root.mainloop()