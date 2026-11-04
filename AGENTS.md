# Policy website

Only edit this repository. Android source in the sibling PrivacyWatchdog repository is read-only evidence, not deployed content.

## Editing and generation

- `_locales/en.json` is the content schema. Every locale must have the same keys and section IDs.
- `_locales/manifest.json` lists languages, native names, direction and review status.
- Edit JSON, `_tools/build.py`, or `assets/policy.css`, not generated HTML.
- Run `python _tools\build.py` and `python _tools\test_site.py` from this repository.
- English is root `index.html`; other languages use their BCP 47 folder. Keep `CNAME` unchanged.
- `_locales` and `_tools` are excluded by normal GitHub Pages/Jekyll underscore-directory rules. `_config.yml` also explicitly excludes sources, instructions and test artifacts. No Actions infrastructure is needed.
- There are no runtime scripts, external fonts, analytics or translation services.
- Language selection must remain explicit and work without JavaScript.

## Review and publishing

All 18 locales, including revised English, are drafts awaiting owner/legal review. Non-English text is machine-assisted translation awaiting native-speaker review; no professional review has been performed. English is not assigned legal precedence. Review the meaning of every section, especially retention, external disclosures and deletion.

All generated pages deliberately have `noindex, nofollow` pending review. This is a crawler request, not access control. Do not remove it or publish, commit or push without owner approval. Do not claim that a local output is deployed.

The policy covers only the current Android Play implementation, not experimental Gold or iOS. Check source again before release; `_tools/policy-evidence.json` records the source snapshot and limitations. Do not infer app behavior from old policy text or generic data-model comments.

## Validation

The test runner checks generated-file freshness, all locale fields and local links, language/direction, landmarks, native keyboard navigation with JavaScript disabled, touch sizes, no external requests, 320px reflow, computed text contrast and 200% text resizing. It saves results, two narrow screenshots and a Chromium accessibility-tree sample under ignored `_tools/artifacts`.

These are automated checks, not WCAG certification, native-speaker/legal review, or human screen-reader testing. Translated-text completeness checks do not establish translation quality. Existing Python Playwright and either its Chromium installation or installed Microsoft Edge are required; the runner uses Edge if the bundled executable is missing. Pillow validates screenshot dimensions. Do not add dependencies unless a chosen check fails because one is missing.
