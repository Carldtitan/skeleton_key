"""Inline the assets into problem.html so the page renders anywhere (one self-contained file)."""
import base64
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def data_uri(name, mime):
    return f"data:{mime};base64," + base64.b64encode((HERE / "assets" / name).read_bytes()).decode()


html = (HERE / "problem.html").read_text(encoding="utf-8")
html = (html.replace("{{LUMA_ICON}}", data_uri("luma_icon.png", "image/png"))
            .replace("{{LUMA_SHOT}}", data_uri("luma_discover.png", "image/png"))
            .replace("{{MARKET_ICON}}", data_uri("marketplace.svg", "image/svg+xml")))
out = HERE / "build" / "problem_built.html"
out.parent.mkdir(exist_ok=True)
out.write_text(html, encoding="utf-8")
print(out, len(html) // 1024, "KB")
