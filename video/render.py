"""Render problem_built.html frame by frame in a sandbox browser (runs inside the control app container).

python render.py stills 1 4.6 9 13.2 15 18.9   -> /tmp/vid/still_<t>.png
python render.py frames 30                     -> /tmp/vid/frames/f00000.png ... (DURATION * fps frames)
"""
import asyncio
import pathlib
import sys

from app import workers
from app.browser import SandboxBrowser

OUT = pathlib.Path("/tmp/vid")


async def main(mode, args):
    html = (OUT / "problem_built.html").read_text(encoding="utf-8")
    worker, sb = await workers.create_sandbox("video", "about:blank")
    try:
        await asyncio.sleep(5)
        async with SandboxBrowser("video", sb["cdp"], record=False) as b:
            ctx = await b.browser.new_context(viewport={"width": 1920, "height": 1080})
            p = await ctx.new_page()
            await p.set_content(html, wait_until="networkidle")
            await p.evaluate("document.fonts.ready")
            if mode == "stills":
                for t in args:
                    await p.evaluate(f"seek({float(t)})")
                    await p.screenshot(path=str(OUT / f"still_{t}.png"))
                    print("still", t)
            else:
                fps = int(args[0])
                frames = OUT / "frames"
                frames.mkdir(parents=True, exist_ok=True)
                total = int(await p.evaluate("DURATION") * fps)
                for i in range(total + 1):
                    await p.evaluate(f"seek({i / fps})")
                    await p.screenshot(path=str(frames / f"f{i:05d}.png"))
                    if i % 60 == 0:
                        print("frame", i, "/", total, flush=True)
    finally:
        await workers.delete_sandbox(worker, sb["id"])


asyncio.run(main(sys.argv[1], sys.argv[2:]))
