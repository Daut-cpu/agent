import DOMPurify from "dompurify";
import { marked } from "marked";
import mermaid from "mermaid";
import { useEffect, useMemo, useRef } from "react";

let mermaidTheme = "";
let counter = 0;

function ensureMermaid(): void {
  const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const theme = dark ? "dark" : "default";
  if (theme !== mermaidTheme) {
    // SVG-подписи вместо HTML в foreignObject: их не вырезает санитайзер.
    mermaid.initialize({ startOnLoad: false, theme, securityLevel: "strict", htmlLabels: false,
      flowchart: { htmlLabels: false } });
    mermaidTheme = theme;
  }
}

/** Markdown из ответов агента: санитизация + рендер блоков ```mermaid. */
export function Markdown({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const html = useMemo(
    () => DOMPurify.sanitize(marked.parse(text, { async: false, gfm: true, breaks: true }) as string),
    [text],
  );

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const blocks = root.querySelectorAll<HTMLElement>("pre > code.language-mermaid");
    if (!blocks.length) return;
    ensureMermaid();
    let cancelled = false;
    blocks.forEach(async (code) => {
      const pre = code.parentElement!;
      const source = code.textContent ?? "";
      try {
        const { svg } = await mermaid.render(`mmd-${++counter}`, source);
        if (cancelled) return;
        const holder = document.createElement("div");
        holder.className = "diagram";
        holder.innerHTML = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true, html: true } });
        pre.replaceWith(holder);
      } catch (e) {
        pre.classList.add("diagram-error");
        pre.title = `Mermaid: ${(e as Error).message}`;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [html]);

  return <div ref={ref} className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}
