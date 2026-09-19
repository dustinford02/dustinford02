// Minimal, dependency-free link checker for the profile.
//   node .cursor/check-links.mjs [file ...]   (defaults to README.md)
//
// Extracts http(s) links from the given Markdown files and requests each one,
// reporting the HTTP status. Some sites (for example LinkedIn) deliberately
// block automated requests and answer with 999/403; those hosts are reported as
// SKIP rather than failures so a human can confirm them manually. Exits non-zero
// only when a checkable link is actually broken.
import { readFile } from "node:fs/promises";

const files = process.argv.slice(2);
if (files.length === 0) files.push("README.md");

// Hosts that block automated traffic; report but do not fail the run.
const manualReviewHosts = ["linkedin.com", "www.linkedin.com"];

const linkPattern = /\bhttps?:\/\/[^\s)<>\]}"']+/g;

function collectLinks(markdown) {
  const found = markdown.match(linkPattern) ?? [];
  return found.map((url) => url.replace(/[.,;:]+$/, ""));
}

async function checkUrl(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 25000);
  const headers = {
    "user-agent":
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36",
    accept: "text/html,application/xhtml+xml,*/*",
  };
  try {
    let res = await fetch(url, { method: "HEAD", redirect: "follow", signal: controller.signal, headers });
    if (res.status === 405 || res.status === 501) {
      res = await fetch(url, { method: "GET", redirect: "follow", signal: controller.signal, headers });
    }
    return { status: res.status, ok: res.ok };
  } catch (err) {
    return { status: 0, ok: false, error: err.name === "AbortError" ? "timeout" : err.message };
  } finally {
    clearTimeout(timeout);
  }
}

const urls = new Set();
for (const file of files) {
  const markdown = await readFile(file, "utf8");
  for (const url of collectLinks(markdown)) urls.add(url);
}

let failures = 0;
for (const url of [...urls].sort()) {
  const host = (() => {
    try {
      return new URL(url).hostname;
    } catch {
      return "";
    }
  })();
  if (manualReviewHosts.includes(host)) {
    console.log(`SKIP  (manual review — bot-protected)  ${url}`);
    continue;
  }
  const { status, ok, error } = await checkUrl(url);
  if (ok) {
    console.log(`OK    ${status}  ${url}`);
  } else {
    failures += 1;
    console.log(`FAIL  ${status || error}  ${url}`);
  }
}

console.log(`\nChecked ${urls.size} link(s): ${failures} failure(s).`);
process.exit(failures > 0 ? 1 : 0);
