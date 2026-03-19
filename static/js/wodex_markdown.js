(function (root, factory) {
  const exported = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  }
  if (root) {
    root.WodexMarkdown = exported;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const ALLOWED_TAGS = new Set([
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "ul",
  ]);

  const ALLOWED_ATTRIBUTES = new Map([
    ["a", new Set(["href", "title"])],
    ["code", new Set(["class"])],
    ["img", new Set(["src", "alt", "title"])],
    ["ol", new Set(["start"])],
  ]);

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function sanitizeUrl(url, baseOrigin) {
    const value = String(url || "").trim();
    if (!value) {
      return null;
    }
    if (value === "/") {
      return value;
    }
    if (value.startsWith("/") && !value.startsWith("//")) {
      if (
        value.startsWith("/api/") ||
        value.startsWith("/assets/") ||
        value.startsWith("/static/")
      ) {
        return value;
      }
      try {
        return new URL(`file://${value}`, baseOrigin || "http://127.0.0.1/").href;
      } catch {
        return null;
      }
    }
    try {
      const parsed = new URL(value, baseOrigin || "http://127.0.0.1/");
      if (!["http:", "https:", "mailto:", "file:"].includes(parsed.protocol)) {
        return null;
      }
      return parsed.href;
    } catch {
      return null;
    }
  }

  function preserveMathDelimiters(markdown) {
    const placeholders = [];

    function reserve(source) {
      const token = `WODEX_MATH_${placeholders.length}_TOKEN`;
      placeholders.push(String(source || ""));
      return token;
    }

    let text = String(markdown || "");
    text = text.replace(/\\\[[\s\S]*?\\\]/g, (match) => reserve(match));
    text = text.replace(/\\\([\s\S]*?\\\)/g, (match) => reserve(match));

    return {
      text,
      restore(html) {
        return placeholders.reduce(
          (current, source, index) =>
            current.replaceAll(`WODEX_MATH_${index}_TOKEN`, escapeHtml(source)),
          String(html || ""),
        );
      },
    };
  }

  function sanitizeNode(node, document, baseOrigin) {
    if (node.nodeType === 3) {
      return;
    }
    if (node.nodeType !== 1) {
      node.remove();
      return;
    }

    const tagName = node.tagName.toLowerCase();
    if (!ALLOWED_TAGS.has(tagName)) {
      node.replaceWith(document.createTextNode(node.outerHTML));
      return;
    }

    const allowedAttributes = ALLOWED_ATTRIBUTES.get(tagName) || new Set();
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      if (!allowedAttributes.has(name)) {
        node.removeAttribute(attribute.name);
        continue;
      }
      if (name === "href" || name === "src") {
        const safeUrl = sanitizeUrl(attribute.value, baseOrigin);
        if (safeUrl === null) {
          node.removeAttribute(attribute.name);
        } else {
          node.setAttribute(attribute.name, safeUrl);
        }
      }
    }

    if (tagName === "a" && node.hasAttribute("href")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }

    for (const child of [...node.childNodes]) {
      sanitizeNode(child, document, baseOrigin);
    }
  }

  function sanitizeRenderedHtml(html, options) {
    if (typeof DOMParser === "undefined") {
      return String(html || "");
    }

    const parser = new DOMParser();
    const document = parser.parseFromString(`<body>${String(html || "")}</body>`, "text/html");
    const baseOrigin =
      options && options.baseOrigin
        ? String(options.baseOrigin)
        : typeof window !== "undefined" && window.location
          ? window.location.origin
          : "http://127.0.0.1/";

    for (const child of [...document.body.childNodes]) {
      sanitizeNode(child, document, baseOrigin);
    }
    return document.body.innerHTML;
  }

  function resolveCommonmark(commonmarkLib) {
    if (commonmarkLib) {
      return commonmarkLib;
    }
    if (typeof globalThis !== "undefined" && globalThis.commonmark) {
      return globalThis.commonmark;
    }
    if (typeof require === "function") {
      try {
        return require("commonmark");
      } catch (_error) {
        // Fall through to the final error below.
      }
    }
    throw new Error("CommonMark library is unavailable.");
  }

  function createRenderer(options) {
    const commonmark = resolveCommonmark(options && options.commonmark);
    const parser = new commonmark.Parser(options && options.parserOptions ? options.parserOptions : {});

    function renderCommonMarkHtml(markdown, renderOptions) {
      const writer = new commonmark.HtmlRenderer(renderOptions || {});
      return writer.render(parser.parse(String(markdown || "")));
    }

    function renderMessageHtml(markdown, renderOptions) {
      const preserved = preserveMathDelimiters(markdown);
      const html = renderCommonMarkHtml(preserved.text, renderOptions || {});
      const restored = preserved.restore(html);
      return sanitizeRenderedHtml(restored, options || {});
    }

    function renderMathInNode(element) {
      if (!element || typeof window === "undefined" || typeof window.renderMathInElement !== "function") {
        return;
      }
      window.renderMathInElement(element, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\begin{equation}", right: "\\end{equation}", display: true },
          { left: "\\begin{align}", right: "\\end{align}", display: true },
          { left: "\\begin{alignat}", right: "\\end{alignat}", display: true },
          { left: "\\begin{gather}", right: "\\end{gather}", display: true },
          { left: "\\begin{CD}", right: "\\end{CD}", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "\\(", right: "\\)", display: false },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
        strict: "ignore",
      });
    }

    return {
      renderCommonMarkHtml,
      renderMessageHtml,
      renderMathInNode,
      sanitizeRenderedHtml,
      sanitizeUrl,
    };
  }

  return {
    createRenderer,
  };
});
