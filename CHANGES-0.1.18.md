# ValveBench 0.1.18 — Generator controls on Live page

## Change

Restored the existing generator-control rows to the `LivePage` layout, above
`Параметры Gain Match`:

- Частота, Гц (`self.f`)
- Уровень генератора, dBFS (`self.lv`)
- Канал генератора (`self.route`)

The controls already existed and were connected to `LivePage.apply()`, but
were not inserted into the visible `controls` container. The change exposes
them without changing generator DSP or measurement logic.

## Scope

Only `valvebench/ui.py` and `valvebench/__init__.py` differ from 0.1.17.
`pcm.py`, `steady.py`, and `io_health.py` are unchanged. Gain Match algorithms,
TARE behavior, SNR/SINAD naming, calibration, and measurement validity are
not changed in this release.
