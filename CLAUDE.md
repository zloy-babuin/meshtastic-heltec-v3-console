# meshtastic playground / meshtui

Песочница для работы с Meshtastic-нодой по USB и консольный клиент **meshtui** (Python + Textual). Документация пользователя — `README.md`.

## Железо
- **Heltec WiFi LoRa 32 V3**: ESP32-S3FN8 (8 МБ флеша, без PSRAM), USB-мост CP2102 (`10c4:ea60`), SX1262 863–928 МГц, OLED 0.96″, кнопка PRG.
- Порт: `/dev/ttyUSB0`, стабильный путь `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`. Пользователь в группе `dialout`.
- ID ноды, имя владельца и MAC — в `CLAUDE.local.md` (не в git).
- Прошивка Meshtastic **2.7.26.54e0d8d** (`heltec-v3`), прошита 2026-09-28 с полным стиранием. Заводская тестовая прошивка удалена.
- Текущие настройки: `lora.region=RU`, пресет LongFast, `lora.tx_power=20` (для RU без лицензии, вероятно, допустимо ~14 дБм / 25 мВт — пользователю предложено снизить, решения нет), `display.screen_on_secs=20`, `device.led_heartbeat_disabled=true`, `device.tzdef=MSK-3`, роль `CLIENT`.

### Грабли с железом
- Первый USB-кабель оказался только для зарядки: плата не появлялась в `lsusb`, в журнале ядра — ни одного события.
- Системный `/usr/bin/esptool` (пакет Ubuntu 4.7.0) без stub для ESP32-S3 — прошивать через `pip`/`pipx install esptool`. Адреса для heltec-v3: factory.bin → `0x0`, `mt-esp32s3-ota.bin` → `0x340000`, littlefs → `0x670000` (см. `*.mt.json` в релизе).
- Открытие порта дёргает DTR/RTS и сбрасывает ESP32.
- Батарейки часов нет: время сбрасывается при каждой перезагрузке. `device.tzdef` применяется только после перезагрузки.
- Оранжевый светодиод CHG аппаратный, программно не отключается.

## meshtui

```bash
.venv/bin/pip install -e .          # venv уже создан, пакет установлен в editable-режиме
.venv/bin/meshtui                   # автопоиск порта; -p PORT, --host IP, --no-time-sync, --log FILE
.venv/bin/python -m pyflakes meshtui
.venv/bin/python dev/screenshots.py /tmp/shots   # headless-прогон с нодой, SVG всех вкладок
```

### Устройство
- `meshtui/radio.py` — `Radio`: обёртка над `meshtastic.SerialInterface`/`TCPInterface`. Подписка на pubsub (`meshtastic.receive`, `connection.established/lost`, `node.updated`), события отдаются в колбэк `on_event(kind, data)` **из потоков библиотеки**. Команды: `send_text`, `set_time`, `set_owner`, `write_section`, `reboot`, `shutdown`.
- `meshtui/config_form.py` — `ConfigForm`: форма секции конфигурации, строится по protobuf-дескриптору (bool → Switch, enum → Select, числа/строки → Input, bytes и repeated — только чтение, вложенные сообщения раскрываются через точку). `apply()` пишет в копию сообщения и возвращает список изменений.
- `meshtui/app.py` — `MeshTUI` (Textual App): вкладки Чат/Узлы/Эфир/Устройство/Настройки (F1–F5), строка статуса, модальный `Confirm`. Все блокирующие вызовы радио — через `@work(thread=True)`, обратно в UI — `call_from_thread`. Таймер `_tick` раз в 2 с обновляет таблицы и следит за переподключением.

### Неочевидные решения и грабли
- **Не называть методы App так же, как внутренности Textual.** Обработчик `_shutdown` перекрыл `App._shutdown` и ломал выход. Проверка: пересечь имена методов `MeshTUI` с `dir(App)`.
- **Переключение вкладок**: перед сменой `TabbedContent.active` нужно `set_focus(None)`. Иначе фокус перескакивает на соседний виджет скрываемой вкладки, и TabbedContent возвращает её обратно (`action_show`).
- Подписи вкладок с `[F1]` — это Rich-разметка, экранировать как `\[F1]`.
- **ACK**: `MeshInterface.sendText` не пробрасывает `onResponseAckPermitted`, а без него успешные ACK не доходят до колбэка. Поэтому используется `sendData(..., portNum=TEXT_MESSAGE_APP, wantAck=True, onResponseAckPermitted=True)`. ACK с `from == адресат` значит «доставлено», с другим `from` — неявный ACK от ретранслятора.
- Событие `connection.established` при первом подключении приходит, когда `radio.iface` ещё не присвоен, и отфильтровывается. Поэтому воркер `_connect` сам вызывает `_on_connected`, а тот идемпотентен (флаг `_online`).
- **Переподключение**: при перезагрузке ноды библиотека сама шлёт `lost` → перечитывает конфиг → `established`, порт остаётся открытым. Приложение ждёт `RECONNECT_GRACE` = 30 с. Если порт пропал (кабель выдернут), библиотека закрывает его и обнуляет `iface.stream` → `Radio.port_closed` → переподключение сразу, с автопоиском порта, если не задан `-p`.
- Библиотека `meshtastic` местами вызывает `sys.exit()` (`our_exit`), поэтому ловится `(Exception, SystemExit)`. Её логи уходят в stderr и ломают TUI — в `main()` на корневой логгер вешается `NullHandler` (или файл через `--log`).
- float-поля protobuf хранятся как float32: в форме они сравниваются с допуском, иначе появляются ложные «изменения».
- Лимит текста — `mesh_pb2.Constants.DATA_PAYLOAD_LEN` = 233 **байта** (кириллица занимает 2 байта).

### Проверка
- Headless через `app.run_test()` + `save_screenshot()`, SVG → PNG через `google-chrome --headless`. Порт не должен быть занят (закрыть `meshtastic` CLI).
- Запись настроек проверена на `display.screen_on_secs` (20 → 21 → 20) со сверкой через `meshtastic --get display`.
- **Отправка сообщений вживую не проверялась**: `LongFast` — публичный канал, в эфир без согласия пользователя ничего не отправлять. Для личного сообщения нужен второй узел.
- Не реализовано: редактирование каналов (только просмотр), сохранение истории чата.
