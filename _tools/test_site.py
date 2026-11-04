"""Local static and Chromium checks; no dependency downloads or external requests."""
from collections import Counter
from contextlib import ExitStack
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

from PIL import Image
from playwright.sync_api import Error, sync_playwright

from build import EXPECTED, ROOT, SECTIONS, build, page_path

ARTIFACTS = ROOT / "_tools" / "artifacts"


class Document(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.tags = []
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def static_checks(manifest, texts):
    docs = {code: Document(page_path(code)) for code in EXPECTED}
    checks = Counter()
    for locale in manifest["locales"]:
        code = locale["code"]
        doc = docs[code]
        tags = doc.tags
        root = next(attrs for tag, attrs in tags if tag == "html")
        assert root == {"lang": code, "dir": locale["dir"]}, code
        ids = [a["id"] for _, a in tags if "id" in a]
        assert len(ids) == len(set(ids)), f"{code}: duplicate IDs"
        assert set(SECTIONS).issubset(ids)
        assert any(t == "meta" and a.get("name") == "robots" and
                   a.get("content") == "noindex, nofollow" for t, a in tags)
        assert Counter(t for t, _ in tags)["h1"] == 1
        for landmark in ("header", "main", "footer"):
            assert Counter(t for t, _ in tags)[landmark] == 1
        assert Counter(t for t, _ in tags)["h2"] == len(SECTIONS)
        assert not any(t in ("script", "iframe", "table") for t, _ in tags)
        current = [a for _, a in tags if a.get("aria-current") == "page"]
        assert len(current) == 1 and current[0]["lang"] == code
        for tag, attrs in tags:
            for ref in attrs.get("aria-labelledby", "").split():
                assert ref in ids, f"{code}: invalid label reference"
            for attr in ("href", "src"):
                if attr not in attrs:
                    continue
                url = urlsplit(attrs[attr])
                if url.scheme == "mailto":
                    assert url.path == "admin@maonoautonomous.com"
                    continue
                assert not url.scheme and not url.netloc, f"{code}: external resource"
                target = (page_path(code).parent / unquote(url.path)).resolve() if url.path else page_path(code)
                assert target.is_relative_to(ROOT) and target.is_file(), f"{code}: broken {attrs[attr]}"
                if url.fragment:
                    target_doc = doc if target == page_path(code) else Document(target)
                    assert any(a.get("id") == url.fragment for _, a in target_doc.tags)
                checks["valid_local_links"] += 1
        checks["complete_locales"] += 1
        checks["translated_policy_sections"] += len(texts[code]["sections"])
    assert (ROOT / "CNAME").read_text().strip() == "privacywatchdoglegal.maonoautonomous.com"
    return checks


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


CONTRAST = """() => {
  const rgb = s => (s.match(/[\\d.]+/g) || []).map(Number);
  const lum = c => c.slice(0,3).map(v => {
    v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
  }).reduce((a,v,i) => a + v * [.2126,.7152,.0722][i], 0);
  const ratio = (a,b) => (Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
  const background = el => {
    let c = [255,255,255];
    const chain = [];
    for (; el; el = el.parentElement) chain.unshift(el);
    for (const p of chain) {
      const b = rgb(getComputedStyle(p).backgroundColor);
      const alpha = b.length > 3 ? b[3] : 1;
      c = c.map((v,i) => b[i] * alpha + v * (1-alpha));
    }
    return c;
  };
  const results = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!el.checkVisibility() || ![...el.childNodes].some(n =>
      n.nodeType === Node.TEXT_NODE && n.textContent.trim())) continue;
    const s = getComputedStyle(el);
    const large = parseFloat(s.fontSize) >= 24 ||
      (parseFloat(s.fontSize) >= 18.66 && parseFloat(s.fontWeight) >= 700);
    results.push({tag: el.tagName, ratio: ratio(rgb(s.color),background(el)), required: large ? 3 : 4.5});
  }
  const focus = document.activeElement;
  const fs = getComputedStyle(focus);
  return {text: results, focus: {width: parseFloat(fs.outlineWidth), style: fs.outlineStyle,
    ratio: ratio(rgb(fs.outlineColor), background(focus))}};
}"""

REFLOW = """() => {
  const w = document.documentElement.clientWidth;
  const failures = [];
  if (document.documentElement.scrollWidth > w + 1) failures.push('document overflow');
  for (const el of document.querySelectorAll('body *')) {
    if (!el.checkVisibility() || el.matches('.skip:not(:focus)')) continue;
    const r = el.getBoundingClientRect();
    if (r.width && (r.left < -1 || r.right > w + 1)) failures.push(el.tagName + ' outside viewport');
    const s = getComputedStyle(el);
    if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1) failures.push(el.tagName + ' clipped horizontally');
    if (el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 1 &&
        ['hidden','clip'].includes(s.overflowY)) failures.push(el.tagName + ' clipped vertically');
  }
  return failures;
}"""


def browser_checks(manifest, texts, base):
    checks = Counter()
    forbidden = []
    print("Launching existing browser...", flush=True)
    with sync_playwright() as pw, ExitStack() as cleanup:
        try:
            browser = pw.chromium.launch()
        except Error as error:
            if "Executable doesn't exist" not in str(error):
                raise
            # Use an installed Chromium-based browser without downloading dependencies.
            browser = pw.chromium.launch(channel="msedge")
        browser_version = browser.version
        cleanup.callback(browser.close)
        print(f"Browser ready: {browser_version}", flush=True)
        # A non-English browser preference must never change the requested page.
        context = browser.new_context(java_script_enabled=False, locale="ja-JP",
                                      viewport={"width": 1280, "height": 900})
        cleanup.callback(context.close)

        def restrict(route):
            if not route.request.url.startswith(base + "/"):
                forbidden.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        context.route("**/*", restrict)
        context.set_default_timeout(15000)
        page = context.new_page()
        for index, locale in enumerate(manifest["locales"]):
            code = locale["code"]
            print(f"Checking {code}: semantics, keyboard, contrast, reflow...", flush=True)
            suffix = "/index.html" if code == "en" else f"/{code}/index.html"
            url = base + suffix
            page.goto("about:blank")
            response = page.goto(url)
            assert response.status == 200 and page.url == url
            assert page.locator("html").get_attribute("lang") == code
            assert page.locator("html").get_attribute("dir") == locale["dir"]
            assert page.locator("h1").inner_text() == texts[code]["title"]
            assert page.locator("main section").count() == len(SECTIONS)
            assert page.locator("main").inner_text().find(texts[code]["draft"]) >= 0
            assert page.locator("time").inner_text() == "2026-11-04"
            assert page.locator("time").get_attribute("dir") == "ltr"
            assert page.locator("#visibility + p bdi").get_attribute("dir") == "ltr"
            checks["no_browser_language_redirect"] += 1

            page.keyboard.press("Tab")
            assert page.locator(".skip").evaluate("(el) => el === document.activeElement")
            assert page.locator(".skip").bounding_box()["y"] >= 0
            focus = page.evaluate(CONTRAST)["focus"]
            assert focus["width"] >= 3 and focus["style"] != "none" and focus["ratio"] >= 3
            page.keyboard.press("Enter")
            assert page.url.endswith("#policy")
            assert page.locator("main").evaluate("(el) => el === document.activeElement")
            checks["keyboard_skip_navigation_no_js"] += 1

            page.goto("about:blank")
            page.goto(url)
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            assert page.locator("summary").evaluate("(el) => el === document.activeElement")
            assert not page.locator("details").evaluate("(el) => el.open")
            page.keyboard.press("Enter")
            assert page.locator("details").evaluate("(el) => el.open")
            assert page.locator(".languages a").count() == len(EXPECTED)
            for other in manifest["locales"]:
                link = page.locator(f'.languages a[lang="{other["code"]}"]')
                assert link.inner_text() == other["name"]
                assert link.get_attribute("hreflang") == other["code"]
                assert link.get_attribute("dir") == other["dir"]
                assert (link.get_attribute("aria-current") == "page") == (other["code"] == code)
                box = link.bounding_box()
                assert box["height"] >= 44 and box["width"] >= 44
                checks["touch_sized_language_links"] += 1
            summary_box = page.locator("summary").bounding_box()
            assert summary_box["height"] >= 44
            focus = page.evaluate(CONTRAST)["focus"]
            assert focus["width"] >= 3 and focus["ratio"] >= 3
            contrast = page.evaluate(CONTRAST)["text"]
            assert contrast and all(r["ratio"] >= r["required"] for r in contrast), code
            checks["computed_text_contrast_nodes"] += len(contrast)

            for width in (1280, 320):
                page.set_viewport_size({"width": width, "height": 900})
                for percent in (100, 200):
                    page.evaluate("(percent) => document.documentElement.style.setProperty('font-size', percent + '%', 'important')", percent)
                    assert page.locator("body").evaluate(
                        "(el) => parseFloat(getComputedStyle(el).fontSize)") >= 16 * percent / 100
                    failures = page.evaluate(REFLOW)
                    assert not failures, f"{code}/{width}/{percent}%: {failures}"
                    for target in ("summary", ".email", ".languages a"):
                        for item in page.locator(target).all():
                            assert item.bounding_box()["height"] >= 44
                    checks["viewport_text_resize_cases"] += 1
                    if code in ("en", "ar") and width == 320 and percent == 100:
                        image = ARTIFACTS / f"{code}-320.png"
                        page.screenshot(path=str(image), full_page=True)
                        with Image.open(image) as saved:
                            assert saved.width == 320 and saved.height > 900
                        checks["narrow_screenshots"] += 1
            page.evaluate("document.documentElement.style.setProperty('font-size', '100%', 'important')")
            page.set_viewport_size({"width": 1280, "height": 900})
            # Space also closes the native disclosure without application JavaScript.
            page.locator("summary").focus()
            page.keyboard.press("Space")
            assert not page.locator("details").evaluate("(el) => el.open")
            page.keyboard.press("Enter")
            next_index = (index + 1) % len(EXPECTED)
            for _ in range(next_index + 1):
                page.keyboard.press("Tab")
            expected_code = EXPECTED[next_index]
            assert page.locator(f'.languages a[lang="{expected_code}"]').evaluate(
                "(el) => el === document.activeElement")
            page.keyboard.press("Enter")
            page.wait_for_load_state()
            assert page.locator("html").get_attribute("lang") == expected_code
            checks["keyboard_language_switches_no_js"] += 1

            if code == "en":
                # CDP semantics only; not a claim of human screen-reader testing.
                page.goto(url)
                session = context.new_cdp_session(page)
                tree = session.send("Accessibility.getFullAXTree")
                roles = Counter(node.get("role", {}).get("value") for node in tree["nodes"])
                assert roles["main"] == 1 and roles["banner"] == 1 and roles["contentinfo"] == 1
                assert roles["heading"] == 12
                assert roles["navigation"] >= 1
                (ARTIFACTS / "en-accessibility-tree.json").write_text(
                    json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
                checks["chromium_accessibility_tree_samples"] += 1
                session.detach()
    assert not forbidden, f"Unexpected external requests: {forbidden}"
    checks["external_requests"] = len(forbidden)
    return checks, browser_version


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "summary.json").unlink(missing_ok=True)
    runtime = ARTIFACTS / "browser-runtime"
    runtime.mkdir(exist_ok=True)
    # Keep browser profiles and library scratch output inside this repository.
    original_env = {key: os.environ.get(key) for key in ("TMP", "TEMP", "TMPDIR")}
    original_tempdir = tempfile.tempdir
    for key in original_env:
        os.environ[key] = str(runtime)
    tempfile.tempdir = str(runtime)
    manifest, texts = build(check=True)
    checks = static_checks(manifest, texts)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        browser_results, version = browser_checks(manifest, texts, f"http://127.0.0.1:{server.server_port}")
        checks.update(browser_results)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
        tempfile.tempdir = original_tempdir
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for attempt in range(5):
            try:
                shutil.rmtree(runtime)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(1)
    report = {
        "status": "passed",
        "checks": dict(checks),
        "languages": list(EXPECTED),
        "browser": f"Playwright Chromium/installed Edge {version}; application JavaScript disabled",
        "limitations": [
            "Automated checks only, not WCAG conformance or human screen-reader testing.",
            "Computed contrast checks cover this page's solid backgrounds and visible text.",
            "200% root-font resizing is a text-resize stress check, not OS/browser zoom coverage.",
            "Content completeness does not establish native-speaker or legal translation quality.",
            "Automated checks do not establish owner, native-speaker or legal approval, or prove deployment."
        ]
    }
    (ARTIFACTS / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
