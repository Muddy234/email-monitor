# Blog — How to Add & Update Posts

The Clarion AI blog is a static site, prerendered at deploy time by `scripts/build-blog.mjs` (Node + `marked`). Posts are authored as Markdown files; the build step turns them into HTML, an RSS feed, and a regenerated sitemap.

You **never** edit the generated HTML directly. You edit the Markdown source + the manifest, commit, and Vercel rebuilds on push.

---

## TL;DR — Add a new post

1. Create `web/blog/posts/<slug>.md` with YAML front-matter (template below).
2. Add a matching entry to `web/blog/posts/index.json`.
3. Run `npm run build` locally to verify (optional but recommended).
4. Commit & push. Vercel builds and deploys automatically.

That's it. Steps 1 and 2 are the only authoring work; everything else is automated.

---

## File layout

```
repo-root/
├── package.json                # marked dep, build scripts
├── vercel.json                 # buildCommand: npm run build, outputDirectory: web
├── scripts/
│   └── build-blog.mjs          # the build script
├── templates/
│   ├── blog-list.html          # /blog/ index template
│   └── blog-post.html          # single-post template
└── web/
    └── blog/
        ├── README.md           # this file
        ├── posts/
        │   ├── index.json      # ← MANIFEST — add your post here
        │   ├── hello-clarion.md
        │   └── <your-slug>.md  # ← MARKDOWN — drop your post here
        ├── index.html          # GENERATED — do not hand-edit
        ├── <slug>.html         # GENERATED — do not hand-edit
        └── feed.xml            # GENERATED — do not hand-edit
```

Anything labelled "GENERATED" gets overwritten on every build. `web/sitemap.xml` is also regenerated on each build.

---

## Slug rules

The slug is the URL path (`/blog/<slug>`) and the markdown filename (`<slug>.md`).

- Lowercase letters, numbers, hyphens, underscores only — regex: `^[A-Za-z0-9_-]+$`
- Keep it short and descriptive: `outlook-rules-vs-ai`, `q2-2026-update`
- The slug in `index.json` must **exactly match** the markdown filename (minus `.md`)
- Once published, **don't change a slug** — you'll break inbound links and lose SEO equity. If you really must rename, set up a redirect in `vercel.json` first.

---

## Step 1 — Create the markdown file

Path: `web/blog/posts/<slug>.md`

```markdown
---
title: Your Post Title Here
date: 2026-05-15
author: Clarion AI Team
excerpt: One or two sentences shown on the blog index card and used as the meta description for SEO + social previews.
---

Opening paragraph. Use plain Markdown — GitHub Flavored Markdown is enabled.

## Section heading

- Bullet lists work
- **Bold** and *italic* work
- [Links work](https://www.clarion-ai.app)

> Block quotes work.

```python
# Code blocks work
print("hello")
```

Closing thoughts. Keep paragraphs scannable.
```

### Front-matter fields

| Field     | Required | Notes                                                                 |
|-----------|----------|-----------------------------------------------------------------------|
| `title`   | yes      | Used in `<h1>`, `<title>`, OG tags, JSON-LD, RSS                      |
| `date`    | yes      | `YYYY-MM-DD` (treated as UTC midnight). Drives sort order             |
| `author`  | no       | Defaults to `Clarion AI Team`                                         |
| `excerpt` | yes      | 1–2 sentences. Used as `<meta name="description">` + OG description   |

The manifest entry takes precedence over front-matter for the same field, so you only need to put each value in one place — front-matter is the recommended home.

### Markdown notes

- GFM is on (`gfm: true`), line breaks off (`breaks: false`) — use blank lines between paragraphs.
- Inline HTML in the body is passed through to the renderer as-is. Avoid it unless necessary; it's untrusted-feeling and will confuse anyone reading the markdown later.
- Images: drop them in `web/blog/img/` (create the folder if needed) and reference as `/blog/img/your-image.jpg`. Always include alt text.
- Headings inside the body should start at `##` — `#` is reserved for the post title (rendered from front-matter).

---

## Step 2 — Update the manifest

File: `web/blog/posts/index.json`

This is a **JSON array** of post entries. Add a new object to the array:

```json
[
  {
    "slug": "hello-clarion",
    "title": "Hello from Clarion AI",
    "excerpt": "We're starting a blog...",
    "date": "2026-05-01",
    "author": "Clarion AI Team",
    "tags": ["announcements"]
  },
  {
    "slug": "your-new-slug",
    "title": "Your Post Title Here",
    "excerpt": "One or two sentences shown on the index card.",
    "date": "2026-05-15",
    "author": "Clarion AI Team",
    "tags": ["product-updates"]
  }
]
```

Manifest fields:

| Field     | Required | Notes                                                          |
|-----------|----------|----------------------------------------------------------------|
| `slug`    | yes      | Must match the `.md` filename                                  |
| `title`   | no\*     | Falls back to front-matter `title`, then to `slug`             |
| `excerpt` | no\*     | Falls back to front-matter `excerpt`                           |
| `date`    | no\*     | Falls back to front-matter `date`                              |
| `author`  | no       | Falls back to front-matter `author`, then to `Clarion AI Team` |
| `tags`    | no       | Currently unused for rendering; reserved for future tag pages  |

