"""Render README.md as a styled page and publish it to Vultr Object Storage (public read).

python scripts/publish_readme.py  -> prints the public URL
"""
import pathlib

import boto3
import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))

body = markdown.markdown((ROOT / "README.md").read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Skeleton Key</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400..800&family=JetBrains+Mono&display=swap">
<style>
:root{{--bg:#f8f2e8;--card:#fffdf9;--ink:#23150f;--ink2:#614537;--line:#e3d6c8;--accent:#b85632;--code:#2d1b14}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1d120d;--card:#2a1a13;--ink:#f8f2e8;--ink2:#cbb9ad;--line:#3d281f;--accent:#e8a17b;--code:#120b08}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:18px/1.65 'DM Sans',Arial,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:64px 20px 96px}}
h1{{font-size:clamp(44px,8vw,72px);letter-spacing:-.03em;line-height:1;margin:0 0 18px}}
h2{{font-size:34px;letter-spacing:-.02em;margin:64px 0 12px;padding-top:24px;border-top:1px solid var(--line)}}
h3{{font-size:22px;margin:36px 0 8px}}a{{color:var(--accent)}}strong{{color:var(--ink)}}p,li{{color:var(--ink2)}}
hr{{border:0;border-top:1px solid var(--line);margin:32px 0}}
table{{width:100%;border-collapse:collapse;margin:18px 0;font-size:16px;background:var(--card);border-radius:12px;overflow:hidden;display:block;overflow-x:auto}}
th,td{{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink2)}}
code{{font:15px 'JetBrains Mono',monospace;background:var(--card);padding:2px 6px;border-radius:6px}}
pre{{background:var(--code);color:#f3e6da;padding:18px 20px;border-radius:14px;overflow-x:auto}}pre code{{background:none;padding:0;color:inherit}}
</style></head><body><main>{body}</main></body></html>"""

s3 = boto3.client("s3", endpoint_url=env["S3_ENDPOINT"] if env["S3_ENDPOINT"].startswith("http") else f"https://{env['S3_ENDPOINT']}",
                  aws_access_key_id=env["S3_ACCESS_KEY"], aws_secret_access_key=env["S3_SECRET_KEY"])
BUCKET = "sk-specs"
s3.put_object(Bucket=BUCKET, Key="readme/index.html", Body=page.encode(), ContentType="text/html; charset=utf-8", ACL="public-read")
host = env["S3_ENDPOINT"].removeprefix("https://").removeprefix("http://")
print(f"https://{host}/{BUCKET}/readme/index.html")
