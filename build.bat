@echo off
rmdir /s /q build
rmdir /s /q dist
del /q *.spec

pyinstaller -D -w -n osu_mania_set_exporter main.py


pause
