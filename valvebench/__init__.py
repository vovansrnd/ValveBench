"""ValveBench — измерительная лаборатория для аналоговой аудиотехники."""
import os as _os

# Однопоточный BLAS — обязателен ДО первого импорта numpy (здесь это
# гарантировано: __init__ исполняется раньше любого подмодуля). Матрицы
# GainTracker малы (окно 0.6 с × ~11 колонок), OpenBLAS на каждую lstsq/pinv
# плодит потоки по числу ядер, и накладные расходы многократно превышают
# пользу: замер на E-MU 0404 USB (48 кГц, тик 75 мс) дал 124.8 % CPU
# процесса при многопоточном BLAS против 31.3 % при одном потоке — при
# одинаковых результатах DSP. Переменные setdefault: пользователь может
# переопределить до запуска (явная настройка уважается, см. ниже).
_BLAS_THREAD_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                     "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
_user_blas_vars = [v for v in _BLAS_THREAD_VARS if v in _os.environ]
for _v in _BLAS_THREAD_VARS:
    _os.environ.setdefault(_v, "1")

# Усиление для случая «numpy импортирован раньше пакета» (встраивание,
# нестандартные лаунчеры, осмотр из ipython): env-переменные тогда опоздали —
# пул OpenBLAS уже создан при импорте numpy. Просим библиотеку напрямую
# сменить число используемых потоков (openblas_set_num_threads). Вмешиваемся
# только когда пользователь НЕ задал явную настройку; /proc/self/maps есть
# только в Linux — на прочих платформах остаётся env-путь.
if not _user_blas_vars:
    try:
        import ctypes as _ctypes
        import re as _re
        import sys as _sys
        if "numpy" in _sys.modules:
            with open("/proc/self/maps") as _maps:
                _libs = sorted(set(_re.findall(r"(/\S+libopenblas\S*\.so\S*)",
                                               _maps.read())))
            for _path in _libs:
                _lib = _ctypes.CDLL(_path)
                for _name in ("openblas_set_num_threads",
                              "openblas_set_num_threads64_"):
                    _fn = getattr(_lib, _name, None)
                    if _fn is not None:
                        _fn(_ctypes.c_int(1))
                        break
                break
    except Exception:
        pass  # не смогли — работают env-переменные для следующего импорта numpy

__version__ = "0.1.21-dev3"