\* Required *somewhere* — either in the manifest or in the markdown front-matter.

Validate the JSON before committing — a trailing comma or missing quote will break the build.

---

## Step 3 — Build & verify locally (optional but recommended)

From the repo root:

```powershell
npm install        # one-time, installs marked into node_modules/
npm run build      # runs scripts/build-blog.mjs
```

Expected console output:

```
[blog] building...
[blog] wrote .../web/blog/<slug>.html
[blog] wrote .../web/blog/index.html
[blog] wrote .../web/blog/feed.xml
[blog] wrote .../web/sitemap.xml
[blog] done: N post(s)
```

Open `web/blog/index.html` and `web/blog/<your-slug>.html` directly in a browser to spot-check rendering. Posts are sorted **newest first** by `date`.

---

## Step 4 — Commit & deploy

```powershell
git add web/blog/posts/<your-slug>.md web/blog/posts/index.json
git commit -m "blog: add <slug>"
git push
```

Vercel detects the push, runs `npm install && npm run build`, and serves `web/`. The new post is live at `https://www.clarion-ai.app/blog/<slug>` within ~1 minute.

You **don't** need to commit the generated files (`web/blog/index.html`, `web/blog/<slug>.html`, `web/blog/feed.xml`, `web/sitemap.xml`) — Vercel regenerates them on every deploy. They're checked in only as a convenience for direct previews.

---

## Updating an existing post

1. Edit the `.md` file in `web/blog/posts/`.
2. If you changed the title / excerpt / author / date, update the matching entry in `index.json` (or just delete those fields from the manifest so front-matter wins).
3. **Don't change the `slug`** unless you also add a redirect.
4. Commit & push.

If the edit is substantial, consider bumping the `date` to today so the post resurfaces at the top of the index. For minor copy fixes, leave the original date alone.

---

## Deleting a post

1. Delete `web/blog/posts/<slug>.md`.
2. Remove the entry from `web/blog/posts/index.json`.
3. Commit & push.

The generated `web/blog/<slug>.html` won't be regenerated, but it'll still exist on Vercel until the next deploy overwrites it. To fully remove it, also delete the stale `web/blog/<slug>.html` from the repo so it's gone from the deployed `web/` directory.

If the post had any inbound traffic, add a redirect in `vercel.json`:

```json
{
  "redirects": [
    { "source": "/blog/<old-slug>", "destination": "/blog/", "permanent": true }
  ]
}
```

---

## What the build produces (for reference)

| Output                      | Purpose                                              |
|-----------------------------|------------------------------------------------------|
| `web/blog/index.html`       | Post list page (cards, sorted newest first)         |
| `web/blog/<slug>.html`      | One static page per post, with OG + JSON-LD         |
| `web/blog/feed.xml`         | RSS 2.0 feed (linked from `<head>` on the list page) |
| `web/sitemap.xml`           | Regenerated; includes static URLs + every blog URL  |

Each post page includes:
- Open Graph tags for Slack/Twitter/LinkedIn previews
- `BlogPosting` JSON-LD structured data for Google
- Canonical URL pointing at `/blog/<slug>`
- `<time datetime="...">` with full ISO 8601 timestamp

---

## Troubleshooting

**Build fails with `Invalid slug in manifest`**
The `slug` field contains characters outside `[A-Za-z0-9_-]`. Lowercase letters/numbers/hyphens only.

**Build fails with `ENOENT: no such file ... <slug>.md`**
You added an entry to `index.json` but didn't create the `.md` file (or the slug in the manifest doesn't match the filename).

**Build fails with JSON parse error**
Trailing comma, unquoted key, or smart quotes in `index.json`. Run it through a JSON validator.

**Post shows up but is in the wrong order**
Order is by `date` descending. Check the `date` field on the post — both formats `YYYY-MM-DD` and full ISO timestamps work.

**Markdown formatting looks wrong**
Make sure there's a blank line between paragraphs (line breaks alone don't render as `<br>` because `breaks: false` is set in `marked`).

**`npm run build` works locally but Vercel build fails**
Confirm Vercel "Root Directory" is set to repo root (not `web/`). The root-level `vercel.json` and `package.json` must be visible to the build.

---

## Adding a new author

No code change needed. Just put the new name in the post's front-matter (`author: Jane Doe`). The byline shows up automatically on the post page, the index card, and the RSS feed.

---

## Changing the site-wide blog metadata

Edit constants near the top of `scripts/build-blog.mjs`:

```js
const SITE_URL = "https://www.clarion-ai.app";
const SITE_TITLE = "Clarion AI Blog";
const SITE_DESCRIPTION = "Practical guides, product updates...";
const SITE_LANGUAGE = "en-US";
```

Edit the `STATIC_URLS` array in the same file to add/remove non-blog pages from the sitemap.

---

## Quick checklist before publishing

- [ ] Slug is lowercase, kebab-case, ASCII
- [ ] `<slug>.md` exists in `web/blog/posts/`
- [ ] Front-matter has `title`, `date`, `excerpt`
- [ ] Entry added to `web/blog/posts/index.json` with matching `slug`
- [ ] `npm run build` runs cleanly with no warnings
- [ ] Spot-checked the rendered HTML in a browser
- [ ] Committed both the `.md` file and `index.json`
