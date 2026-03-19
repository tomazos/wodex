import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const commonmark = require("commonmark");
const { createRenderer } = require("../static/js/wodex_markdown.js");

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, "..");
const specPath = path.join(projectRoot, "third_party", "commonmark-spec", "0.31.2", "spec.txt");

function extractSpecTests(data) {
  const examples = [];
  let currentSection = "";
  let exampleNumber = 0;
  const tests = String(data || "")
    .replace(/\r\n?/g, "\n")
    .replace(/^<!-- END TESTS -->(.|[\n])*/m, "");

  tests.replace(
    /^`{32} example\n([\s\S]*?)^\.\n([\s\S]*?)^`{32}$|^#{1,6} *(.*)$/gm,
    (_match, markdownSubmatch, htmlSubmatch, sectionSubmatch) => {
      if (sectionSubmatch) {
        currentSection = sectionSubmatch;
        return "";
      }
      exampleNumber += 1;
      examples.push({
        markdown: markdownSubmatch.replace(/\u2192/g, "\t"),
        html: htmlSubmatch.replace(/\u2192/g, "\t"),
        section: currentSection,
        number: exampleNumber,
      });
      return "";
    },
  );

  return examples;
}

function parseArgs(argv) {
  const filters = {
    example: null,
    section: null,
    limit: null,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--example") {
      filters.example = Number(argv[index + 1]);
      index += 1;
      continue;
    }
    if (arg === "--section") {
      filters.section = String(argv[index + 1] || "");
      index += 1;
      continue;
    }
    if (arg === "--limit") {
      filters.limit = Number(argv[index + 1]);
      index += 1;
      continue;
    }
  }

  return filters;
}

function formatFailure(test, actualHtml) {
  return [
    `Example ${test.number} (${test.section})`,
    "--- markdown ---",
    test.markdown,
    "--- expected ---",
    test.html,
    "--- actual ---",
    actualHtml,
  ].join("\n");
}

const filters = parseArgs(process.argv.slice(2));
const renderer = createRenderer({ commonmark });
const tests = extractSpecTests(fs.readFileSync(specPath, "utf8"))
  .filter((test) => (filters.example === null ? true : test.number === filters.example))
  .filter((test) =>
    filters.section === null
      ? true
      : test.section.toLowerCase().includes(filters.section.toLowerCase()),
  );

const limitedTests = filters.limit === null ? tests : tests.slice(0, filters.limit);

let passed = 0;
const failures = [];

for (const test of limitedTests) {
  const actualHtml = renderer.renderCommonMarkHtml(test.markdown);
  if (actualHtml === test.html) {
    passed += 1;
    continue;
  }
  failures.push(formatFailure(test, actualHtml));
}

console.log(`CommonMark spec: ${path.relative(projectRoot, specPath)}`);
console.log(`Examples run: ${limitedTests.length}`);
console.log(`Passed: ${passed}`);
console.log(`Failed: ${failures.length}`);

if (failures.length > 0) {
  const previewCount = Math.min(10, failures.length);
  console.log("");
  console.log(`Showing first ${previewCount} failure(s):`);
  for (let index = 0; index < previewCount; index += 1) {
    console.log("");
    console.log(failures[index]);
  }
  process.exitCode = 1;
}
