"""Заглушка gi (Gtk/Adw/GLib) для проверки логики страниц без дисплея — см. test_ui_logic_0120.py."""
import sys, types
class _Color: red, green, blue = 0.92, 0.92, 0.92
_DEF = {"get_selected": 0, "get_value": 0.0, "get_active": False, "get_text": "", "get_name": "",
        "get_n_items": 0, "get_width": 800, "get_height": 600, "get_first_child": None, "get_next_sibling": None}
class _V:
    """Значение-заглушка: вызывается, даёт атрибуты, поддерживает | и сравнения."""
    def __call__(self, *a, **k): return _Stub()
    def __getattr__(self, n): return _V()
    def __or__(self, o): return self
    __ror__ = __or__
    def __int__(self): return 0
    __index__ = __int__
_STATE = ("selected", "active", "value", "text")


class _Stub:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        # set_selected/get_selected, set_active/get_active, ... хранят значение — как настоящие виджеты
        if n[:4] == "set_" and n[4:] in _STATE:
            return lambda v=None, *a, **k: self.__dict__.__setitem__("_" + n[4:], v)
        if n[:4] == "get_" and n[4:] in _STATE and ("_" + n[4:]) in self.__dict__:
            return lambda *a, **k: self.__dict__["_" + n[4:]]
        if n in _DEF:
            v = _DEF[n]; return lambda *a, **k: v
        return lambda *a, **k: _Stub()
    def get_color(self): return _Color()
class _Meta(type):
    def __getattr__(cls, n): return _V()
def _cls(name): return _Meta(name, (_Stub,), {})
class _Mod(types.SimpleNamespace):
    def __getattr__(self, n):
        c = _cls(n); setattr(self, n, c); return c
def install():
    gi = types.ModuleType("gi"); gi.require_version = lambda *a, **k: None
    rep = types.ModuleType("gi.repository")
    for n in ("Gtk", "Adw", "GLib", "Gio", "Gdk", "GObject", "Pango"):
        setattr(rep, n, _Mod())
    sys.modules.update({"gi": gi, "gi.repository": rep})
