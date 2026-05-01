/**
 * Clarion AI blog build
 * ----------------------------------------------------------------
 * Reads markdown posts from web/blog/posts/<slug>.md plus a manifest
 * at web/blog/posts/index.json and emits:
 *   - web/blog/index.html             (post-list page, fully rendered)
 *   - web/blog/<slug>.html            (one static page per post)
 *   - web/blog/feed.xml               (RSS 2.0)
 *   - web/sitemap.xml                 (regenerated, includes blog URLs)
 *
 * Run: `npm run build` (Node >= 18, ESM).
 */

import { readFile, readdir, writeFile, mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";

// --- paths ---
const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const TEMPLATES_DIR = join(ROOT, "templates");
const WEB_DIR = join(ROOT, "web");
const BLOG_DIR = join(WEB_DIR, "blog");
const POSTS_DIR = join(BLOG_DIR, "posts");

// --- site constants ---
const SITE_URL = "https://www.clarion-ai.app";
const SITE_TITLE = "Clarion AI Blog";
const SITE_DESCRIPTION = "Practical guides, product updates, and ideas on email productivity from the Clarion AI team.";
const SITE_LANGUAGE = "en-US";

// Static (non-blog) URLs included in sitemap.xml.
const STATIC_URLS = [
    { loc: `${SITE_URL}/`,                     changefreq: "weekly",  priority: "1.0" },
    { loc: `${SITE_URL}/privacy.html`,         changefreq: "monthly", priority: "0.5" },
    { loc: `${SITE_URL}/terms.html`,           changefreq: "monthly", priority: "0.5" },
    { loc: `${SITE_URL}/demo-animated.html`,   changefreq: "monthly", priority: "0.7" },
];

// --- helpers ---
function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}

// Conservative JSON-string escaper for inline JSON-LD.
function escapeJson(s) {
    return JSON.stringify(String(s ?? "")).slice(1, -1);
}

// Parse YAML-ish front-matter (key: value lines between --- markers).
function parseFrontmatter(raw) {
    const m = /^---\s*\r?\n([\s\S]*?)\r?\n---\s*\r?\n?([\s\S]*)$/.exec(raw);
    if (!m) return { meta: {}, body: raw };
    const meta = {};
    for (const line of m[1].split(/\r?\n/)) {
        const kv = /^([A-Za-z0-9_-]+)\s*:\s*(.*)$/.exec(line);
        if (!kv) continue;
        let v = kv[2].trim();
        if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
            v = v.slice(1, -1);
        }
        meta[kv[1]] = v;
    }
    return { meta, body: m[2] };
}

function formatDateDisplay(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });
}

function toIsoDate(iso) {
    if (!iso) return "";
    // Ensure full RFC 3339 timestamp; treat date-only as UTC midnight.
    if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return `${iso}T00:00:00Z`;
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toISOString();
}

