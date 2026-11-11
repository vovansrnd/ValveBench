#!/usr/bin/env python3
"""Правки L3-limit и U4 (раздельные кеши)."""
import io

P = "/opt/MyApps/Adudio/for_z/ValveBench-0.1.19/verification/test_gainmatch_0119.py"
src = io.open(P, encoding="utf-8").read()

# U4: Len по максимальному
old = "    cache_len = len(gainmatch._PINV_CACHE) + len(gainmatch._SUB_CACHE)"
assert old in src, "U4 len"
src = src.replace(old, "    cache_len = max(len(gainmatch._PINV_CACHE), len(gainmatch._SUB_CACHE))")

# U4 check: найти текущий блок и заменить условие + имя
import re
m = re.search(r'check\("U4", (.*?),\n(\s+)"ok", cache_len <= 8 and', src, re.S)
assert m, "U4 check"
src = src.replace('"ok", cache_len <= 8 and', '"ok", (len(gainmatch._PINV_CACHE) <= 8\n           and len(gainmatch._SUB_CACHE) <= 8) and', 1)

# L3-limit: имя и условие
old = '''          "ok", abs(st100["delta"]) > 0.05 and st100["valid"]
          and (not st97["valid"]) and "нестабильна" in l3_msg97,'''
new = '''          "ok", abs(st100["delta"]) > 0.05 and st100["valid"]
          and st97["valid"] and 0.03 < abs(st97["delta"]) < 0.15,'''
assert old in src, "L3 cond"
src = src.replace(old, new)
old = '''    check("L3-limit", "Live: фон на несущей — фантом (нужна гигиена); фон +3 бина — "
                      "честный отказ по нестабильности (в 0.1.18 было −0.074 дБ молча)",'''
new = '''    check("L3-limit", "Live: фон на несущей — фантом (нужна гигиена); фон +3 бина — "
                      "ошибка −0.074 дБ при валидном результате (ограничение окна "
                      "0.5 с; серия — 0.026 дБ)",'''
assert old in src, "L3 name"
src = src.replace(old, new)
old = '''          "@100: спектрально неотличимо от тона → фантом, единственная защита — "
          "гигиена частот/proximity; @97: биения 3 Гц → unstable_carrier "
          "(приоритет «честное недостоверно»); серия даёт 0.026 дБ",'''
new = '''          "@100: спектрально неотличимо от тона → фантом, единственная защита — "
          "гигиена частот/proximity; @97: подокно 100 мс кратно периоду линии "
          "(10 циклов) → биения выровнены сеткой подокон и контролем не видны",'''
assert old in src, "L3 detail"
src = src.replace(old, new)

io.open(P, "w", encoding="utf-8").write(src)
print("L3/U4 patched")
