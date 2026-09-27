"""Browser-side helpers: connect to a sandbox over CDP, number clickable elements, record traffic.

The agent never clicks by pixel coordinates. Each step we tag visible interactive elements
with data-sk ids, draw numbered labels for the screenshot, and the model answers with an id.
"""
import base64

from playwright.async_api import async_playwright

from . import db

# Traffic worth keeping: the page's own API calls and form posts, not assets.
KEEP_TYPES = {"xhr", "fetch", "document", "eventsource", "websocket"}
NOISE_HOSTS = (
    "google-analytics", "googletagmanager", "doubleclick", "segment.", "sentry.", "mixpanel", "amplitude",
    "hotjar", "intercom", "facebook.net", "clarity.ms", "datadoghq", "posthog", "stripe.network", "fullstory",
    "launchdarkly", "cloudflareinsights", "newrelic", "bugsnag", "logrocket",
)
MAX_BODY = 20_000

MARK_JS = """
() => {
  document.querySelectorAll('[data-sk-label]').forEach(e => e.remove());
  document.querySelectorAll('[data-sk]').forEach(e => e.removeAttribute('data-sk'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], '
    + '[role=tab], [role=menuitem], [role=checkbox], [role=switch], [role=option], [role=combobox], [onclick], [contenteditable=true]';
  const vw = innerWidth, vh = innerHeight, out = [], seen = new Set();
  // Many apps build menus from plain divs (e.g. floating-ui portals), so also take the outermost
  // element of any pointer-cursor region.
  const pointer = [...document.querySelectorAll('div, span, li, p, img, svg, label')].filter(e => {
    if (getComputedStyle(e).cursor !== 'pointer') return false;
    const p = e.parentElement;
    return !p || getComputedStyle(p).cursor !== 'pointer';
  });
  const candidates = [...new Set([...document.querySelectorAll(sel), ...pointer])];
  for (const e of candidates) {
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const top = document.elementFromPoint(Math.min(Math.max(cx, 0), vw - 1), Math.min(Math.max(cy, 0), vh - 1));
    if (top && !e.contains(top) && !top.contains(e)) continue;  // covered by something else
    if ([...seen].some(s => s.contains(e) && s.tagName !== 'FORM')) continue;  // nested inside an already-marked element
    seen.add(e);
    const id = out.length + 1;
    e.setAttribute('data-sk', id);
    const text = (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('placeholder')
                  || e.getAttribute('title') || e.getAttribute('alt') || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    out.push({id, tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || e.getAttribute('role') || '',
              text, href: e.getAttribute('href') || ''});
    const l = document.createElement('div');
    l.setAttribute('data-sk-label', '1');
    l.textContent = id;
    Object.assign(l.style, {position: 'fixed', left: Math.max(r.left, 0) + 'px', top: Math.max(r.top - 14, 0) + 'px',
      background: '#e5153b', color: '#fff', font: 'bold 11px/13px Arial', padding: '0 3px', borderRadius: '3px',
      zIndex: 2147483647, pointerEvents: 'none'});
    document.body.appendChild(l);
    if (out.length >= 120) break;
  }
  return out;
}
"""
UNMARK_JS = "() => document.querySelectorAll('[data-sk-label]').forEach(e => e.remove())"


class SandboxBrowser:
    """A CDP connection to one sandbox's Chromium, with network recording."""

    def __init__(self, job_id, cdp):
        self.job_id, self.cdp = job_id, cdp
        self.step = 0
        self._pw = self.browser = self.context = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.connect_over_cdp(f"http://{self.cdp}")
        self.context = self.browser.contexts[0]
        self.context.on("response", self._on_response)
        return self

    async def __aexit__(self, *exc):
        # Disconnect only; the sandbox (and its logged-in session) stays alive.
        try:
            await self._pw.stop()
        except Exception:
            pass

    @property
    def page(self):
        pages = [p for p in self.context.pages if not p.is_closed()]
        return pages[-1]  # newest tab is the one the user just opened

    async def _on_response(self, response):
        # Must never raise: an exception here would be lost in the event emitter and the request dropped.
        try:
            await self._record(response)
        except Exception as e:
            db.add_event(self.job_id, "recorder_error", {"url": response.url[:200], "error": str(e)[:200]})

    async def _record(self, response):
        req = response.request
        if req.resource_type not in KEEP_TYPES or any(h in req.url for h in NOISE_HOSTS):
            return
        if req.url.startswith("data:") or req.url.startswith("chrome"):
            return
        try:
            body = await response.text() if response.status not in (204, 304) and not (300 <= response.status < 400) else ""
        except Exception:
            body = ""
        try:
            req_headers = await req.all_headers()
            resp_headers = await response.all_headers()
        except Exception:
            req_headers, resp_headers = req.headers, response.headers
        raw = req.post_data_buffer or b""  # post_data raises on non-UTF-8 bodies
        req_body = raw.decode("utf-8", errors="replace")
        db.add_request(
            self.job_id, self.step,
            method=req.method, url=req.url, resource_type=req.resource_type,
            req_headers=req_headers, req_body=req_body[:MAX_BODY],
            status=response.status, resp_headers=resp_headers, resp_body=body[:MAX_BODY],
        )

    async def observe(self, include_text=False, max_text=6000):
        """Screenshot with numbered marks plus the matching element list.

        include_text adds the whole page's visible text (not just the viewport), so an agent can read
        content that doesn't fit on one screen instead of depending on pixel-perfect scrolling.
        """
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        elements = await page.evaluate(MARK_JS)
        shot = await page.screenshot(type="jpeg", quality=70)
        await page.evaluate(UNMARK_JS)
        obs = {
            "url": page.url,
            "title": await page.title(),
            "elements": elements,
            "screenshot_b64": base64.b64encode(shot).decode(),
        }
        if include_text:
            text = await page.evaluate("() => (document.body.innerText || '').replace(/\\s+/g, ' ').trim()")
            obs["text"] = text[:max_text] + (" …(truncated)" if len(text) > max_text else "")
        return obs

    async def plain_screenshot(self):
        return base64.b64encode(await self.page.screenshot(type="jpeg", quality=60)).decode()

    async def settle(self):
        try:
            await self.page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass
        await self.page.wait_for_timeout(800)

    async def cookies(self):
        return await self.context.cookies()