function toRfc822(iso) {
    if (!iso) return "";
    const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00Z` : iso);
    return isNaN(d.getTime()) ? iso : d.toUTCString();
}

function applyTemplate(template, replacements) {
    let out = template;
    for (const [key, value] of Object.entries(replacements)) {
        out = out.split(`{{${key}}}`).join(value);
    }
    return out;
}

async function ensureDir(p) {
    await mkdir(p, { recursive: true });
}

// --- main build ---
async function build() {
    console.log("[blog] building...");

    // Load manifest.
    const manifestRaw = await readFile(join(POSTS_DIR, "index.json"), "utf8");
    const manifest = JSON.parse(manifestRaw);
    if (!Array.isArray(manifest)) throw new Error("posts/index.json must be a JSON array");

    // Load templates.
    const listTpl = await readFile(join(TEMPLATES_DIR, "blog-list.html"), "utf8");
    const postTpl = await readFile(join(TEMPLATES_DIR, "blog-post.html"), "utf8");

    // Configure marked: GitHub Flavored Markdown, no embedded HTML pass-through.
    marked.setOptions({ gfm: true, breaks: false });

    // Read & sort posts (newest first).
    const posts = [];
    for (const entry of manifest) {
        if (!entry.slug || !/^[A-Za-z0-9_-]+$/.test(entry.slug)) {
            throw new Error(`Invalid slug in manifest: ${JSON.stringify(entry)}`);
        }
        const mdPath = join(POSTS_DIR, `${entry.slug}.md`);
        const raw = await readFile(mdPath, "utf8");
        const { meta, body } = parseFrontmatter(raw);

        // Manifest entry takes precedence; markdown front-matter fills gaps.
        const post = {
            slug: entry.slug,
            title: entry.title || meta.title || entry.slug,
            excerpt: entry.excerpt || meta.excerpt || "",
            date: entry.date || meta.date || "",
            author: entry.author || meta.author || "Clarion AI Team",
            tags: entry.tags || [],
            bodyHtml: marked.parse(body),
        };
        posts.push(post);
    }
    posts.sort((a, b) => (b.date || "").localeCompare(a.date || ""));

    await ensureDir(BLOG_DIR);

    // --- Render each post page ---
    for (const post of posts) {
        const html = applyTemplate(postTpl, {
            TITLE: escapeHtml(post.title),
            EXCERPT: escapeHtml(post.excerpt),
            AUTHOR: escapeHtml(post.author),
            SLUG: post.slug,
            DATE_ISO: toIsoDate(post.date),
            DATE_DISPLAY: escapeHtml(formatDateDisplay(post.date)),
            BODY_HTML: post.bodyHtml,
            TITLE_JSON: escapeJson(post.title),
            EXCERPT_JSON: escapeJson(post.excerpt),
            AUTHOR_JSON: escapeJson(post.author),
        });
        const outPath = join(BLOG_DIR, `${post.slug}.html`);
        await writeFile(outPath, html, "utf8");
        console.log(`[blog] wrote ${outPath}`);
    }

    // --- Render the list page ---
    const cards = posts.map(p => `
                    <article class="lp-blog-card">
                        <a class="lp-blog-card__link" href="/blog/${p.slug}">
                            <div class="lp-blog-card__meta">
                                <time datetime="${toIsoDate(p.date)}">${escapeHtml(formatDateDisplay(p.date))}</time>
                                ${p.author ? `<span class="lp-blog-card__author">· ${escapeHtml(p.author)}</span>` : ""}
                            </div>
                            <h2 class="lp-blog-card__title">${escapeHtml(p.title)}</h2>
                            ${p.excerpt ? `<p class="lp-blog-card__excerpt">${escapeHtml(p.excerpt)}</p>` : ""}
                            <span class="lp-blog-card__cta">Read post &rarr;</span>
                        </a>
                    </article>`).join("\n");

    const listHtml = posts.length
        ? listTpl.replace("<!-- POST_CARDS -->", cards)
        : listTpl.replace("<!-- POST_CARDS -->", `<p class="lp-blog-empty">No posts yet — check back soon.</p>`);
    const listOut = join(BLOG_DIR, "index.html");
    await writeFile(listOut, listHtml, "utf8");
    console.log(`[blog] wrote ${listOut}`);

    // --- RSS feed ---
    const rssItems = posts.map(p => `
        <item>
            <title>${escapeHtml(p.title)}</title>
            <link>${SITE_URL}/blog/${p.slug}</link>
            <guid isPermaLink="true">${SITE_URL}/blog/${p.slug}</guid>
            <pubDate>${toRfc822(p.date)}</pubDate>
            <author>noreply@clarion-ai.app (${escapeHtml(p.author)})</author>
            <description>${escapeHtml(p.excerpt)}</description>
        </item>`).join("");

    const lastBuildDate = posts.length ? toRfc822(posts[0].date) : new Date().toUTCString();
    const rss = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
    <channel>
        <title>${escapeHtml(SITE_TITLE)}</title>
        <link>${SITE_URL}/blog/</link>
        <description>${escapeHtml(SITE_DESCRIPTION)}</description>
        <language>${SITE_LANGUAGE}</language>
        <lastBuildDate>${lastBuildDate}</lastBuildDate>
        <atom:link href="${SITE_URL}/blog/feed.xml" rel="self" type="application/rss+xml"/>${rssItems}
    </channel>
</rss>
`;
    const rssOut = join(BLOG_DIR, "feed.xml");
    await writeFile(rssOut, rss, "utf8");
    console.log(`[blog] wrote ${rssOut}`);

    // --- Sitemap (regenerated) ---
    const blogUrls = [
        { loc: `${SITE_URL}/blog/`, changefreq: "weekly", priority: "0.8" },
        ...posts.map(p => ({
            loc: `${SITE_URL}/blog/${p.slug}`,
            lastmod: toIsoDate(p.date).slice(0, 10),
            changefreq: "monthly",
            priority: "0.6",
        })),
    ];
    const allUrls = [...STATIC_URLS, ...blogUrls];

    const sitemap = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${allUrls.map(u => `    <url>
        <loc>${u.loc}</loc>${u.lastmod ? `\n        <lastmod>${u.lastmod}</lastmod>` : ""}
        <changefreq>${u.changefreq}</changefreq>
        <priority>${u.priority}</priority>
    </url>`).join("\n")}
</urlset>
`;
    const sitemapOut = join(WEB_DIR, "sitemap.xml");
    await writeFile(sitemapOut, sitemap, "utf8");
    console.log(`[blog] wrote ${sitemapOut}`);

    console.log(`[blog] done: ${posts.length} post(s)`);
}

build().catch(err => {
    console.error("[blog] build failed:", err);
    process.exit(1);
});
