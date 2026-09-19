// Renders the profile README as GitHub-styled HTML.
//   node .cursor/preview.mjs            -> serve a live preview (re-renders on each request)
//   node .cursor/preview.mjs --build    -> write a static preview to dist/index.html
//
// The rendered HTML is sanitized before output so that raw HTML embedded in the
// Markdown cannot inject scripts or other active content into the preview. The
// preview is a local authoring aid, not a hosting target, so the allowlist stays
// deliberately small and matches the formatting a profile README needs.
import { createServer } from "node:http";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";
import sanitizeHtml from "sanitize-html";

const require = createRequire(import.meta.url);
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const readmePath = resolve(repoRoot, "README.md");
const cssPath = require.resolve("github-markdown-css/github-markdown-light.css");

const sanitizeOptions = {
  allowedTags: [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "a", "ul", "ol", "li", "blockquote",
    "code", "pre", "strong", "em", "del", "hr", "br",
    "table", "thead", "tbody", "tr", "th", "td",
    "img", "span", "sub", "sup",
  ],
  allowedAttributes: {
    a: ["href", "name", "title"],
    img: ["src", "alt", "title"],
    td: ["align"],
    th: ["align"],
  },
  allowedSchemes: ["http", "https", "mailto"],
  allowProtocolRelative: false,
  // Force safe rel/target on links so the preview never opener-leaks or executes
  // javascript: URLs that slipped past scheme filtering.
  transformTags: {
    a: (tagName, attribs) => ({
      tagName,
      attribs: { ...attribs, rel: "noopener noreferrer nofollow", target: "_blank" },
    }),
  },
};

async function renderPage() {
  const [markdown, css] = await Promise.all([
    readFile(readmePath, "utf8"),
    readFile(cssPath, "utf8"),
  ]);
  const rendered = marked.parse(markdown, { gfm: true, breaks: false });
  const body = sanitizeHtml(rendered, sanitizeOptions);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https: data:; style-src 'unsafe-inline'" />
<title>README preview</title>
<style>
${css}
body { margin: 0; background: #f6f8fa; }
.markdown-body { box-sizing: border-box; max-width: 980px; margin: 0 auto; padding: 45px; background: #ffffff; }
@media (max-width: 767px) { .markdown-body { padding: 15px; } }
</style>
</head>
<body>
<article class="markdown-body">
${body}
</article>
</body>
</html>`;
}

if (process.argv.includes("--build")) {
  const html = await renderPage();
  const outDir = resolve(repoRoot, "dist");
  await mkdir(outDir, { recursive: true });
  await writeFile(resolve(outDir, "index.html"), html);
  console.log(`Wrote ${resolve(outDir, "index.html")}`);
} else {
  const host = process.env.HOST ?? "0.0.0.0";
  const port = Number(process.env.PORT ?? 6419);
  const server = createServer(async (req, res) => {
    try {
      const html = await renderPage();
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(html);
    } catch (err) {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(`Failed to render README: ${err.message}`);
    }
  });
  server.listen(port, host, () => {
    console.log(`README preview running at http://${host}:${port}`);
  });
}
