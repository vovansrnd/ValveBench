Сырой захват базового прогона timing-v2 (см. REPORT-TIMING-D10-EMU-v2.md).
capture-full-baseline-2s.raw: E-MU arecord, S24_3LE, stereo, 192000 Hz, 7.0 с (1344000 кадров, 8064000 байт).
submitted-baseline-2s.s32: программа, поданная в aplay, S32_LE stereo, 192000 Hz (2-байтовый пад 0.05 с в начале и конце + программа: тишина 1 с, чирп-А 6.5-7.5 кГц, тон 997 Гц 2 с, чирп-Б 10.5-11.5 кГц, тишина 1 с).
Воспроизведение: aplay -D hw:D10 -f S32_LE -r 192000 -c 2 --buffer-time 100000 submitted-baseline-2s.s32
Захват: arecord -D hw:E-MU -f S24_3LE -r 192000 -c 2 (старт на 0.5 с раньше aplay).
