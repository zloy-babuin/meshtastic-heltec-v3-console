"""Headless-прогон meshtui с реальной нодой: скриншоты всех вкладок в SVG.

    .venv/bin/python dev/screenshots.py OUT_DIR [PORT]

PNG для просмотра:
    google-chrome --headless=new --hide-scrollbars --window-size=1900,1100 \
        --screenshot=OUT_DIR/chat.png file://OUT_DIR/chat.svg
Порт при этом не должен быть занят другой программой.
"""

import asyncio
import sys
from pathlib import Path

from meshtui.app import MeshTUI

TABS = [("f1", "chat"), ("f2", "nodes"), ("f3", "air"), ("f4", "device")]


async def main(out: str, port: str | None) -> None:
    Path(out).mkdir(parents=True, exist_ok=True)
    app = MeshTUI(port, None, sync_time=False)
    async with app.run_test(size=(150, 42)) as pilot:
        for _ in range(60):
            await pilot.pause(0.5)
            if app._online:
                break
        print("online:", app._online, app._conn_msg)
        await pilot.pause(3)  # дать прийти первым пакетам
        for key, name in TABS:
            await pilot.press(key)
            await pilot.pause(1)
            app.save_screenshot(f"{name}.svg", out)
        await pilot.press("f5")
        lv = app.query_one("#sections")
        lv.index = [c.id for c in lv.children].index("sec-lora")
        await pilot.press("enter")
        await pilot.pause(1)
        app.save_screenshot("settings.svg", out)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
