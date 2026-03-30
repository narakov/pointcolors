# RGB Point Cloud Colorizer

Набор Python-скриптов для раскраски облаков точек `LAS` по фотографиям (TerraPhoto `*.cal` + `*.iml`) или по геопривязанным ортофотоплиткам.

## 1. Что есть в проекте

- `colorize_las.py` — основной движок раскраски LAS.
- `run_colorize_job.py` — запуск готовых профилей (`standard`, `roll_minus`, `enhanced`) для одного или нескольких LAS.
- `rgbpoint_gui.py` — desktop GUI (Tkinter) для запуска профилей без ручного набора команд.
- `blend_las_rgb.py` — смешивание двух RGB-LAS (берет основной файл и заполняет черные точки из fallback).
- `compare_las_rgb.py` — сравнение кандидатов с эталонным RGB-LAS.
- `run_gui.bat` / `run_gui_debug.bat` — быстрые Windows-лаунчеры GUI.
- `rgbpoint_gui.ps1` — альтернативный PowerShell GUI-лаунчер.
- `rgbpoint_gui_launcher.log`, `rgbpoint_gui_run.log`, `rgbpoint_gui_state.json` — логи и состояние GUI.

## 2. Анализ зависимостей

По импорту всех `*.py` файлов:

- Внешние Python-библиотеки:
  - `numpy`
  - `Pillow`
- Стандартная библиотека Python:
  - `argparse`, `dataclasses`, `pathlib`, `tempfile`, `struct`, `math`, `re`, `json`, `subprocess`, `threading`, `tkinter` и др.

Итог: для запуска проекта в окружении нужно установить только `numpy` и `Pillow` (см. `requirements.txt`).

## 3. Подготовка окружения через uv

Ниже команды для PowerShell в корне проекта (`c:\Reposit\pointcolors`).

### 3.1 Проверка uv

```powershell
uv --version
```

### 3.2 Создание локального окружения

```powershell
uv venv .venv --cache-dir .uv-cache
```

Если хотите явно зафиксировать версию интерпретатора:

```powershell
uv venv .venv --python 3.11 --cache-dir .uv-cache
```

### 3.3 Установка зависимостей в `.venv`

```powershell
uv pip install --python .\.venv\Scripts\python.exe --cache-dir .uv-cache -r requirements.txt
```

### 3.4 Проверка установки

```powershell
uv pip list --python .\.venv\Scripts\python.exe --cache-dir .uv-cache
.\.venv\Scripts\python.exe .\colorize_las.py --help
.\.venv\Scripts\python.exe .\run_colorize_job.py --help
.\.venv\Scripts\python.exe .\rgbpoint_gui.py --smoke-test
```

## 4. Запуск приложения (GUI)

Рекомендуемый запуск из созданного `.venv`:

```powershell
.\.venv\Scripts\python.exe .\rgbpoint_gui.py
```

Дополнительно:

- `run_gui.bat` — запускает GUI в отдельном окне через `pythonw`/`pyw` (использует системный Python, не обязательно `.venv`).
- `run_gui_debug.bat` — запуск GUI в текущей консоли для отладки старта.
- `rgbpoint_gui.py --smoke-test` — быстрая проверка, что GUI инициализируется корректно.

Логи GUI:

- `rgbpoint_gui_launcher.log` — ошибки/события старта.
- `rgbpoint_gui_run.log` — лог выполнения задач раскраски.

## 5. Запуск через CLI

### 5.1 Раскраска по TerraPhoto-фото

```powershell
.\.venv\Scripts\python.exe .\colorize_las.py `
  --las input.las `
  --cal camera.cal `
  --iml images.iml `
  --images-dir D:\photos\set1 `
  --output input_rgb.las
```

### 5.2 Раскраска по ортофотоплиткам

```powershell
.\.venv\Scripts\python.exe .\colorize_las.py `
  --las input.las `
  --ortho-dir D:\ortho_tiles `
  --output input_rgb.las
```

Важно: для каждого изображения в `--ortho-dir` должен быть world file с тем же basename (`.jgw`, `.jpgw`, `.wld`, `.world`, `.tfw`).

### 5.3 Запуск профиля обработки

```powershell
.\.venv\Scripts\python.exe .\run_colorize_job.py `
  --las cloud_01.las `
  --cal camera.cal `
  --iml images.iml `
  --images-dir D:\photos\set1 `
  --output cloud_01_rgb.las `
  --profile enhanced
```

### 5.4 Batch-режим для нескольких LAS

```powershell
.\.venv\Scripts\python.exe .\run_colorize_job.py `
  --las cloud_01.las `
  --las cloud_02.las `
  --cal camera.cal `
  --iml images.iml `
  --images-dir D:\photos\set1 `
  --output-dir D:\rgb_results `
  --profile enhanced
```

### 5.5 Сравнение и смешивание результатов

Сравнить кандидатов с эталоном:

```powershell
.\.venv\Scripts\python.exe .\compare_las_rgb.py `
  --reference reference_rgb.las `
  --candidate candidate_a.las `
  --candidate candidate_b.las
```

Смешать два RGB-LAS (заполнить черные зоны fallback-файлом):

```powershell
.\.venv\Scripts\python.exe .\blend_las_rgb.py `
  --primary primary_rgb.las `
  --fallback fallback_rgb.las `
  --output blended_rgb.las
```

## 6. Полезные параметры

Для `colorize_las.py` чаще всего пригодятся:

- `--chunk-size 500000` или выше (если хватает RAM).
- `--default-color 0,0,0` — цвет точек без покрытия.
- `--disable-overlap-filter` — отключить предфильтр перекрытия.
- `--projection-model simple|matrix`
- `--camera-top forward|backward|right|left`
- `--rotation-order rxry|ryrx`
- `--attitude-corrections-sign plus|minus`
- `--roll-sign plus|minus`
- `--pitch-sign plus|minus`

## 7. Частые проблемы

1. Ошибка `No module named numpy` или `No module named PIL`.
   - Зависимости не установлены в нужный Python. Используйте:
   - `uv pip install --python .\.venv\Scripts\python.exe --cache-dir .uv-cache -r requirements.txt`

2. GUI не открывается.
   - Запустите `run_gui_debug.bat` или:
   - `.\.venv\Scripts\python.exe .\rgbpoint_gui.py --smoke-test`
   - Проверьте `rgbpoint_gui_launcher.log`.

3. Скрипт отрабатывает без ошибок, но раскрашено 0 точек.
   - Обычно LAS и фото/орто не пересекаются географически (разные области/системы координат).

## 8. Быстрое восстановление окружения

```powershell
Remove-Item .\.venv -Recurse -Force
Remove-Item .\.uv-cache -Recurse -Force
uv venv .venv --cache-dir .uv-cache
uv pip install --python .\.venv\Scripts\python.exe --cache-dir .uv-cache -r requirements.txt
```

---

Проверено в этом репозитории: `uv 0.11.2`, локальное окружение `.venv`, установлены `numpy 2.4.4` и `Pillow 12.1.1`.
