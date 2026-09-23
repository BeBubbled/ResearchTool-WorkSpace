const SETTINGS_STORAGE_KEY = "paper-lens.selection-settings.v1";
const NOTES_STORAGE_PREFIX = "paper-lens.reader-notes.v1";
const PDFJS_BASE = "/static/vendor/pdfjs";
const DEFAULT_TYPOGRAPHY = Object.freeze({
  chinese: "Songti SC, SimSun",
  english: "Georgia, Times New Roman",
  math: "STIX Two Math, Cambria Math",
});
const DEFAULT_READING_LAYOUT = Object.freeze({
  fontSize: 17,
  letterSpacing: 0,
  lineHeight: 1.9,
  paragraphSpacing: 20,
  margins: Object.freeze({
    header: Object.freeze({ top: 18, right: 48, bottom: 12, left: 48 }),
    body: Object.freeze({ top: 38, right: 64, bottom: 56, left: 64 }),
    footer: Object.freeze({ top: 12, right: 48, bottom: 24, left: 48 }),
  }),
});
const PAGE_MARGIN_ZONES = Object.freeze(["header", "body", "footer"]);
const PAGE_MARGIN_SIDES = Object.freeze(["top", "right", "bottom", "left"]);
const readerState = {
  config: null,
  document: null,
  content: null,
  view: "original",
  selection: "",
  blockId: "",
  selectionContext: "",
  selectionSection: "",
  selectionPage: null,
  selectionAnchor: null,
  poller: null,
  selectedPreset: "",
  sourceFile: null,
  translationFile: null,
  autoSelectionAction: false,
  defaultSelectionAction: "",
  actionModels: {},
  asking: false,
  questionRequestId: 0,
  questionAbortController: null,
  pdfDocument: null,
  pdfLoadingTask: null,
  pdfScale: 1.15,
  renderToken: 0,
  inspectionToken: 0,
  pdfInspectionHint: "",
  pdfInspectionWarning: false,
  notes: [],
  textFormats: [],
  notesUpdatedAt: 0,
  assistantWidth: 320,
  paperWidth: null,
  paperBaseWidth: null,
  readerAxisOffset: 0,
  showTableOfContents: true,
  compactTableOfContents: false,
  tableOfContentsOverlayOpen: false,
  assistantOverlay: false,
  assistantOverlayOpen: false,
  pdfRenderWidth: 0,
  promptUnderlineLanes: new Map(),
  lastAnswer: null,
  liveTranslations: new Map(),
  liveTranslationRequests: new Map(),
  speechAbortController: null,
  speechAudioContext: null,
  speechSource: null,
  speechTarget: null,
  speechClickEnabled: true,
  speechPlaybackRate: 1,
  speechRequestId: 0,
  typography: { ...DEFAULT_TYPOGRAPHY },
  readingLayout: normalizeReadingLayout(),
  codexStatus: null,
  analysisProviders: null,
  threePassAnalysis: null,
  threePassEventSource: null,
  threePassPoller: null,
  codexLoginPoller: null,
  apiModelRequestId: 0,
  threePassPreview: "",
};

const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value).replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[char]));

function loadSelectionSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "{}");
    readerState.autoSelectionAction = saved.autoSelectionAction === true;
    readerState.defaultSelectionAction = typeof saved.defaultSelectionAction === "string" ? saved.defaultSelectionAction : "";
    readerState.actionModels = saved.actionModels && typeof saved.actionModels === "object" ? saved.actionModels : {};
    readerState.selectedPreset = typeof saved.fallbackPreset === "string" ? saved.fallbackPreset : "";
    readerState.assistantWidth = normalizeAssistantWidth(saved.assistantWidth);
    readerState.paperWidth = normalizePaperWidth(saved.paperWidth, null);
    readerState.readerAxisOffset = normalizeReaderAxisOffset(saved.readerAxisOffset);
    readerState.showTableOfContents = saved.showTableOfContents !== false;
    readerState.speechClickEnabled = saved.speechClickEnabled !== false;
    readerState.speechPlaybackRate = normalizeSpeechPlaybackRate(saved.speechPlaybackRate);
    readerState.typography = {
      chinese: normalizeFontFamily(saved.typography?.chinese, DEFAULT_TYPOGRAPHY.chinese),
      english: normalizeFontFamily(saved.typography?.english, DEFAULT_TYPOGRAPHY.english),
      math: normalizeFontFamily(saved.typography?.math, DEFAULT_TYPOGRAPHY.math),
    };
    readerState.readingLayout = normalizeReadingLayout(saved.readingLayout);
  } catch {
    readerState.actionModels = {};
  }
}

function persistSelectionSettings(message = "设置已保存在此浏览器。") {
  try {
    localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({
      autoSelectionAction: readerState.autoSelectionAction,
      defaultSelectionAction: readerState.defaultSelectionAction,
      actionModels: readerState.actionModels,
      fallbackPreset: readerState.selectedPreset,
      assistantWidth: readerState.assistantWidth,
      paperWidth: readerState.paperWidth,
      readerAxisOffset: readerState.readerAxisOffset,
      showTableOfContents: readerState.showTableOfContents,
      speechClickEnabled: readerState.speechClickEnabled,
      speechPlaybackRate: readerState.speechPlaybackRate,
      typography: readerState.typography,
      readingLayout: readerState.readingLayout,
    }));
  } catch {
    message = "设置已应用，但浏览器不允许持久保存。";
  }
  const status = $("#selectionSettingsStatus");
  if (status) {
    status.textContent = message;
    clearTimeout(persistSelectionSettings.timer);
    persistSelectionSettings.timer = setTimeout(() => { status.textContent = ""; }, 1800);
  }
}

function normalizeFontFamily(value, fallback = "") {
  const normalized = String(value || "")
    .replace(/[\u0000-\u001f{};<>]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 160);
  return normalized || fallback;
}

function normalizeLayoutNumber(value, fallback, minimum, maximum, precision = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  const factor = 10 ** precision;
  return Math.round(Math.min(maximum, Math.max(minimum, number)) * factor) / factor;
}

function normalizeSpeechPlaybackRate(value, fallback = 1) {
  return normalizeLayoutNumber(value, fallback, 0.5, 2, 2);
}

function normalizeReadingLayout(value = {}) {
  const source = value && typeof value === "object" ? value : {};
  const margins = {};
  PAGE_MARGIN_ZONES.forEach(zone => {
    const savedZone = source.margins?.[zone] && typeof source.margins[zone] === "object"
      ? source.margins[zone]
      : {};
    margins[zone] = {};
    PAGE_MARGIN_SIDES.forEach(side => {
      margins[zone][side] = normalizeLayoutNumber(
        savedZone[side],
        DEFAULT_READING_LAYOUT.margins[zone][side],
        0,
        240,
        0,
      );
    });
  });
  return {
    fontSize: normalizeLayoutNumber(source.fontSize, DEFAULT_READING_LAYOUT.fontSize, 10, 48),
    letterSpacing: normalizeLayoutNumber(source.letterSpacing, DEFAULT_READING_LAYOUT.letterSpacing, -3, 12),
    lineHeight: normalizeLayoutNumber(source.lineHeight, DEFAULT_READING_LAYOUT.lineHeight, 1, 3, 2),
    paragraphSpacing: normalizeLayoutNumber(source.paragraphSpacing, DEFAULT_READING_LAYOUT.paragraphSpacing, 0, 96, 0),
    margins,
  };
}

function normalizeAssistantWidth(value) {
  const width = Number(value);
  return Number.isFinite(width) ? Math.min(420, Math.max(280, Math.round(width))) : 320;
}

function normalizePaperWidth(value, fallback = 470) {
  const width = Number(value);
  return Number.isFinite(width) ? Math.min(2500, Math.max(470, Math.round(width))) : fallback;
}

function normalizeReaderAxisOffset(value) {
  const offset = Number(value);
  return Number.isFinite(offset) ? Math.min(1200, Math.max(-1200, Math.round(offset))) : 0;
}

function readerPaperColumnWidth(shellWidth, assistantWidth = 320) {
  const width = Number(shellWidth);
  return Math.max(
    470,
    (Number.isFinite(width) ? width : 0) - 220 - 36 - normalizeAssistantWidth(assistantWidth),
  );
}

function readerLayoutForWidth(width) {
  const available = Number(width) || 0;
  return {
    compactTableOfContents: available > 0 && available < 1320,
    assistantOverlay: available > 0 && available < 1120,
  };
}

function responsiveReaderWidth(viewportWidth, shellWidth) {
  const viewport = Number(viewportWidth);
  if (Number.isFinite(viewport) && viewport > 0) return viewport;
  const shell = Number(shellWidth);
  return Number.isFinite(shell) && shell > 0 ? shell : 0;
}

function pdfReflowNeeded(currentWidth, renderedWidth, tolerance = 24) {
  const current = Number(currentWidth);
  const rendered = Number(renderedWidth);
  const threshold = Number(tolerance);
  if (!Number.isFinite(current) || current <= 0) return false;
  if (!Number.isFinite(rendered) || rendered <= 0) return true;
  return Math.abs(current - rendered) >= (Number.isFinite(threshold) && threshold > 0 ? threshold : 24);
}

function pdfFitScale(availableWidth, naturalWidth, zoom = 1) {
  const available = Number(availableWidth);
  const natural = Number(naturalWidth);
  const multiplier = Number(zoom);
  if (!Number.isFinite(available) || !Number.isFinite(natural) || natural <= 0) return 1;
  return Math.max(0.1, available / natural) * (Number.isFinite(multiplier) ? multiplier : 1);
}

function closeResponsivePanels() {
  readerState.tableOfContentsOverlayOpen = false;
  readerState.assistantOverlayOpen = false;
  applyResponsiveReaderLayout();
}

function applyResponsiveReaderLayout() {
  const shell = $("#readerShell");
  if (!shell) return;
  // Viewport width is stable when a panel or the document scrollbar appears.
  // Using shell.clientWidth here can bounce across a breakpoint and repeatedly
  // show/hide Copilot, which in turn resizes and rerenders the entire paper.
  const width = responsiveReaderWidth(window.innerWidth, shell.clientWidth);
  const layout = readerLayoutForWidth(width);
  readerState.compactTableOfContents = layout.compactTableOfContents;
  readerState.assistantOverlay = layout.assistantOverlay;
  if (!layout.compactTableOfContents) readerState.tableOfContentsOverlayOpen = false;
  if (!layout.assistantOverlay) readerState.assistantOverlayOpen = false;
  shell.classList.toggle("toc-compact", layout.compactTableOfContents);
  shell.classList.toggle("toc-overlay-open", layout.compactTableOfContents && readerState.tableOfContentsOverlayOpen);
  shell.classList.toggle("assistant-overlay", layout.assistantOverlay);
  shell.classList.toggle("assistant-overlay-open", layout.assistantOverlay && readerState.assistantOverlayOpen);
  const outlineExpanded = layout.compactTableOfContents
    ? readerState.tableOfContentsOverlayOpen
    : readerState.showTableOfContents;
  $("#outlinePanel")?.setAttribute("aria-hidden", String(!outlineExpanded));
  const assistantVisible = !layout.assistantOverlay || readerState.assistantOverlayOpen;
  $("#assistantPanel")?.setAttribute("aria-hidden", String(!assistantVisible));
  const outlineButton = $("#outlineToggle");
  const outlineLabel = outlineExpanded ? "关闭文档目录" : "打开文档目录";
  outlineButton?.setAttribute("aria-expanded", String(outlineExpanded));
  outlineButton?.setAttribute("aria-label", outlineLabel);
  if (outlineButton) outlineButton.title = outlineLabel;
  const assistantButton = $("#assistantToggle");
  const assistantExpanded = layout.assistantOverlay && readerState.assistantOverlayOpen;
  const assistantLabel = assistantExpanded ? "关闭划词解读" : "打开划词解读";
  assistantButton?.setAttribute("aria-expanded", String(assistantExpanded));
  assistantButton?.setAttribute("aria-label", assistantLabel);
  if (assistantButton) assistantButton.title = assistantLabel;
  $("#readerPanelBackdrop")?.setAttribute("aria-hidden", String(!(readerState.tableOfContentsOverlayOpen || readerState.assistantOverlayOpen)));
  applyAssistantWidth();
}

function schedulePdfReflow() {
  if (!readerState.pdfDocument || !$("#paperContent")?.classList.contains("pdf-content")) return;
  const width = $("#paperContent")?.clientWidth || 0;
  if (!pdfReflowNeeded(width, readerState.pdfRenderWidth)) return;
  clearTimeout(schedulePdfReflow.timer);
  schedulePdfReflow.timer = setTimeout(() => {
    const currentWidth = $("#paperContent")?.clientWidth || 0;
    if (!pdfReflowNeeded(currentWidth, readerState.pdfRenderWidth)) return;
    renderPaper();
  }, 180);
}

function initResponsiveReaderLayout() {
  const shell = $("#readerShell");
  if (!shell) return;
  if (typeof ResizeObserver !== "undefined") {
    let observedWidth = Math.round(shell.getBoundingClientRect().width);
    const observer = new ResizeObserver(entries => {
      const nextWidth = Math.round(entries[0]?.contentRect?.width || shell.clientWidth || 0);
      if (!nextWidth || nextWidth === observedWidth) return;
      observedWidth = nextWidth;
      schedulePdfReflow();
    });
    observer.observe(shell);
  }
  window.addEventListener("resize", applyResponsiveReaderLayout);
  $("#outlineToggle")?.addEventListener("click", () => {
    if (readerState.compactTableOfContents) {
      readerState.tableOfContentsOverlayOpen = !readerState.tableOfContentsOverlayOpen;
      readerState.assistantOverlayOpen = false;
      applyResponsiveReaderLayout();
      return;
    }
    readerState.showTableOfContents = !readerState.showTableOfContents;
    applyTableOfContentsVisibility();
    persistSelectionSettings(readerState.showTableOfContents ? "文档目录已显示。" : "文档目录已隐藏。 ");
  });
  $("#closeOutline")?.addEventListener("click", () => {
    if (readerState.compactTableOfContents) closeResponsivePanels();
  });
  $("#assistantToggle")?.addEventListener("click", () => {
    readerState.assistantOverlayOpen = !readerState.assistantOverlayOpen;
    readerState.tableOfContentsOverlayOpen = false;
    applyResponsiveReaderLayout();
  });
  $("#closeAssistant")?.addEventListener("click", closeResponsivePanels);
  $("#readerPanelBackdrop")?.addEventListener("click", closeResponsivePanels);
  applyResponsiveReaderLayout();
}

function applyTableOfContentsVisibility() {
  const shell = $("#readerShell");
  const outline = $("#outlinePanel");
  const toggle = $("#showTableOfContents");
  shell?.classList.toggle("toc-hidden", !readerState.showTableOfContents);
  const visible = readerState.compactTableOfContents
    ? readerState.tableOfContentsOverlayOpen
    : readerState.showTableOfContents;
  outline?.setAttribute("aria-hidden", String(!visible));
  if (toggle) toggle.checked = readerState.showTableOfContents;
  applyResponsiveReaderLayout();
}

function lockReaderPaperWidth() {
  const shell = $("#readerShell");
  if (!shell) return;
  unlockReaderPaperWidth();
  applyResponsiveReaderLayout();
}

function unlockReaderPaperWidth() {
  const shell = $("#readerShell");
  shell?.classList.remove("reader-width-locked");
  shell?.style.removeProperty("--reader-paper-column-width");
  shell?.style.removeProperty("--reader-paper-base-width");
  readerState.paperBaseWidth = null;
}

function applyPaperWidth(width = readerState.paperWidth) {
  const shell = $("#readerShell");
  readerState.paperWidth = normalizePaperWidth(width, readerState.paperBaseWidth || 470);
  shell?.style.setProperty("--reader-paper-column-width", `${readerState.paperWidth}px`);
  const handle = $("#paperResizeHandle");
  handle?.setAttribute("aria-valuemax", "2500");
  handle?.setAttribute("aria-valuenow", String(readerState.paperWidth));
  handle?.setAttribute("aria-valuetext", `正文阅读宽度 ${readerState.paperWidth}px，向左拖动可加宽`);
  const slider = $("#paperWidthSlider");
  if (slider) {
    slider.value = String(readerState.paperWidth);
    slider.disabled = window.innerWidth <= 1120;
  }
  const output = $("#paperWidthValue");
  if (output) output.textContent = `${readerState.paperWidth}px`;
  document.querySelectorAll("[data-paper-width-delta]").forEach(button => {
    button.disabled = window.innerWidth <= 1120;
  });
}

function applyReaderAxisOffset(offset = readerState.readerAxisOffset) {
  readerState.readerAxisOffset = normalizeReaderAxisOffset(offset);
  $("#readerShell")?.style.setProperty("--reader-axis-offset", `${readerState.readerAxisOffset}px`);
  const slider = $("#readerAxisSlider");
  if (slider) {
    slider.value = String(readerState.readerAxisOffset);
    slider.disabled = window.innerWidth <= 1120;
  }
  const output = $("#readerAxisValue");
  if (output) {
    output.textContent = `${readerState.readerAxisOffset > 0 ? "+" : ""}${readerState.readerAxisOffset}px`;
  }
  document.querySelectorAll("[data-reader-axis-delta]").forEach(button => {
    button.disabled = window.innerWidth <= 1120;
  });
}

function assistantWidthLimit() {
  return 420;
}

function applyAssistantWidth(width = readerState.assistantWidth) {
  const widthLimit = assistantWidthLimit();
  readerState.assistantWidth = Math.min(normalizeAssistantWidth(width), widthLimit);
  $("#readerShell")?.style.setProperty("--assistant-panel-width", `${readerState.assistantWidth}px`);
  $("#assistantResizeHandle")?.setAttribute("aria-valuenow", String(readerState.assistantWidth));
  $("#assistantResizeHandle")?.setAttribute("aria-valuemax", String(widthLimit));
  const slider = $("#assistantWidthSlider");
  if (slider) {
    slider.max = String(widthLimit);
    slider.value = String(readerState.assistantWidth);
    slider.disabled = readerState.assistantOverlay;
  }
  const output = $("#assistantWidthValue");
  if (output) output.textContent = `${readerState.assistantWidth}px`;
  document.querySelectorAll("[data-assistant-width-delta]").forEach(button => {
    button.disabled = readerState.assistantOverlay;
  });
  schedulePdfReflow();
}

function initAssistantResize() {
  const handle = $("#assistantResizeHandle");
  if (!handle) return;
  let startX = 0;
  let startWidth = 0;
  const finish = event => {
    const wasResizing = document.body.classList.contains("resizing-assistant");
    if (handle.hasPointerCapture?.(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove("resizing-assistant");
    if (wasResizing) persistSelectionSettings(`右侧解读栏宽度已设为 ${readerState.assistantWidth}px。`);
  };
  handle.addEventListener("pointerdown", event => {
    if (readerState.assistantOverlay) return;
    startX = event.clientX;
    startWidth = readerState.assistantWidth;
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing-assistant");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", event => {
    if (!handle.hasPointerCapture?.(event.pointerId)) return;
    applyAssistantWidth(startWidth - (event.clientX - startX));
  });
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
  handle.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key) || readerState.assistantOverlay) return;
    applyAssistantWidth(readerState.assistantWidth + (event.key === "ArrowLeft" ? 20 : -20));
    persistSelectionSettings(`右侧解读栏宽度已设为 ${readerState.assistantWidth}px。`);
    event.preventDefault();
  });
  const slider = $("#assistantWidthSlider");
  slider?.addEventListener("input", event => applyAssistantWidth(event.target.value));
  slider?.addEventListener("change", () => {
    persistSelectionSettings(`右侧解读面板整体宽度已设为 ${readerState.assistantWidth}px。`);
  });
  document.querySelectorAll("[data-assistant-width-delta]").forEach(button => {
    button.addEventListener("click", () => {
      applyAssistantWidth(readerState.assistantWidth + Number(button.dataset.assistantWidthDelta || 0));
      persistSelectionSettings(`右侧解读面板整体宽度已设为 ${readerState.assistantWidth}px。`);
    });
  });
  window.addEventListener("resize", () => {
    applyResponsiveReaderLayout();
    applyAssistantWidth();
  });
}

function initPaperResize() {
  const handle = $("#paperResizeHandle");
  if (!handle) return;
  let startX = 0;
  let startWidth = 0;
  const finish = event => {
    const wasResizing = document.body.classList.contains("resizing-paper");
    if (handle.hasPointerCapture?.(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove("resizing-paper");
    if (wasResizing) {
      persistSelectionSettings(`正文阅读宽度已设为 ${readerState.paperWidth}px，并保持右侧位置不变。`);
    }
  };
  handle.addEventListener("pointerdown", event => {
    const shell = $("#readerShell");
    if (window.innerWidth <= 1120 || !shell?.classList.contains("reader-width-locked")) return;
    startX = event.clientX;
    startWidth = readerState.paperWidth;
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing-paper");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", event => {
    if (!handle.hasPointerCapture?.(event.pointerId)) return;
    applyPaperWidth(startWidth - (event.clientX - startX));
  });
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
  handle.addEventListener("keydown", event => {
    const shell = $("#readerShell");
    if (
      !["ArrowLeft", "ArrowRight"].includes(event.key)
      || window.innerWidth <= 1120
      || !shell?.classList.contains("reader-width-locked")
    ) return;
    applyPaperWidth(readerState.paperWidth + (event.key === "ArrowLeft" ? 20 : -20));
    persistSelectionSettings(`正文阅读宽度已设为 ${readerState.paperWidth}px，并保持右侧位置不变。`);
    event.preventDefault();
  });
  const slider = $("#paperWidthSlider");
  slider?.addEventListener("input", event => applyPaperWidth(event.target.value));
  slider?.addEventListener("change", () => {
    persistSelectionSettings(`正文阅读宽度已设为 ${readerState.paperWidth}px。`);
  });
  document.querySelectorAll("[data-paper-width-delta]").forEach(button => {
    button.addEventListener("click", () => {
      applyPaperWidth(readerState.paperWidth + Number(button.dataset.paperWidthDelta || 0));
      persistSelectionSettings(`正文阅读宽度已设为 ${readerState.paperWidth}px。`);
    });
  });
  const axisSlider = $("#readerAxisSlider");
  axisSlider?.addEventListener("input", event => applyReaderAxisOffset(event.target.value));
  axisSlider?.addEventListener("change", () => {
    persistSelectionSettings(`阅读器中轴已偏移 ${readerState.readerAxisOffset}px。`);
  });
  document.querySelectorAll("[data-reader-axis-delta]").forEach(button => {
    button.addEventListener("click", () => {
      applyReaderAxisOffset(
        readerState.readerAxisOffset + Number(button.dataset.readerAxisDelta || 0),
      );
      persistSelectionSettings(`阅读器中轴已偏移 ${readerState.readerAxisOffset}px。`);
    });
  });
}

function typographyFontStack() {
  return `${readerState.typography.english}, ${readerState.typography.chinese}, serif`;
}

function applyReadingLayoutVariables(root) {
  if (!root?.style) return;
  const layout = readerState.readingLayout;
  root.style.setProperty("--reader-font-size", `${layout.fontSize}px`);
  root.style.setProperty("--reader-letter-spacing", `${layout.letterSpacing}px`);
  root.style.setProperty("--reader-line-height", String(layout.lineHeight));
  root.style.setProperty("--reader-paragraph-spacing", `${layout.paragraphSpacing}px`);
  PAGE_MARGIN_ZONES.forEach(zone => {
    PAGE_MARGIN_SIDES.forEach(side => {
      root.style.setProperty(`--reader-${zone}-margin-${side}`, `${layout.margins[zone][side]}px`);
    });
  });
}

function applyTypographyToDocument(targetDocument) {
  if (!targetDocument?.documentElement) return;
  const root = targetDocument.documentElement;
  root.style.setProperty("--reader-text-font", typographyFontStack());
  root.style.setProperty("--reader-math-font", `${readerState.typography.math}, serif`);
  applyReadingLayoutVariables(root);
  if (targetDocument !== document) {
    let style = targetDocument.getElementById("paper-lens-typography");
    if (!style) {
      style = targetDocument.createElement("style");
      style.id = "paper-lens-typography";
      (targetDocument.head || targetDocument.documentElement).append(style);
    }
    style.textContent = `
      body, p, li, td, th, blockquote, figcaption, h1, h2, h3, h4, h5, h6 {
        font-family: var(--reader-text-font) !important;
      }
      p, li, td, th, blockquote, figcaption {
        font-size: var(--reader-font-size) !important;
        letter-spacing: var(--reader-letter-spacing) !important;
        line-height: var(--reader-line-height) !important;
      }
      body {
        box-sizing: border-box !important;
        margin: 0 !important;
        font-size: var(--reader-font-size) !important;
        letter-spacing: var(--reader-letter-spacing) !important;
        line-height: var(--reader-line-height) !important;
      }
      body:not(:has(> main, > article)) {
        padding: var(--reader-body-margin-top) var(--reader-body-margin-right)
          var(--reader-body-margin-bottom) var(--reader-body-margin-left) !important;
      }
      body:has(> main, > article) {
        padding: 0 !important;
      }
      body > header, header[role="banner"] {
        box-sizing: border-box !important;
        padding: var(--reader-header-margin-top) var(--reader-header-margin-right)
          var(--reader-header-margin-bottom) var(--reader-header-margin-left) !important;
      }
      body > main, main[role="main"], body > article {
        box-sizing: border-box !important;
        padding: var(--reader-body-margin-top) var(--reader-body-margin-right)
          var(--reader-body-margin-bottom) var(--reader-body-margin-left) !important;
      }
      body > footer, footer[role="contentinfo"] {
        box-sizing: border-box !important;
        padding: var(--reader-footer-margin-top) var(--reader-footer-margin-right)
          var(--reader-footer-margin-bottom) var(--reader-footer-margin-left) !important;
      }
      p {
        margin-block-start: 0 !important;
        margin-block-end: var(--reader-paragraph-spacing) !important;
      }
      .math-source, math, mjx-mtext {
        font-family: var(--reader-math-font) !important;
      }
    `;
  }
}

function applyReaderTypography() {
  applyTypographyToDocument(document);
  const frameDocument = $("#readerDocumentFrame")?.contentDocument;
  if (frameDocument) applyTypographyToDocument(frameDocument);
}

function renderTypographySettings() {
  $("#chineseFont").value = readerState.typography.chinese;
  $("#englishFont").value = readerState.typography.english;
  $("#mathFont").value = readerState.typography.math;
}

function renderReadingLayoutSettings() {
  const layout = readerState.readingLayout;
  $("#readerFontSize").value = String(layout.fontSize);
  $("#readerLetterSpacing").value = String(layout.letterSpacing);
  $("#readerLineHeight").value = String(layout.lineHeight);
  $("#readerParagraphSpacing").value = String(layout.paragraphSpacing);
  document.querySelectorAll("[data-page-margin-zone][data-page-margin-side]").forEach(input => {
    input.value = String(layout.margins[input.dataset.pageMarginZone][input.dataset.pageMarginSide]);
  });
}

function updateTypographyFromInputs(message = "阅读字体已更新。") {
  readerState.typography = {
    chinese: normalizeFontFamily($("#chineseFont").value, DEFAULT_TYPOGRAPHY.chinese),
    english: normalizeFontFamily($("#englishFont").value, DEFAULT_TYPOGRAPHY.english),
    math: normalizeFontFamily($("#mathFont").value, DEFAULT_TYPOGRAPHY.math),
  };
  renderTypographySettings();
  applyReaderTypography();
  persistSelectionSettings(message);
}

function updateReadingLayoutFromInputs(message = "阅读排版已更新。") {
  const margins = {};
  PAGE_MARGIN_ZONES.forEach(zone => {
    margins[zone] = {};
    PAGE_MARGIN_SIDES.forEach(side => {
      const input = document.querySelector(`[data-page-margin-zone="${zone}"][data-page-margin-side="${side}"]`);
      margins[zone][side] = input?.value;
    });
  });
  readerState.readingLayout = normalizeReadingLayout({
    fontSize: $("#readerFontSize").value,
    letterSpacing: $("#readerLetterSpacing").value,
    lineHeight: $("#readerLineHeight").value,
    paragraphSpacing: $("#readerParagraphSpacing").value,
    margins,
  });
  renderReadingLayoutSettings();
  applyReaderTypography();
  persistSelectionSettings(message);
}

function configHtml(id, allowCustom = true) {
  const presets = readerState.config?.llmPresets || [];
  if (!readerState.selectedPreset || (!allowCustom && readerState.selectedPreset === "custom") || (readerState.selectedPreset !== "custom" && !presets.some(item => item.id === readerState.selectedPreset))) {
    readerState.selectedPreset = presets[0]?.id || (allowCustom ? "custom" : "");
  }
  const presetOptions = presets.map(item => (
    `<option value="${escapeHtml(item.id)}" ${item.id === readerState.selectedPreset ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)} · 并发 ${escapeHtml(item.concurrency || 1)}</option>`
  )).join("");
  const options = presetOptions || `<option value="">请先在全局面板添加 LLM 配置</option>`;
  const customOption = allowCustom
    ? `<option value="custom" ${readerState.selectedPreset === "custom" ? "selected" : ""}>手动添加 OpenAI-compatible 配置</option>`
    : "";
  const customFields = allowCustom ? `
    <div class="custom-llm ${readerState.selectedPreset === "custom" ? "" : "hidden"}" data-custom="${id}">
      <label>名称<input data-llm="name" placeholder="例如：实验室代理"></label>
      <label>Base URL<input data-llm="baseUrl" placeholder="https://api.example.com/v1"></label>
      <label>API Key<input data-llm="apiKey" type="password" autocomplete="off"></label>
      <label>模型 ID<input data-llm="model" placeholder="模型名称"></label>
      <label>并发请求数<input data-llm="concurrency" type="number" min="1" max="64" step="1" value="1"></label>
      <a class="manage-presets-link" href="/#llm-settings">前往全局 LLM 配置添加或编辑预设 →</a>
      <p class="preset-note">这组手动参数只用于当前请求，不会被保存。</p>
    </div>` : `<a class="manage-presets-link" href="/#llm-settings">前往全局 LLM 配置添加或编辑预设 →</a>
      <p class="preset-note">整篇翻译的模型、密钥与并发只从全局 LLM 管理面板读取。</p>`;
  return `<label>LLM 配置<select class="llm-preset" data-config="${id}" ${presets.length ? "" : "disabled"}>${options}${customOption}</select></label>${customFields}`;
}

function wireLlmConfig(target, trackFallback = false) {
  target.querySelector(".llm-preset")?.addEventListener("change", event => {
    target.querySelector(".custom-llm")?.classList.toggle("hidden", event.target.value !== "custom");
    if (trackFallback) {
      readerState.selectedPreset = event.target.value;
      persistSelectionSettings("兜底 LLM 已更新。");
    }
  });
}

function llmConfig(target) {
  const presetId = target.querySelector(".llm-preset")?.value;
  if (!presetId) throw new Error("请先在全局 LLM 管理面板添加配置。");
  if (presetId !== "custom") return { mode: "preset", presetId };
  const custom = key => target.querySelector(`[data-llm="${key}"]`)?.value.trim();
  const result = {
    mode: "custom",
    name: custom("name"),
    baseUrl: custom("baseUrl"),
    apiKey: custom("apiKey"),
    model: custom("model"),
    concurrency: Number(custom("concurrency") || 1),
  };
  if (Object.values(result).some(value => !value)) throw new Error("请完整填写当前请求使用的手动 LLM 配置。");
  return result;
}

function refreshLlmConfigs() {
  $("#uploadLlm").innerHTML = configHtml("upload", false);
  $("#questionLlm").innerHTML = configHtml("question");
  wireLlmConfig($("#uploadLlm"));
  wireLlmConfig($("#questionLlm"), true);
}

function renderSelectionSettings() {
  const actions = readerState.config?.actions || [];
  const presets = readerState.config?.llmPresets || [];
  if (!actions.some(action => action.id === readerState.defaultSelectionAction)) {
    readerState.defaultSelectionAction = actions[0]?.id || "";
  }
  $("#autoSelectionAction").checked = readerState.autoSelectionAction;
  $("#showTableOfContents").checked = readerState.showTableOfContents;
  renderTypographySettings();
  renderReadingLayoutSettings();
  $("#defaultSelectionAction").disabled = !readerState.autoSelectionAction;
  $("#defaultSelectionAction").innerHTML = actions.map(action => (
    `<option value="${escapeHtml(action.id)}" ${action.id === readerState.defaultSelectionAction ? "selected" : ""}>${escapeHtml(action.label)}</option>`
  )).join("");
  const modelOptions = selected => [
    `<option value="" ${selected ? "" : "selected"}>使用兜底配置</option>`,
    ...presets.map(preset => (
      `<option value="${escapeHtml(preset.id)}" ${preset.id === selected ? "selected" : ""}>${escapeHtml(preset.name)} · ${escapeHtml(preset.model)} · 并发 ${escapeHtml(preset.concurrency || 1)}</option>`
    )),
  ].join("");
  $("#actionModelMappings").innerHTML = actions.map(action => {
    const selected = presets.some(item => item.id === readerState.actionModels[action.id]) ? readerState.actionModels[action.id] : "";
    return `<label class="action-model-row"><span>${escapeHtml(action.label)}</span><select data-action-model="${escapeHtml(action.id)}" aria-label="${escapeHtml(action.label)}使用的 LLM">${modelOptions(selected)}</select></label>`;
  }).join("");
  $("#actionModelMappings").querySelectorAll("[data-action-model]").forEach(select => {
    select.addEventListener("change", event => {
      const action = event.target.dataset.actionModel;
      if (event.target.value) readerState.actionModels[action] = event.target.value;
      else delete readerState.actionModels[action];
      persistSelectionSettings(`${actionLabel(action)}的 LLM 已更新。`);
      renderActions();
    });
  });
}

function actionLabel(actionId) {
  return readerState.config?.actions?.find(action => action.id === actionId)?.label || actionId;
}

function mappedPreset(actionId) {
  const presetId = readerState.actionModels[actionId];
  return readerState.config?.llmPresets?.find(preset => preset.id === presetId) || null;
}

function llmForAction(actionId) {
  const preset = mappedPreset(actionId);
  return preset ? { mode: "preset", presetId: preset.id } : llmConfig($("#questionLlm"));
}

function toggleSelectionSettings(force) {
  const panel = $("#selectionSettingsPanel");
  const button = $("#selectionSettingsButton");
  const shouldOpen = typeof force === "boolean" ? force : panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !shouldOpen);
  button.setAttribute("aria-expanded", String(shouldOpen));
  button.setAttribute("aria-label", shouldOpen ? "关闭阅读设置" : "打开阅读设置");
  button.title = shouldOpen ? "关闭阅读设置" : "打开阅读设置";
}

function localAsset(url) {
  const value = String(url || "").trim().replace(/^<|>$/g, "");
  if (/^(https?:|data:|blob:)/i.test(value)) return value;
  const match = value.replace(/\\/g, "/").match(/^([^?#]*)([?#].*)?$/);
  const path = match?.[1] || "";
  const suffix = match?.[2] || "";
  const encoded = path.split("/").filter(part => part && part !== "." && part !== "..").map(part => {
    try {
      return encodeURIComponent(decodeURIComponent(part));
    } catch {
      return encodeURIComponent(part);
    }
  }).join("/");
  return `${readerState.content?.assetBase || ""}${encoded}${suffix}`;
}

function safeLink(url) {
  return /^(https?:|mailto:|#)/i.test(url) ? url : "#";
}

function markdownInline(text) {
  const formulas = [];
  const images = [];
  const protectFormula = value => {
    formulas.push(value);
    // Private-use sentinels contain no Markdown emphasis characters, so
    // underscores inside the implementation token cannot be consumed before
    // the original LaTeX is restored.
    return `\uE000RF${formulas.length - 1}\uE001`;
  };
  const protectImage = (url, alt = "") => {
    images.push({ url, alt });
    return `\uE000RI${images.length - 1}\uE001`;
  };
  const protectedText = String(text)
    .replace(/!\[([^\]]*)\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g, (_all, alt, angleUrl, plainUrl) => protectImage(angleUrl || plainUrl, alt))
    .replace(/!\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_all, url, alt) => protectImage(url, alt || url.split(/[\\/]/).pop() || "论文插图"))
    .replace(/\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}/g, (_all, url) => protectImage(url, "论文插图"))
    .replace(/\\\(([\s\S]*?)\\\)/g, protectFormula)
    .replace(/(?<!\\)\$\$([\s\S]*?)\$\$/g, protectFormula)
    .replace(/(?<!\\)\$(?!\$)([^$\n]+?)\$/g, protectFormula);
  let html = escapeHtml(protectedText);
  html = html.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)/g, (_all, label, url) => `<a href="${escapeHtml(safeLink(url))}" target="_blank" rel="noreferrer">${label}</a>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/(?<!\*)\*([^*\n]+)\*/g, "<em>$1</em>");
  html = html.replace(/(?<!_)_([^_\n]+)_(?!_)/g, "<em>$1</em>");
  html = html.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  html = html.replace(/\uE000RI(\d+)\uE001/g, (_all, index) => {
    const image = images[Number(index)];
    return `<img src="${escapeHtml(localAsset(image.url))}" alt="${escapeHtml(image.alt)}" loading="lazy">`;
  });
  return html.replace(/\uE000RF(\d+)\uE001/g, (_all, index) => {
    const source = formulas[Number(index)];
    return `<span class="math-source" data-latex="${escapeHtml(source)}">${escapeHtml(source)}</span>`;
  });
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
}

function tableHtml(content) {
  const rows = content.split("\n").filter(Boolean).map(tableCells);
  const header = rows.shift() || [];
  rows.shift();
  return `<div class="table-wrap"><table><thead><tr>${header.map(cell => `<th>${markdownInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${header.map((_, index) => `<td>${markdownInline(row[index] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function listHtml(content) {
  const lines = content.split("\n").filter(line => line.trim());
  const ordered = /^\s*\d+[.)]\s+/.test(lines[0] || "");
  const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
  const tag = ordered ? "ol" : "ul";
  return `<${tag}>${lines.map(line => {
    const indent = Math.floor((line.match(/^\s*/)?.[0].length || 0) / 2);
    return `<li style="margin-left:${indent * 16}px">${markdownInline(line.replace(pattern, ""))}</li>`;
  }).join("")}</${tag}>`;
}

function blockHtml(block, idPrefix = "") {
  const content = block.content || "";
  const headingLevel = Math.min(4, Math.max(2, block.level || 2));
  if (block.type === "heading") return `<h${headingLevel} id="${idPrefix}${block.id}">${markdownInline(content)}</h${headingLevel}>`;
  if (block.type === "code") return `<pre><code>${escapeHtml(content.replace(/^```[^\n]*\n?|```$/g, ""))}</code></pre>`;
  if (block.type === "math") return `<div class="math-block math-source" data-latex="${escapeHtml(content)}">${escapeHtml(content)}</div>`;
  if (block.type === "table") return tableHtml(content);
  if (block.type === "quote") return `<blockquote>${content.split("\n").map(markdownInline).join("<br>")}</blockquote>`;
  if (block.type === "rule") return "<hr>";
  if (block.type === "list") return listHtml(content);
  return `<p>${content.split("\n").map(markdownInline).join("<br>")}</p>`;
}

function activeBlocks() {
  if (readerState.view === "translated" && readerState.content.translatedBlocks) return readerState.content.translatedBlocks;
  return readerState.content.blocks || [];
}

function liveTranslationKey(blockId) {
  return `${readerState.document?.id || "document"}:${blockId}`;
}

function liveSourceFingerprint(value) {
  const text = String(value || "").trim();
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `${text.length}:${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function liveTranslationRecord(blockId) {
  return readerState.liveTranslations.get(liveTranslationKey(blockId)) || null;
}

function liveTranslationContent(record) {
  if (record?.loading) return "正在翻译本段…";
  if (record?.error) return `${escapeHtml(record.error)}（再次点击原文可重试）`;
  return record?.translation ? answerMarkdownHtml(record.translation) : "";
}

function liveTranslationClassName(record) {
  return [
    "live-paragraph-translation",
    record?.loading ? "is-loading" : "",
    record?.error ? "is-error" : "",
  ].filter(Boolean).join(" ");
}

function liveTranslationMarkup(blockId) {
  const record = liveTranslationRecord(blockId);
  if (!record) return "";
  return `<div class="${liveTranslationClassName(record)}" data-live-translation-for="${escapeHtml(blockId)}">${liveTranslationContent(record)}</div>`;
}

function renderLiveTranslationNode(sourceElement, blockId) {
  if (!sourceElement?.parentNode) return;
  const record = liveTranslationRecord(blockId);
  const next = sourceElement.nextElementSibling;
  let node = next?.dataset?.liveTranslationFor === blockId ? next : null;
  if (!record) {
    node?.remove();
    return;
  }
  if (!node) {
    node = sourceElement.ownerDocument.createElement("div");
    node.dataset.liveTranslationFor = blockId;
    sourceElement.insertAdjacentElement("afterend", node);
  }
  node.className = liveTranslationClassName(record);
  node.innerHTML = liveTranslationContent(record);
  if (record.translation) {
    node.dataset.readerSpeechId = `live-translation-${blockId}`;
    node.title = "单击朗读本段译文";
  } else {
    delete node.dataset.readerSpeechId;
    node.removeAttribute("title");
  }
}

async function translateLiveParagraph(blockId, sourceText, sourceElement) {
  const key = liveTranslationKey(blockId);
  if (readerState.liveTranslationRequests.has(key)) return readerState.liveTranslationRequests.get(key);
  const current = readerState.liveTranslations.get(key);
  if (current?.translation) {
    renderLiveTranslationNode(sourceElement, blockId);
    return current.translation;
  }
  let llm;
  try {
    llm = llmForAction("translate");
  } catch (error) {
    readerState.liveTranslations.set(key, { error: error.message || "请先配置翻译所用的 LLM。" });
    renderLiveTranslationNode(sourceElement, blockId);
    toggleSelectionSettings(true);
    return null;
  }
  readerState.liveTranslations.set(key, { loading: true });
  renderLiveTranslationNode(sourceElement, blockId);
  $("#readingStatus").textContent = "正在翻译当前段落";
  const request = (async () => {
    try {
      const response = await fetch(`/api/reader/documents/${readerState.document.id}/paragraph-translations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ blockId, sourceText, llm }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "本段翻译失败。");
      readerState.liveTranslations.set(key, {
        translation: payload.translation,
        cached: payload.cached === true,
        sourceHash: liveSourceFingerprint(sourceText),
        updatedAt: Date.now(),
      });
      persistDocumentNotes();
      $("#readingStatus").textContent = payload.cached ? "已显示缓存译文" : "本段译文已显示";
      return payload.translation;
    } catch (error) {
      readerState.liveTranslations.set(key, { error: error.message || "本段翻译失败。" });
      $("#readingStatus").textContent = "本段翻译失败";
      return null;
    } finally {
      readerState.liveTranslationRequests.delete(key);
      renderLiveTranslationNode(sourceElement, blockId);
    }
  })();
  readerState.liveTranslationRequests.set(key, request);
  return request;
}

function wireLiveSourceElement(element, blockId, sourceText) {
  if (!element || !blockId || !sourceText?.trim()) return;
  const key = liveTranslationKey(blockId);
  const record = readerState.liveTranslations.get(key);
  const sourceHash = liveSourceFingerprint(sourceText);
  if (record?.sourceHash && record.sourceHash !== sourceHash) {
    readerState.liveTranslations.delete(key);
    persistDocumentNotes();
  }
  element.classList.add("live-translation-source");
  element.tabIndex = 0;
  element.setAttribute("role", "button");
  element.setAttribute("aria-label", "翻译本段并在下方显示译文");
  element.title = "点击后翻译本段，译文会显示在原文正下方";
  const activate = event => {
    if (event.type === "click") {
      const selection = element.ownerDocument.defaultView?.getSelection?.();
      if (selection && !selection.isCollapsed && selection.toString().trim()) return;
      if (event.target.closest?.("a,button,input,select,textarea,label")) return;
    }
    if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
    if (event.type === "keydown") event.preventDefault();
    translateLiveParagraph(blockId, sourceText.trim(), element);
  };
  element.addEventListener("click", activate);
  element.addEventListener("keydown", activate);
  renderLiveTranslationNode(element, blockId);
}

function wireLiveMarkdownTranslation() {
  const blocksById = new Map((readerState.content.blocks || []).map(block => [block.id, block]));
  $("#paperContent").querySelectorAll("[data-live-block-id]").forEach(element => {
    const blockId = element.dataset.liveBlockId;
    const block = blocksById.get(blockId);
    wireLiveSourceElement(element, blockId, block?.content || element.textContent);
  });
}

function interleavedBlockPairs(originalBlocks = [], translatedBlocks = []) {
  const hasAlignmentGroups = translatedBlocks.some(block => (
    Array.isArray(block?.sourceIds) && block.sourceIds.length
  ));
  if (hasAlignmentGroups) {
    const originalById = new Map(originalBlocks.map(block => [String(block.id || ""), block]));
    const usedOriginals = new Set();
    const pairs = translatedBlocks.map((translated, index) => {
      const originals = (translated.sourceIds || [])
        .map(id => originalById.get(String(id)))
        .filter(Boolean);
      originals.forEach(block => usedOriginals.add(block));
      return {
        index,
        original: originals[0] || null,
        translated,
        originals,
        translations: [translated],
      };
    });
    originalBlocks.forEach(original => {
      if (!usedOriginals.has(original)) {
        pairs.push({
          index: pairs.length,
          original,
          translated: null,
          originals: [original],
          translations: [],
        });
      }
    });
    return pairs;
  }
  const count = Math.max(originalBlocks.length, translatedBlocks.length);
  return Array.from({ length: count }, (_, index) => ({
    index,
    original: originalBlocks[index] || null,
    translated: translatedBlocks[index] || null,
  }));
}

function interleavedHtmlBlockPairs(originalBlocks = [], translatedBlocks = []) {
  const pairId = block => block?.dataset?.readerPairId || block?.getAttribute?.("data-reader-pair-id") || "";
  const translatedById = new Map();
  translatedBlocks.forEach(block => {
    const id = pairId(block);
    if (!id) return;
    if (!translatedById.has(id)) translatedById.set(id, []);
    translatedById.get(id).push(block);
  });
  const commonIds = originalBlocks.map(pairId).filter(id => id && translatedById.has(id));
  if (!commonIds.length) return interleavedBlockPairs(originalBlocks, translatedBlocks);
  const usedTranslations = new Set();
  const usedOriginals = new Set();
  const emittedIds = new Set();
  const pairs = [];
  originalBlocks.forEach(original => {
    const id = pairId(original);
    if (!id || emittedIds.has(id)) {
      if (!id) pairs.push({ index: pairs.length, original, translated: null });
      return;
    }
    emittedIds.add(id);
    const originals = originalBlocks.filter(candidate => pairId(candidate) === id);
    const translations = translatedById.get(id) || [];
    originals.forEach(block => usedOriginals.add(block));
    translations.forEach(block => usedTranslations.add(block));
    const pair = {
      index: pairs.length,
      original: originals[0] || null,
      translated: translations[0] || null,
    };
    if (originals.length > 1 || translations.length > 1) {
      pair.originals = originals;
      pair.translations = translations;
    }
    pairs.push(pair);
  });
  originalBlocks.forEach(original => {
    if (!usedOriginals.has(original) && pairId(original)) {
      pairs.push({ index: pairs.length, original, translated: null });
    }
  });
  translatedBlocks.forEach(translated => {
    if (!usedTranslations.has(translated)) {
      pairs.push({ index: pairs.length, original: null, translated });
    }
  });
  return pairs;
}

function renderMarkdown() {
  const isLiveTranslation = readerState.view === "liveTranslation";
  const blocks = isLiveTranslation ? (readerState.content.blocks || []) : activeBlocks();
  const isSideBySide = readerState.view === "sideBySide";
  const isPaired = readerState.view === "interleaved" || isSideBySide;
  const pairs = isPaired
    ? interleavedBlockPairs(readerState.content.blocks || [], readerState.content.translatedBlocks || [])
    : [];
  const pairBlocksHtml = (blocks, language, idPrefix) => blocks.map(block => (
    `<section class="paper-block ${block.type}" data-block-id="${block.id}" data-pair-language="${language}">${blockHtml(block, idPrefix)}</section>`
  )).join("");
  $("#paperContent").className = `paper-content${isPaired ? " paired-content" : ""}`;
  const title = readerState.document?.title || readerState.content?.title || "阅读文档";
  const body = isPaired
    ? pairs.map(pair => `
      <section id="interleaved-pair-${pair.index}" class="interleaved-pair ${isSideBySide ? "side-by-side-pair" : ""}">
        ${pair.original ? `<div class="interleaved-side interleaved-original">${pairBlocksHtml(pair.originals || [pair.original], "original", "original-")}</div>` : ""}
        ${pair.translated ? `<div class="interleaved-side interleaved-translated">${pairBlocksHtml(pair.translations || [pair.translated], "translated", "translated-")}</div>` : ""}
      </section>
    `).join("")
    : blocks.map(block => {
      const liveEligible = isLiveTranslation && !["rule", "code", "math"].includes(block.type) && Boolean(block.content?.trim());
      return `<section class="paper-block ${block.type}" data-block-id="${block.id}"${liveEligible ? ` data-live-block-id="${escapeHtml(block.id)}"` : ""}>${blockHtml(block)}</section>${liveEligible ? liveTranslationMarkup(block.id) : ""}`;
    }).join("");
  $("#paperContent").innerHTML = `
    <article class="reader-page-layout">
      <header class="reader-page-header"><span>${escapeHtml(title)}</span></header>
      <main class="reader-page-body ${isPaired ? "interleaved-reader-body" : ""} ${isSideBySide ? "side-by-side-reader-body" : ""}">${body}</main>
      <footer class="reader-page-footer"><span>Paper Lens</span></footer>
    </article>
  `;
  const headings = (readerState.content.blocks || []).filter(block => block.type === "heading");
  $("#outline").innerHTML = headings.map(block => {
    const pairIndex = isPaired
      ? pairs.findIndex(pair => (pair.originals || [pair.original]).includes(block))
      : null;
    const target = isPaired ? `interleaved-pair-${pairIndex}` : block.id;
    return `<button data-target="${target}" class="outline-level-${Math.min(block.level || 1, 3)}">${escapeHtml(block.content)}</button>`;
  }).join("") || "<p class=\"muted\">未检测到标题。</p>";
  $("#outline").querySelectorAll("[data-target]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.target)?.scrollIntoView({ behavior: "smooth", block: "start" })));
  $("#zoomControls").classList.add("hidden");
  if (isLiveTranslation) wireLiveMarkdownTranslation();
  wireMarkdownParagraphSpeech();
  renderReaderDecorations();
  typesetMath();
}

function typesetMath() {
  if (!window.MathJax?.typesetPromise) return;
  window.MathJax.typesetPromise([$("#paperContent"), $("#answerPanel")]).then(() => {
    $("#paperContent").querySelectorAll(".math-source[data-latex]").forEach(source => {
      const rendered = source.querySelector("mjx-container");
      if (rendered) rendered.dataset.latex = source.dataset.latex;
    });
    renderReaderDecorations();
  }).catch(() => {});
}

async function loadPdfJs() {
  if (!loadPdfJs.promise) {
    loadPdfJs.promise = import(`${PDFJS_BASE}/build/pdf.mjs`).then(pdfjs => {
      pdfjs.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}/build/pdf.worker.mjs`;
      return pdfjs;
    });
  }
  return loadPdfJs.promise;
}

function pdfDocumentOptions(source) {
  return {
    ...source,
    cMapUrl: `${PDFJS_BASE}/web/cmaps/`,
    cMapPacked: true,
    standardFontDataUrl: `${PDFJS_BASE}/web/standard_fonts/`,
    wasmUrl: `${PDFJS_BASE}/web/wasm/`,
  };
}

function joinSpeechText(left, right) {
  const before = String(left || "").trimEnd();
  const after = String(right || "").trimStart();
  if (!before) return after;
  if (!after) return before;
  if (/[\u3400-\u9fff]$/.test(before) && /^[\u3400-\u9fff，。！？；：、）》】]/.test(after)) {
    return before + after;
  }
  if (/[-\u2010]$/.test(before) && /^[a-z]/.test(after)) {
    return before.slice(0, -1) + after;
  }
  if (/^[,.;:!?，。！？；：、)\]》】]/.test(after)) return before + after;
  return `${before} ${after}`;
}

function pdfParagraphGroups(items = []) {
  const lines = [];
  let current = null;
  const finishLine = () => {
    if (current?.text.trim()) lines.push(current);
    current = null;
  };
  items.forEach((item, itemIndex) => {
    const text = String(item?.str || "");
    const transform = Array.isArray(item?.transform) ? item.transform : [];
    const x = Number(transform[4]) || 0;
    const y = Number(transform[5]) || 0;
    const height = Math.max(Math.abs(Number(item?.height) || Number(transform[3]) || 10), 1);
    if (current && Math.abs(y - current.y) > Math.max(height, current.height) * 0.45) finishLine();
    if (text.trim()) {
      const width = Math.max(Number(item?.width) || text.length * height * 0.45, 1);
      if (!current) current = { text: "", itemIndexes: [], x, y, right: x + width, height };
      current.text = joinSpeechText(current.text, text);
      current.itemIndexes.push(itemIndex);
      current.x = Math.min(current.x, x);
      current.right = Math.max(current.right, x + width);
      current.height = Math.max(current.height, height);
    }
    if (item?.hasEOL) finishLine();
  });
  finishLine();
  if (!lines.length) return [];

  const pageRight = Math.max(...lines.map(line => line.right));
  const paragraphs = [];
  lines.forEach((line, index) => {
    const previousLine = lines[index - 1];
    const currentParagraph = paragraphs[paragraphs.length - 1];
    const verticalGap = previousLine ? Math.abs(previousLine.y - line.y) : 0;
    const typicalHeight = previousLine ? Math.max(previousLine.height, line.height) : line.height;
    const indented = previousLine && line.x - previousLine.x > typicalHeight * 1.2;
    const previousEndsEarly = previousLine && previousLine.right < pageRight - typicalHeight * 2.2;
    const previousEndsSentence = previousLine && /[.!?。！？]["'”’）》】]?$/.test(previousLine.text);
    const startsList = /^(?:[-•●▪◦]|\(?\d+[.)]|[（(]?[一二三四五六七八九十]+[）)])\s*/.test(line.text);
    const boundary = !currentParagraph
      || verticalGap > typicalHeight * 1.55
      || indented
      || startsList
      || (previousEndsEarly && previousEndsSentence);
    if (boundary) {
      paragraphs.push({ text: line.text, itemIndexes: [...line.itemIndexes] });
    } else {
      currentParagraph.text = joinSpeechText(currentParagraph.text, line.text);
      currentParagraph.itemIndexes.push(...line.itemIndexes);
    }
  });
  return paragraphs.filter(paragraph => paragraph.text.trim());
}

function clearSpeechHighlight() {
  readerState.speechTarget?.elements?.forEach(element => element?.classList?.remove("is-speaking-paragraph"));
  readerState.speechTarget = null;
}

function readerSpeechAvailable(readerDocument = readerState.document) {
  return new Set(["pdf", "markdown", "html"]).has(String(readerDocument?.sourceType || "").toLowerCase());
}

function updateSpeechControl() {
  const startButton = $("#startSpeech");
  const stopButton = $("#stopSpeech");
  const rateControl = $("#speechRateControl");
  const rateSelect = $("#speechPlaybackRate");
  if (!startButton || !stopButton || !rateControl || !rateSelect) return;
  const speechAvailable = readerSpeechAvailable();
  const speech = readerState.config?.speech;
  startButton.classList.toggle("hidden", !speechAvailable);
  startButton.classList.toggle("active", readerState.speechClickEnabled);
  startButton.setAttribute("aria-pressed", String(readerState.speechClickEnabled));
  const speechEnabled = readerState.speechClickEnabled;
  const speechAction = `${speechEnabled ? "关闭" : "开启"}点击段落朗读`;
  const speechIcon = startButton.querySelector("[data-speech-icon]");
  const speechLabel = startButton.querySelector("[data-speech-label]");
  if (speechIcon) speechIcon.textContent = speechEnabled ? "🔊" : "🔇";
  if (speechLabel) speechLabel.textContent = speechEnabled ? "点击朗读：开" : "点击朗读：关";
  startButton.setAttribute("aria-label", speechAction);
  startButton.title = `${speechAction}${
    speech?.configured ? "" : "（Azure Speech 尚未配置）"
  }`;
  rateControl.classList.toggle("hidden", !speechAvailable);
  rateSelect.value = String(readerState.speechPlaybackRate);
  rateSelect.disabled = speech?.configured !== true;
  stopButton.classList.toggle("hidden", !speechAvailable);
  stopButton.disabled = !readerState.speechSource && !readerState.speechAbortController;
  stopButton.title = speech?.configured
    ? `停止 Azure 朗读（${speech.region} · ${speech.voice}）`
    : "请先在“AI 与语音配置”中保存 Azure Speech API Key";
  const hint = $("#readerInteractionHint");
  if (hint && speechAvailable) {
    hint.textContent = speech?.configured
      ? (readerState.speechClickEnabled
        ? `点击原文或译文段落即可朗读 · 当前 ${readerState.speechPlaybackRate}×`
        : "点击段落朗读已关闭，可用工具栏开关重新开启")
      : "朗读需先在“AI 与语音配置”中保存 Azure Speech API Key";
  } else if (hint) {
    hint.textContent = "划选内容以获得针对性解读";
  }
  document.body.classList.toggle("speech-click-enabled", speechAvailable && readerState.speechClickEnabled);
  const frameDocument = $("#readerDocumentFrame")?.contentDocument;
  frameDocument?.documentElement?.classList.toggle("reader-speech-enabled", speechAvailable && readerState.speechClickEnabled);
}

function stopParagraphSpeech(status = "") {
  readerState.speechRequestId += 1;
  readerState.speechAbortController?.abort();
  readerState.speechAbortController = null;
  if (readerState.speechSource) {
    readerState.speechSource.onended = null;
    try {
      readerState.speechSource.stop();
    } catch {
      // An already-ended AudioBufferSourceNode cannot be stopped twice.
    }
    readerState.speechSource.disconnect();
    readerState.speechSource = null;
  }
  clearSpeechHighlight();
  updateSpeechControl();
  if (status) $("#readingStatus").textContent = status;
}

function toggleParagraphSpeechClick() {
  if (!readerSpeechAvailable()) return;
  readerState.speechClickEnabled = !readerState.speechClickEnabled;
  if (!readerState.speechClickEnabled) stopParagraphSpeech();
  persistSelectionSettings(readerState.speechClickEnabled
    ? "点击段落朗读已开启。"
    : "点击段落朗读已关闭。");
  $("#readingStatus").textContent = readerState.speechClickEnabled
    ? "点击段落朗读已开启"
    : "点击段落朗读已关闭";
  updateSpeechControl();
}

function updateSpeechPlaybackRate(value) {
  readerState.speechPlaybackRate = normalizeSpeechPlaybackRate(value);
  if (readerState.speechSource || readerState.speechAbortController) stopParagraphSpeech();
  persistSelectionSettings(`Azure 朗读语速已设为 ${readerState.speechPlaybackRate}×。`);
  $("#readingStatus").textContent = `Azure 朗读语速已设为 ${readerState.speechPlaybackRate}×`;
  updateSpeechControl();
}

function speechTargetFromElement(element, sourceDocument = document) {
  if (!element || !readerSpeechAvailable()) return null;
  if (element.closest?.("pre,code,math,svg")) return null;
  const pdfSpan = element.closest?.(".textLayer span[data-pdf-paragraph]");
  if (pdfSpan) {
    const layer = pdfSpan.closest(".textLayer");
    const paragraphId = pdfSpan.dataset.pdfParagraph;
    const paragraph = layer?._readerSpeechParagraphs?.[Number(paragraphId)];
    if (!paragraph?.text) return null;
    return {
      key: `pdf:${pdfSpan.closest("[data-pdf-page]")?.dataset.pdfPage || ""}:${paragraphId}`,
      text: paragraph.text,
      elements: [...layer.querySelectorAll(`span[data-pdf-paragraph="${paragraphId}"]`)],
    };
  }
  const block = element.closest?.("[data-reader-speech-id],p,li,figcaption,blockquote,td,th,h1,h2,h3,h4,h5,h6");
  const text = block?.textContent?.replace(/\s+/g, " ").trim();
  if (block && text) {
    if (!block.dataset.readerSpeechId) {
      block.dataset.readerSpeechId = `speech-${Math.random().toString(36).slice(2)}`;
    }
    const kind = sourceDocument === document ? "markdown" : "html";
    return { key: `${kind}:${block.dataset.readerSpeechId}`, text, elements: [block] };
  }
  return null;
}

async function speakParagraph(target) {
  if (readerState.config?.speech?.configured !== true) {
    $("#readingStatus").textContent = "请先在“AI 与语音配置”中保存 Azure Speech API Key";
    updateSpeechControl();
    return;
  }
  if (readerState.speechTarget?.key === target.key) {
    stopParagraphSpeech("已停止朗读");
    return;
  }
  stopParagraphSpeech();
  const requestId = readerState.speechRequestId;
  readerState.speechTarget = target;
  target.elements.forEach(element => element.classList.add("is-speaking-paragraph"));
  updateSpeechControl();
  $("#readingStatus").textContent = "正在生成段落语音…";
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    stopParagraphSpeech("当前浏览器不支持音频播放");
    return;
  }
  readerState.speechAudioContext ||= new AudioContextClass();
  try {
    await readerState.speechAudioContext.resume();
  } catch {
    stopParagraphSpeech("浏览器阻止了音频播放");
    return;
  }
  const controller = new AbortController();
  readerState.speechAbortController = controller;
  updateSpeechControl();
  try {
    const response = await fetch(`/api/reader/documents/${readerState.document.id}/speech`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: target.text, rate: readerState.speechPlaybackRate }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || "段落语音生成失败。");
    }
    const encodedAudio = await response.arrayBuffer();
    const audioBuffer = await readerState.speechAudioContext.decodeAudioData(encodedAudio.slice(0));
    if (requestId !== readerState.speechRequestId) return;
    const source = readerState.speechAudioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(readerState.speechAudioContext.destination);
    readerState.speechAbortController = null;
    readerState.speechSource = source;
    source.onended = () => {
      if (readerState.speechSource !== source) return;
      readerState.speechSource = null;
      clearSpeechHighlight();
      updateSpeechControl();
      $("#readingStatus").textContent = "朗读完成";
    };
    source.start();
    updateSpeechControl();
    $("#readingStatus").textContent = `正在朗读 · ${readerState.config.speech.voice} · ${readerState.speechPlaybackRate}×`;
  } catch (error) {
    if (error.name !== "AbortError" && requestId === readerState.speechRequestId) {
      stopParagraphSpeech(error.message || "段落朗读失败");
    }
  } finally {
    if (readerState.speechAbortController === controller) {
      readerState.speechAbortController = null;
      updateSpeechControl();
    }
  }
}

function handleParagraphSpeechClick(event, sourceDocument = document) {
  if (!readerState.speechClickEnabled) return;
  if (event.target.closest?.("a,button,input,select,textarea,label")) return;
  const selection = sourceDocument.defaultView?.getSelection?.();
  if (selection && !selection.isCollapsed && selection.toString().trim()) return;
  const target = speechTargetFromElement(event.target, sourceDocument);
  if (target) {
    void speakParagraph(target);
  }
}

function wireHtmlParagraphSpeech(frameDocument) {
  if (!readerSpeechAvailable()) return;
  const style = frameDocument.createElement("style");
  style.dataset.readerSpeechStyle = "true";
  style.textContent = `
    html.reader-speech-enabled [data-reader-speech-id],
    html.reader-speech-enabled p,
    html.reader-speech-enabled li,
    html.reader-speech-enabled figcaption,
    html.reader-speech-enabled blockquote,
    html.reader-speech-enabled td,
    html.reader-speech-enabled th,
    html.reader-speech-enabled h1,
    html.reader-speech-enabled h2,
    html.reader-speech-enabled h3,
    html.reader-speech-enabled h4,
    html.reader-speech-enabled h5,
    html.reader-speech-enabled h6{cursor:pointer}
    .is-speaking-paragraph{outline:2px solid #157262!important;outline-offset:3px;background:rgba(228,242,238,.72)!important}
  `;
  (frameDocument.head || frameDocument.documentElement).appendChild(style);
  frameDocument.documentElement.classList.toggle("reader-speech-enabled", readerState.speechClickEnabled);
  htmlInterleavableBlocks(frameDocument)
    .filter(block => block.isConnected)
    .forEach((block, index) => {
      const text = block.textContent?.replace(/\s+/g, " ").trim() || "";
      if (!text || text.length > 12000 || block.matches("pre,code,math,svg")) return;
      block.dataset.readerSpeechId ||= `speech-block-${index + 1}`;
      block.title = "单击朗读本段";
    });
  frameDocument.addEventListener("click", event => handleParagraphSpeechClick(event, frameDocument));
}

function wireMarkdownParagraphSpeech() {
  if (!readerSpeechAvailable()) return;
  $("#paperContent").querySelectorAll("p,li,figcaption,blockquote,td,th,h1,h2,h3,h4,h5,h6")
    .forEach((block, index) => {
      if (block.closest("pre,code,math,svg")) return;
      const text = block.textContent?.replace(/\s+/g, " ").trim() || "";
      if (!text || text.length > 12000) return;
      block.dataset.readerSpeechId ||= `markdown-speech-block-${index + 1}`;
      block.title = "单击朗读本段";
    });
}

async function pdfOutlineEntries(pdf) {
  const outline = await pdf.getOutline();
  if (!outline?.length) return [];
  const entries = [];
  async function visit(items, level) {
    for (const item of items) {
      try {
        const destination = typeof item.dest === "string" ? await pdf.getDestination(item.dest) : item.dest;
        const reference = destination?.[0];
        const pageIndex = typeof reference === "object" ? await pdf.getPageIndex(reference) : Number(reference || 0);
        entries.push({ title: item.title || `第 ${pageIndex + 1} 页`, page: pageIndex + 1, level });
      } catch {
        // Ignore one malformed outline entry without hiding the remaining entries.
      }
      if (item.items?.length) await visit(item.items, Math.min(3, level + 1));
    }
  }
  await visit(outline, 1);
  return entries;
}

async function renderPdf(url) {
  const token = ++readerState.renderToken;
  const paperContent = $("#paperContent");
  paperContent.className = "paper-content pdf-content";
  paperContent.innerHTML = `<p class="pdf-loading">正在载入 PDF 文本层…</p>`;
  $("#zoomControls").classList.remove("hidden");
  $("#zoomLevel").textContent = `${Math.round(readerState.pdfScale * 100 / 1.15)}%`;
  $("#outline").innerHTML = "<p class=\"muted\">正在读取页码…</p>";
  try {
    const pdfjs = await loadPdfJs();
    if (!readerState.pdfDocument || readerState.pdfDocument.loadingUrl !== url) {
      const loadingTask = pdfjs.getDocument(pdfDocumentOptions({ url }));
      readerState.pdfLoadingTask = loadingTask;
      readerState.pdfDocument = await loadingTask.promise;
      readerState.pdfDocument.loadingUrl = url;
    }
    if (token !== readerState.renderToken) return;
    const pages = Array.from({ length: readerState.pdfDocument.numPages }, (_, index) => index + 1);
    const outline = await pdfOutlineEntries(readerState.pdfDocument);
    const navigation = outline.length ? outline : pages.map(page => ({ title: `第 ${page} 页`, page, level: 1 }));
    $("#outline").innerHTML = navigation.map(item => `<button data-page="${item.page}" class="outline-level-${item.level}">${escapeHtml(item.title)}</button>`).join("");
    $("#outline").querySelectorAll("[data-page]").forEach(button => button.addEventListener("click", () => $(`[data-pdf-page="${button.dataset.page}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" })));
    const contentStyle = getComputedStyle(paperContent);
    const horizontalPadding = (parseFloat(contentStyle.paddingLeft) || 0) + (parseFloat(contentStyle.paddingRight) || 0);
    const availablePageWidth = Math.max(240, paperContent.clientWidth - horizontalPadding - 4);
    readerState.pdfRenderWidth = paperContent.clientWidth;
    paperContent.innerHTML = "";
    for (const pageNumber of pages) {
      if (token !== readerState.renderToken) return;
      const page = await readerState.pdfDocument.getPage(pageNumber);
      const naturalViewport = page.getViewport({ scale: 1 });
      const scale = pdfFitScale(availablePageWidth, naturalViewport.width, readerState.pdfScale / 1.15);
      const viewport = page.getViewport({ scale });
      const pageElement = document.createElement("section");
      pageElement.className = "pdf-page";
      pageElement.dataset.pdfPage = String(pageNumber);
      pageElement.style.width = `${viewport.width}px`;
      pageElement.style.height = `${viewport.height}px`;
      pageElement.style.setProperty("--total-scale-factor", viewport.scale);
      const canvas = document.createElement("canvas");
      const outputScale = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      pageElement.appendChild(canvas);
      const textLayer = document.createElement("div");
      textLayer.className = "textLayer";
      textLayer.style.setProperty("--total-scale-factor", viewport.scale);
      pageElement.appendChild(textLayer);
      paperContent.appendChild(pageElement);
      await Promise.all([
        page.render({
          canvasContext: canvas.getContext("2d"),
          viewport,
          transform: outputScale === 1 ? null : [outputScale, 0, 0, outputScale, 0, 0],
        }).promise,
        renderPdfTextLayer(pdfjs, page, viewport, textLayer, pageElement),
      ]);
    }
    renderReaderDecorations();
  } catch (error) {
    $("#paperContent").innerHTML = `<div class="pdf-error"><strong>PDF 阅读层载入失败。</strong><p>${escapeHtml(error.message)}</p><a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">在浏览器中打开原始 PDF</a></div>`;
  }
}

async function renderPdfTextLayer(pdfjs, page, viewport, target, pageElement) {
  const content = await page.getTextContent();
  pageElement.dataset.pageText = content.items
    .map(item => `${item.str || ""}${item.hasEOL ? "\n" : " "}`)
    .join("")
    .replace(/[ \t]+\n/g, "\n")
    .trim();
  const layer = new pdfjs.TextLayer({ textContentSource: content, container: target, viewport });
  await layer.render();
  const paragraphs = pdfParagraphGroups(content.items);
  target._readerSpeechParagraphs = paragraphs;
  const paragraphByItem = new Map();
  paragraphs.forEach((paragraph, paragraphIndex) => {
    paragraph.itemIndexes.forEach(itemIndex => paragraphByItem.set(itemIndex, paragraphIndex));
  });
  const renderedSpans = [...target.querySelectorAll("span:not(.markedContent)")];
  let renderedIndex = 0;
  content.items.forEach((item, itemIndex) => {
    if (!String(item?.str || "").trim()) return;
    const span = renderedSpans[renderedIndex];
    renderedIndex += 1;
    const paragraphIndex = paragraphByItem.get(itemIndex);
    if (span && paragraphIndex !== undefined) {
      span.dataset.pdfParagraph = String(paragraphIndex);
      span.title = "单击朗读本段";
    }
  });
}

function htmlSemanticNodes(sourceDocument) {
  return [...sourceDocument.querySelectorAll("h1,h2,h3,h4,h5,h6,p,li,figcaption,blockquote,td,th")]
    .filter(node => node.textContent.trim());
}

function htmlSection(nodes, index) {
  const language = nodes[index]?.closest?.("[data-pair-language]")?.dataset.pairLanguage || "";
  for (let cursor = index; cursor >= 0; cursor -= 1) {
    const candidateLanguage = nodes[cursor].closest?.("[data-pair-language]")?.dataset.pairLanguage || "";
    if (language && candidateLanguage !== language) continue;
    if (/^H[1-6]$/.test(nodes[cursor].tagName)) return nodes[cursor].textContent.trim();
  }
  return "HTML 文档选区";
}

function htmlInterleavableBlocks(sourceDocument) {
  const blocks = [];
  const wholeBlockTags = new Set([
    "H1", "H2", "H3", "H4", "H5", "H6",
    "P", "BLOCKQUOTE", "PRE", "TABLE", "FIGURE", "UL", "OL",
  ]);
  const containerTags = new Set([
    "BODY", "MAIN", "ARTICLE", "SECTION", "DIV", "HEADER", "FOOTER", "ASIDE", "NAV",
  ]);
  const ignoredTags = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "LINK", "META",
    "NAV", "FOOTER", "ASIDE",
  ]);
  const isHidden = node => {
    const style = String(node.getAttribute?.("style") || "").replace(/\s+/g, "").toLowerCase();
    return (
      node.hasAttribute?.("hidden")
      || node.getAttribute?.("aria-hidden") === "true"
      || style.includes("display:none")
      || style.includes("visibility:hidden")
    );
  };
  const hasContent = node => (
    Boolean(node.textContent?.trim())
    || Boolean(node.querySelector?.("img,svg,math,mjx-container,canvas,video"))
  );
  const hasOwnInlineContent = element => [...element.childNodes].some(node => {
    if (node.nodeType === 3) return Boolean(node.textContent.trim());
    if (node.nodeType !== 1 || ignoredTags.has(node.tagName) || isHidden(node)) return false;
    return !containerTags.has(node.tagName) && !wholeBlockTags.has(node.tagName);
  });
  const visit = parent => {
    [...parent.childNodes].forEach(node => {
      if (node.nodeType === 3) {
        const text = node.textContent.trim();
        if (!text) return;
        const wrapper = sourceDocument.createElement("div");
        wrapper.textContent = text;
        blocks.push(wrapper);
        return;
      }
      if (node.nodeType !== 1 || ignoredTags.has(node.tagName) || isHidden(node) || !hasContent(node)) return;
      if (wholeBlockTags.has(node.tagName)) {
        blocks.push(node);
        return;
      }
      if (containerTags.has(node.tagName) && !hasOwnInlineContent(node)) {
        visit(node);
        return;
      }
      blocks.push(node);
    });
  };
  if (sourceDocument.body) visit(sourceDocument.body);
  if (sourceDocument.body) {
    const readableBody = sourceDocument.body.cloneNode(true);
    readableBody.querySelectorAll([...ignoredTags].join(",")).forEach(node => node.remove());
    readableBody.querySelectorAll("[hidden],[aria-hidden='true'],[style]").forEach(node => {
      if (isHidden(node)) node.remove();
    });
    const compactLength = value => String(value || "").replace(/\s+/g, "").length;
    const sourceTextLength = compactLength(readableBody.textContent);
    const extractedTextLength = blocks.reduce(
      (total, block) => total + compactLength(block.textContent),
      0,
    );
    if (sourceTextLength && extractedTextLength < sourceTextLength * 0.98) {
      const fallbackBlock = sourceDocument.createElement("div");
      fallbackBlock.className = "interleaved-document-fallback";
      fallbackBlock.innerHTML = readableBody.innerHTML;
      return [fallbackBlock];
    }
  }
  return blocks;
}

function wireLiveHtmlTranslation(frameDocument) {
  const style = frameDocument.createElement("style");
  style.dataset.readerLiveTranslationStyle = "true";
  style.textContent = `
    .live-translation-source{cursor:pointer;transition:outline-color .15s}
    .live-translation-source:hover,.live-translation-source:focus-visible{outline:1px solid rgba(21,114,98,.48);outline-offset:3px}
    .live-translation-source>:last-child{margin-bottom:.2em}
    .live-paragraph-translation{margin:0 0 1.15em;color:inherit;font:inherit;line-height:inherit}
    .live-paragraph-translation p{margin:0}
    .live-paragraph-translation.is-loading{color:#718680;font-style:italic}
    .live-paragraph-translation.is-error{color:#a5483d}
    .live-paragraph-translation>:first-child{margin-top:0}.live-paragraph-translation>:last-child{margin-bottom:0}
  `;
  (frameDocument.head || frameDocument.documentElement).appendChild(style);
  const blocks = htmlInterleavableBlocks(frameDocument).filter(block => block.isConnected);
  blocks.forEach((block, index) => {
    const sourceText = block.textContent?.trim() || "";
    if (!sourceText || sourceText.length > 12000 || block.matches("pre,code,math,svg")) return;
    const blockId = block.dataset.readerPairId || `live-html-${index + 1}`;
    wireLiveSourceElement(block, blockId, sourceText);
  });
}

function mountHtmlFrame({ url = "", srcdoc = "", interleaved = false, liveTranslation = false }, token = ++readerState.renderToken) {
  if (token !== readerState.renderToken) return;
  readerState.pdfLoadingTask?.destroy?.().catch(() => {});
  readerState.pdfLoadingTask = null;
  readerState.pdfDocument = null;
  $("#zoomControls").classList.add("hidden");
  $("#paperContent").className = "paper-content document-frame-content";
  $("#paperContent").innerHTML = `<iframe id="readerDocumentFrame" class="reader-document-frame" title="${interleaved ? "原文译文段落对照" : liveTranslation ? "点击段落实时翻译" : "论文正文"}" sandbox="allow-same-origin" referrerpolicy="no-referrer"></iframe>`;
  const frame = $("#readerDocumentFrame");
  if (!frame) {
    $("#readingStatus").textContent = "HTML 阅读视图初始化失败";
    return;
  }
  frame.addEventListener("load", event => {
    try {
      const frameDocument = event.target.contentDocument;
      applyTypographyToDocument(frameDocument);
      const headingSelector = interleaved
        ? ".interleaved-original h1,.interleaved-original h2,.interleaved-original h3,.interleaved-original h4,.interleaved-original h5,.interleaved-original h6"
        : "h1,h2,h3,h4,h5,h6";
      const headings = [...frameDocument.querySelectorAll(headingSelector)]
        .filter(node => node.textContent.trim());
      headings.forEach((heading, index) => { if (!heading.id) heading.id = `reader-heading-${index + 1}`; });
      $("#outline").innerHTML = headings.map(heading => (
        `<button data-html-target="${escapeHtml(heading.id)}" class="outline-level-${Math.min(Number(heading.tagName.slice(1)), 3)}">${escapeHtml(heading.textContent.trim())}</button>`
      )).join("") || "<p class=\"muted\">未检测到标题。</p>";
      $("#outline").querySelectorAll("[data-html-target]").forEach(button => button.addEventListener("click", () => {
        frameDocument.getElementById(button.dataset.htmlTarget)?.scrollIntoView({ behavior: "smooth", block: "start" });
      }));
      frameDocument.addEventListener("mouseup", () => setTimeout(() => captureSelection(frameDocument, event.target), 0));
      frameDocument.addEventListener("touchend", () => setTimeout(() => captureSelection(frameDocument, event.target), 0));
      wireHtmlParagraphSpeech(frameDocument);
      if (liveTranslation) wireLiveHtmlTranslation(frameDocument);
      renderReaderDecorations(frameDocument);
    } catch {
      $("#readingStatus").textContent = "无法读取选区";
    }
  });
  if (interleaved) frame.srcdoc = srcdoc;
  else frame.src = url;
}

function renderHtmlDocument(url, { liveTranslation = false } = {}) {
  const token = ++readerState.renderToken;
  $("#outline").innerHTML = "<p class=\"muted\">正在读取 HTML 文档结构…</p>";
  mountHtmlFrame({ url, liveTranslation }, token);
}

async function renderInterleavedHtmlDocument() {
  const token = ++readerState.renderToken;
  const isSideBySide = readerState.view === "sideBySide";
  readerState.pdfLoadingTask?.destroy?.().catch(() => {});
  readerState.pdfLoadingTask = null;
  readerState.pdfDocument = null;
  $("#zoomControls").classList.add("hidden");
  $("#outline").innerHTML = `<p class="muted">正在生成${isSideBySide ? "左右" : "段落"}对照…</p>`;
  $("#paperContent").className = "paper-content document-frame-content";
  $("#paperContent").innerHTML = "<div class=\"pdf-loading\">正在配对原文与译文段落…</div>";
  try {
    const [originalResponse, translatedResponse] = await Promise.all([
      fetch(readerState.content.documentUrl),
      fetch(readerState.content.translatedDocumentUrl),
    ]);
    if (!originalResponse.ok || !translatedResponse.ok) throw new Error("原文或译文 HTML 无法读取。");
    const [originalText, translatedText] = await Promise.all([
      originalResponse.text(),
      translatedResponse.text(),
    ]);
    if (token !== readerState.renderToken) return;
    const parser = new DOMParser();
    const originalDocument = parser.parseFromString(originalText, "text/html");
    const translatedDocument = parser.parseFromString(translatedText, "text/html");
    const pairs = interleavedHtmlBlockPairs(
      htmlInterleavableBlocks(originalDocument),
      htmlInterleavableBlocks(translatedDocument),
    );
    const baseUrl = new URL(".", new URL(readerState.content.documentUrl, window.location.href)).href;
    const inheritedStyles = [...originalDocument.querySelectorAll("head style")]
      .map(style => style.outerHTML)
      .join("");
    const pairMarkup = pairs.map(pair => `
      <section class="interleaved-pair ${isSideBySide ? "side-by-side-pair" : ""}" id="interleaved-html-pair-${pair.index}">
        ${pair.original ? `<div class="interleaved-side interleaved-original" data-pair-language="original">${(pair.originals || [pair.original]).map(block => block.outerHTML).join("")}</div>` : ""}
        ${pair.translated ? `<div class="interleaved-side interleaved-translated" data-pair-language="translated">${(pair.translations || [pair.translated]).map(block => block.outerHTML).join("")}</div>` : ""}
      </section>
    `).join("");
    const srcdoc = `<!doctype html><html><head><meta charset="utf-8"><base href="${escapeHtml(baseUrl)}">${inheritedStyles}<style>
      :root{color:#243b38;background:#fff;font-family:Georgia,"Times New Roman","Songti SC",SimSun,serif}
      *{box-sizing:border-box}html,body{width:100%;max-width:none;min-width:0}body{margin:0;padding:34px 42px 70px;line-height:1.75}
      img,svg,video{max-width:100%;height:auto}table{max-width:100%;border-collapse:collapse}th,td{padding:7px;border:1px solid #d9e4e1}
      .interleaved-pair,.interleaved-side{display:block;width:100%;max-width:none;min-width:0;margin:0;padding:0;border:0;border-radius:0;background:transparent;scroll-margin-top:18px}
      .interleaved-side+.interleaved-side{margin-top:0}
      body.side-by-side-document{width:100%;max-width:none;padding-right:20px!important;padding-left:20px!important}
      .side-by-side-pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);column-gap:20px;align-items:start}
      .side-by-side-pair>.interleaved-original{grid-column:1}.side-by-side-pair>.interleaved-translated{grid-column:2}
      .side-by-side-pair>.interleaved-side{width:auto;min-width:0;overflow-wrap:anywhere}.side-by-side-pair>.interleaved-side>*{max-width:100%!important}
      .interleaved-document-fallback,.interleaved-document-fallback>main,.interleaved-document-fallback>article{width:100%!important;max-width:none!important}
      @media(max-width:420px){.side-by-side-pair{display:block}.side-by-side-pair>.interleaved-side{margin-top:0}}
    </style></head><body class="${isSideBySide ? "side-by-side-document" : ""}">${pairMarkup}</body></html>`;
    mountHtmlFrame({ srcdoc, interleaved: true }, token);
  } catch (error) {
    if (token !== readerState.renderToken) return;
    $("#paperContent").innerHTML = `<div class="pdf-error"><strong>段落对照生成失败。</strong><p>${escapeHtml(error.message)}</p></div>`;
    $("#readingStatus").textContent = "对照生成失败";
  }
}

function renderPaper() {
  hideSelectionMenu();
  stopParagraphSpeech();
  const kind = readerState.content.renderKind || "markdown";
  if (kind === "pdf") {
    const url = readerState.view === "translated" ? readerState.content.translatedDocumentUrl : readerState.content.documentUrl;
    return renderPdf(url);
  }
  if (kind === "html") {
    if (readerState.view === "interleaved" || readerState.view === "sideBySide") return renderInterleavedHtmlDocument();
    const url = readerState.view === "translated" ? readerState.content.translatedDocumentUrl : readerState.content.documentUrl;
    return renderHtmlDocument(url, { liveTranslation: readerState.view === "liveTranslation" });
  }
  renderMarkdown();
}

function answerTableHtml(lines) {
  const rows = lines.map(tableCells);
  const header = rows.shift() || [];
  rows.shift();
  return `<div class="answer-table-wrap"><table><thead><tr>${header.map(cell => `<th>${markdownInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${header.map((_, index) => `<td>${markdownInline(row[index] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function answerListHtml(lines) {
  const ordered = /^\s*\d+[.)]\s+/.test(lines[0] || "");
  const pattern = ordered ? /^(\s*)\d+[.)]\s+(.+)$/ : /^(\s*)[-+*]\s+(.+)$/;
  const tag = ordered ? "ol" : "ul";
  const items = lines.map(line => {
    const match = line.match(pattern);
    if (!match) return "";
    const indent = Math.floor(match[1].replace(/\t/g, "  ").length / 2);
    return `<li style="margin-left:${indent * 14}px">${markdownInline(match[2])}</li>`;
  }).join("");
  return `<${tag}>${items}</${tag}>`;
}

function answerMarkdownHtml(markdown) {
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  const isTableDivider = line => {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
  };
  const isBlockStart = (line, next = "") => (
    /^ {0,3}(#{1,6})\s+/.test(line)
    || /^ {0,3}(```|~~~)/.test(line)
    || /^ {0,3}(?:[-*_]\s*){3,}$/.test(line)
    || /^\s*(?:[-+*]|\d+[.)])\s+/.test(line)
    || /^ {0,3}>\s?/.test(line)
    || (line.includes("|") && isTableDivider(next))
  );
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(/^ {0,3}(```|~~~)\s*([A-Za-z0-9_-]*)\s*$/);
    if (fence) {
      const marker = fence[1];
      const language = fence[2];
      const code = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^ {0,3}${marker}`).test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const languageClass = language ? ` class="language-${escapeHtml(language)}"` : "";
      blocks.push(`<pre><code${languageClass}>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const level = Math.min(6, Number(heading[1].length) + 2);
      blocks.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^ {0,3}(?:[-*_]\s*){3,}$/.test(line)) {
      blocks.push("<hr>");
      index += 1;
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const table = [line, lines[index + 1]];
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
        table.push(lines[index]);
        index += 1;
      }
      blocks.push(answerTableHtml(table));
      continue;
    }
    if (/^ {0,3}>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^ {0,3}>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^ {0,3}>\s?/, ""));
        index += 1;
      }
      blocks.push(`<blockquote>${quote.map(markdownInline).join("<br>")}</blockquote>`);
      continue;
    }
    if (/^\s*(?:[-+*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = [];
      const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
      while (index < lines.length && pattern.test(lines[index])) {
        list.push(lines[index]);
        index += 1;
      }
      blocks.push(answerListHtml(list));
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (
      index < lines.length
      && lines[index].trim()
      && !isBlockStart(lines[index], lines[index + 1] || "")
    ) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push(`<p>${paragraph.map(markdownInline).join("<br>")}</p>`);
  }
  return blocks.join("");
}

function noteStorageKey() {
  const identity = readerState.document?.cacheKey || readerState.document?.id;
  return identity ? `${NOTES_STORAGE_PREFIX}.${identity}` : "";
}

function notesToMarkdown(title, notes) {
  const grouped = new Map();
  notes.forEach(note => {
    const section = note.section?.trim() || "未命名章节";
    if (!grouped.has(section)) grouped.set(section, []);
    grouped.get(section).push(note);
  });
  const output = [`# ${title || "阅读笔记"}`, ""];
  grouped.forEach((items, section) => {
    output.push(`## ${section}`, "");
    items.forEach(note => {
      if (note.kind === "answer") {
        output.push(`### 划词问答 · ${note.action || "划词解读"}`, "");
        output.push("#### 划词内容", "");
        if (note.selection) {
          output.push(...String(note.selection).split("\n").map(line => `> ${line}`), "");
        }
        output.push("#### Prompt", "", `- 动作：${note.action || "划词解读"}`);
        if (note.question) output.push(`- 补充问题：${note.question}`);
        output.push("", "#### 返回结果", "");
        output.push(note.text.trim(), "");
      } else {
        output.push(`### ${note.view === "translated" ? "译文摘录" : "原文摘录"}`, "");
        output.push(...String(note.text).split("\n").map(line => `> ${line}`), "");
      }
    });
  });
  return output.join("\n").trim() + "\n";
}

function setNotesStatus(message) {
  const status = $("#notesStatus");
  if (!status) return;
  status.textContent = message;
  clearTimeout(setNotesStatus.timer);
  setNotesStatus.timer = setTimeout(() => { status.textContent = ""; }, 2200);
}

function readerStateTimestamp(value) {
  const timestamp = Number(value) || 0;
  return timestamp > 0 && timestamp < 1e12 ? timestamp * 1000 : timestamp;
}

function readerStatePayload(updatedAt = readerState.notesUpdatedAt || Date.now()) {
  const documentPrefix = `${readerState.document?.id || "document"}:`;
  const liveTranslations = [...readerState.liveTranslations.entries()]
    .filter(([key, record]) => key.startsWith(documentPrefix) && record?.translation)
    .map(([key, record]) => ({
      blockId: key.slice(documentPrefix.length),
      sourceHash: String(record.sourceHash || ""),
      translation: String(record.translation),
      cached: record.cached === true,
      updatedAt: Number(record.updatedAt) || updatedAt,
    }))
    .slice(-200);
  return {
    version: 4,
    updatedAt,
    notes: readerState.notes.slice(-200),
    textFormats: readerState.textFormats.slice(-500),
    liveTranslations,
  };
}

function schedulePersistentReaderState(payload = readerStatePayload()) {
  const documentId = readerState.document?.id;
  if (!documentId) return;
  clearTimeout(schedulePersistentReaderState.timer);
  schedulePersistentReaderState.timer = setTimeout(() => {
    fetch(`/api/reader/documents/${documentId}/state`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => {});
  }, 220);
}

function persistDocumentNotes() {
  const key = noteStorageKey();
  if (!key) return false;
  readerState.notesUpdatedAt = Date.now();
  const payload = readerStatePayload();
  let localSaved = false;
  try {
    localStorage.setItem(key, JSON.stringify(payload));
    localSaved = true;
  } catch {
    setNotesStatus("浏览器存储空间不足；正在尝试保存到本地文档缓存。");
  }
  schedulePersistentReaderState(payload);
  return localSaved;
}

async function loadDocumentNotes() {
  readerState.notes = [];
  readerState.textFormats = [];
  readerState.liveTranslations = new Map();
  readerState.liveTranslationRequests = new Map();
  readerState.notesUpdatedAt = 0;
  const key = noteStorageKey();
  let localState = {};
  if (key) {
    try {
      localState = JSON.parse(localStorage.getItem(key) || "{}");
    } catch {
      localState = {};
    }
  }
  let cachedState = {};
  if (readerState.document?.id) {
    try {
      const response = await fetch(`/api/reader/documents/${readerState.document.id}/state`);
      if (response.ok) cachedState = await response.json();
    } catch {
      cachedState = {};
    }
  }
  const localUpdatedAt = readerStateTimestamp(localState.updatedAt);
  const cachedUpdatedAt = readerStateTimestamp(cachedState.updatedAt);
  const selected = cachedUpdatedAt > localUpdatedAt ? cachedState : localState;
  if (Array.isArray(selected.notes)) readerState.notes = selected.notes.slice(-200);
  if (Array.isArray(selected.textFormats)) readerState.textFormats = selected.textFormats.slice(-500);
  if (Array.isArray(selected.liveTranslations)) {
    selected.liveTranslations.slice(-200).forEach(item => {
      const blockId = String(item?.blockId || "");
      const translation = String(item?.translation || "").trim();
      if (!blockId || !translation) return;
      readerState.liveTranslations.set(liveTranslationKey(blockId), {
        sourceHash: String(item.sourceHash || ""),
        translation,
        cached: item.cached === true,
        updatedAt: Number(item.updatedAt) || 0,
      });
    });
  }
  readerState.notesUpdatedAt = Math.max(localUpdatedAt, cachedUpdatedAt);
  if (key && selected === cachedState) {
    try { localStorage.setItem(key, JSON.stringify(readerStatePayload(readerState.notesUpdatedAt))); } catch {}
  } else if (
    selected === localState
    && (readerState.notes.length || readerState.textFormats.length || readerState.liveTranslations.size)
    && localUpdatedAt >= cachedUpdatedAt
  ) {
    schedulePersistentReaderState(readerStatePayload(localUpdatedAt || Date.now()));
  }
  readerState.lastAnswer = null;
  renderNotes();
  const saveAnswer = $("#saveAnswerNote");
  if (saveAnswer) {
    saveAnswer.disabled = true;
    saveAnswer.textContent = "回答会自动保存为配对笔记";
  }
}

function renderNotes() {
  const target = $("#notesList");
  if (!target) return;
  $("#notesCount").textContent = String(readerState.notes.length);
  if (!readerState.notes.length) {
    target.innerHTML = `<p class="notes-empty">还没有笔记。划词并执行一个 Prompt 后，选区与回答会自动配对保存在这里。</p>`;
    return;
  }
  const grouped = new Map();
  readerState.notes.forEach(note => {
    const section = note.section?.trim() || "未命名章节";
    if (!grouped.has(section)) grouped.set(section, []);
    grouped.get(section).push(note);
  });
  target.innerHTML = [...grouped.entries()].map(([section, notes], groupIndex) => `
    <details class="notes-group" ${groupIndex === 0 ? "open" : ""}>
      <summary>${escapeHtml(section)} <span>${notes.length}</span></summary>
      <div class="notes-group-items">${notes.map(note => `
        <article class="note-item" data-note-id="${escapeHtml(note.id)}" tabindex="-1">
          <div class="note-item-heading">
            <span>${note.kind === "answer" ? `AI · ${escapeHtml(note.action || "解读")}` : note.view === "translated" ? "译文摘录" : "原文摘录"}</span>
            <button class="note-delete-button" type="button" data-delete-note="${escapeHtml(note.id)}" aria-label="删除这条笔记" title="只删除这条笔记">删除</button>
          </div>
          ${note.kind === "answer"
            ? `<details><summary>${escapeHtml((note.selection || "查看配对笔记").slice(0, 70))}</summary>
                <div class="note-pair">
                  <div class="note-pair-section">
                    <span class="note-pair-label">划词内容</span>
                    <blockquote>${escapeHtml(note.selection || "").replace(/\n/g, "<br>")}</blockquote>
                  </div>
                  <div class="note-pair-section">
                    <span class="note-pair-label">PROMPT</span>
                    <p>${escapeHtml(note.action || "划词解读")}</p>
                    ${note.question ? `<p class="note-prompt-question">补充问题：${escapeHtml(note.question)}</p>` : ""}
                  </div>
                  <div class="note-pair-section note-answer">
                    <span class="note-pair-label">返回结果</span>
                    ${answerMarkdownHtml(note.text)}
                  </div>
                </div>
              </details>`
            : `<p>${escapeHtml(note.text).replace(/\n/g, "<br>")}</p>`}
        </article>
      `).join("")}</div>
    </details>
  `).join("");
  target.querySelectorAll("[data-delete-note]").forEach(button => {
    button.addEventListener("click", () => deleteReaderNote(button.dataset.deleteNote));
  });
}

function deleteReaderNote(noteId) {
  const deleted = readerState.notes.find(note => note.id === noteId);
  if (!deleted) return false;
  readerState.notes = readerState.notes.filter(note => note.id !== noteId);
  if (readerState.lastAnswer?.id === noteId) {
    readerState.lastAnswer = null;
    renderAnswer("这条缓存笔记已删除。");
    $("#saveAnswerNote").disabled = true;
    $("#saveAnswerNote").textContent = "回答会自动保存为配对笔记";
  }
  persistDocumentNotes();
  renderNotes();
  renderReaderDecorations($("#readerDocumentFrame")?.contentDocument || document);
  setNotesStatus(`已单独删除“${deleted.action || "阅读笔记"}”。`);
  return true;
}

function notesMatchingAnchor(notes, anchor, view = anchor?.view) {
  if (!anchor) return [];
  return (Array.isArray(notes) ? notes : []).filter(note => (
    note.kind === "answer"
    && note.anchor
    && note.anchor.view === view
    && anchorsOverlap(note.anchor, anchor)
  ));
}

function answerNotesForAnchor(anchor) {
  return notesMatchingAnchor(readerState.notes, anchor, readerState.view);
}

function locateReaderNotes(notes, message = "") {
  const ids = new Set((Array.isArray(notes) ? notes : [notes]).map(note => note?.id).filter(Boolean));
  if (!ids.size) return;
  const list = $("#notesList");
  if (!list) return;
  const panel = $("#notesPanel");
  if (panel) panel.open = true;
  clearTimeout(locateReaderNotes.timer);
  list.querySelectorAll(".note-item.is-located").forEach(item => {
    item.classList.remove("is-located");
    item.removeAttribute("aria-current");
  });
  const located = [...list.querySelectorAll("[data-note-id]")].filter(item => ids.has(item.dataset.noteId));
  if (!located.length) return;
  located.forEach(item => {
    const group = item.closest(".notes-group");
    if (group) group.open = true;
    const details = item.querySelector(":scope > details");
    if (details) details.open = true;
    item.classList.add("is-located");
    item.setAttribute("aria-current", "true");
  });
  requestAnimationFrame(() => {
    located[0].scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  });
  if (message) setNotesStatus(message);
  locateReaderNotes.timer = setTimeout(() => {
    located.forEach(item => {
      item.classList.remove("is-located");
      item.removeAttribute("aria-current");
    });
  }, 3200);
}

function addReaderNote(note) {
  const normalizedText = String(note.text || "").trim();
  if (!normalizedText) return;
  const duplicate = readerState.notes.some(item => (
    item.kind === note.kind
    && item.section === note.section
    && item.text === normalizedText
  ));
  if (duplicate) {
    setNotesStatus("这条内容已经在笔记中。");
    return;
  }
  readerState.notes.push({
    id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    createdAt: Date.now(),
    ...note,
    text: normalizedText.slice(0, 30000),
  });
  readerState.notes = readerState.notes.slice(-200);
  persistDocumentNotes();
  renderNotes();
  setNotesStatus("已按章节加入阅读笔记。");
  return readerState.notes.at(-1);
}

function answerNoteIdentity(note) {
  return JSON.stringify({
    action: note.actionId || note.action || "",
    analysisId: note.analysisId || "",
    question: note.question || "",
    anchor: note.anchor || null,
    selection: note.selection || "",
  });
}

function cacheAnswerNote(note) {
  const normalizedText = String(note.text || "").trim().slice(0, 30000);
  if (!normalizedText) return null;
  const identity = answerNoteIdentity(note);
  let cached = readerState.notes.find(item => item.kind === "answer" && answerNoteIdentity(item) === identity);
  if (cached) {
    Object.assign(cached, note, { text: normalizedText, updatedAt: Date.now() });
  } else {
    cached = {
      id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      createdAt: Date.now(),
      kind: "answer",
      ...note,
      text: normalizedText,
    };
    readerState.notes.push(cached);
    readerState.notes = readerState.notes.slice(-200);
  }
  const persisted = persistDocumentNotes();
  renderNotes();
  renderReaderDecorations($("#readerDocumentFrame")?.contentDocument || document);
  if (persisted) setNotesStatus(`“${cached.action || "划词解读"}”已与选区配对并缓存。`);
  return { note: cached, persisted };
}

function saveCurrentSelectionNote() {
  if (!readerState.selection) return;
  addReaderNote({
    kind: "selection",
    section: readerState.selectionSection || (readerState.selectionPage ? `第 ${readerState.selectionPage} 页` : "未命名章节"),
    view: readerState.view,
    text: readerState.selection,
  });
  hideSelectionMenu();
}

async function copyNotesMarkdown() {
  if (!readerState.notes.length) return setNotesStatus("当前没有可复制的笔记。");
  try {
    await navigator.clipboard.writeText(notesToMarkdown(readerState.document?.title, readerState.notes));
    setNotesStatus("Markdown 笔记已复制。");
  } catch {
    setNotesStatus("浏览器不允许自动复制，请使用导出功能。");
  }
}

function exportNotesMarkdown() {
  if (!readerState.notes.length) return setNotesStatus("当前没有可导出的笔记。");
  const content = notesToMarkdown(readerState.document?.title, readerState.notes);
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${String(readerState.document?.title || "reading-notes").replace(/[\\/:*?"<>|]/g, "_")}_notes.md`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  setNotesStatus("Markdown 笔记已导出。");
}

function renderAnswer(markdown) {
  $("#answerPanel").innerHTML = answerMarkdownHtml(markdown);
  typesetMath();
}

function ensureAnnotationStyles(targetDocument) {
  if (targetDocument === document || targetDocument.getElementById("paper-lens-note-annotations")) return;
  const style = targetDocument.createElement("style");
  style.id = "paper-lens-note-annotations";
  style.textContent = `
    .reader-note-badges { position:absolute; z-index:30; display:flex; flex-wrap:nowrap; justify-content:flex-end; gap:2px; max-width:calc(100% - 8px); transform:translate(-100%,calc(-100% - 1px)); pointer-events:auto; user-select:none; }
    .reader-note-badge { height:var(--reader-note-badge-height,11px); padding:0 max(2px,calc(var(--reader-note-badge-font,8px) * .38)); border:1px solid #74aa9b; border-radius:999px; color:#0d6557; background:#e7f5f0; box-shadow:0 1px 3px rgba(16,65,57,.14); font:800 var(--reader-note-badge-font,8px)/1 Inter,"PingFang SC","Microsoft YaHei",sans-serif; letter-spacing:-.02em; white-space:nowrap; cursor:pointer; }
    .reader-note-badge:hover,.reader-note-badge:focus-visible { border-color:#157262; color:#fff; background:#157262; outline:none; }
    .reader-note-spaced { line-height:var(--reader-note-line-height) !important; }
    .reader-prompt-mark { padding:0 1px; border:0; border-radius:2px; color:inherit; background:transparent; text-decoration-line:underline; text-decoration-style:dashed; text-decoration-color:#3d9788; text-decoration-thickness:1.2px; text-underline-offset:var(--reader-prompt-underline-offset,2px); text-decoration-skip-ink:none; box-decoration-break:clone; -webkit-box-decoration-break:clone; cursor:pointer; }
    .reader-prompt-mark:hover { background:rgba(61,151,136,.11); }
    .reader-prompt-math { text-decoration-line:underline; text-decoration-style:dashed; text-decoration-color:#3d9788; text-decoration-thickness:1.2px; text-underline-offset:var(--reader-prompt-underline-offset,2px); text-decoration-skip-ink:none; }
    .reader-prompt-spaced { line-height:var(--reader-prompt-line-height) !important; }
    .reader-prompt-spaced.reader-note-spaced { line-height:max(var(--reader-prompt-line-height),var(--reader-note-line-height)) !important; }
    .reader-vocabulary-mark { padding:0 1px; border:0; border-radius:2px; color:inherit; background:linear-gradient(transparent 58%,rgba(155,126,220,.25) 58%); text-decoration-line:underline; text-decoration-style:dashed; text-decoration-color:#7c5ac7; text-decoration-thickness:1.2px; text-underline-offset:var(--reader-prompt-underline-offset,2px); text-decoration-skip-ink:none; box-decoration-break:clone; -webkit-box-decoration-break:clone; cursor:help; }
    .reader-vocabulary-mark:hover,.reader-vocabulary-mark:focus { background:rgba(155,126,220,.24); outline:1px solid rgba(124,90,199,.45); outline-offset:1px; }
    .reader-vocabulary-tooltip { position:fixed; z-index:1000; width:min(340px,calc(100vw - 20px)); max-height:min(360px,70vh); padding:12px; overflow:auto; border:1px solid #b9a8df; border-radius:9px; color:#243b38; background:#fff; box-shadow:0 14px 34px rgba(43,32,76,.22); font:12px/1.55 var(--reader-text-font); }
    .reader-vocabulary-tooltip-heading { display:flex; align-items:baseline; justify-content:space-between; gap:10px; padding-bottom:7px; border-bottom:1px solid #e5def3; }
    .reader-vocabulary-tooltip-heading strong { color:#593e9c; font-size:14px; word-break:break-word; }
    .reader-vocabulary-tooltip-heading span,.reader-vocabulary-tooltip > small { color:#817698; font-size:9px; }
    .reader-vocabulary-tooltip-answer { margin:8px 0; } .reader-vocabulary-tooltip-answer p { margin:0 0 7px; } .reader-vocabulary-tooltip-answer p:last-child { margin-bottom:0; }
    .reader-vocabulary-tooltip[hidden] { display:none; }
  `;
  (targetDocument.head || targetDocument.documentElement).append(style);
}

function showCachedAnswer(note, relatedNotes = [note]) {
  readerState.lastAnswer = { ...note };
  renderAnswer(note.text);
  $("#saveAnswerNote").disabled = true;
  $("#saveAnswerNote").textContent = `已从本地笔记读取 · ${note.action || "划词解读"}`;
  $("#readingStatus").textContent = "本地回答";
  setNotesStatus(`已打开“${note.action || "划词解读"}”的缓存结果。`);
  if (window.matchMedia("(max-width: 1180px)").matches) {
    $("#answerPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  locateReaderNotes(relatedNotes, `已定位“${note.action || "划词解读"}”对应的笔记。`);
}

function calculateNoteBadgeMetrics(fontSizeValue, lineHeightValue, textHeightValue, canAdjustLineHeight = true) {
  const fontSize = Number.isFinite(Number(fontSizeValue)) ? Number(fontSizeValue) : 16;
  const lineHeight = Number.isFinite(Number(lineHeightValue)) ? Number(lineHeightValue) : fontSize * 1.5;
  const textHeight = Number.isFinite(Number(textHeightValue)) ? Number(textHeightValue) : fontSize * 1.15;
  const badgeFont = Math.round(Math.min(9, Math.max(7, fontSize * 0.44)) * 10) / 10;
  const badgeHeight = Math.ceil(badgeFont * 1.05 + 2);
  const requiredLineHeight = textHeight + badgeHeight + 3;
  return {
    badgeFont,
    badgeHeight,
    lineHeight: canAdjustLineHeight ? Math.ceil(Math.max(lineHeight, requiredLineHeight)) : lineHeight,
  };
}

function firstVisibleRangeRect(range) {
  return [...(range?.getClientRects?.() || [])].find(rect => rect.width || rect.height)
    || range?.getBoundingClientRect?.()
    || null;
}

function noteBadgeSpacingElement(range, root) {
  const start = range?.startContainer;
  const element = start?.nodeType === 1 ? start : start?.parentElement;
  return element?.closest?.("p,li,blockquote,figcaption,td,th,h1,h2,h3,h4,h5,h6,pre,.math-source")
    || root;
}

function renderNoteAnnotations(targetDocument = document) {
  targetDocument.querySelectorAll(".reader-note-badges").forEach(node => node.remove());
  targetDocument.querySelectorAll(".reader-note-spaced").forEach(node => {
    node.classList.remove("reader-note-spaced");
    node.style.removeProperty("--reader-note-line-height");
  });
  ensureAnnotationStyles(targetDocument);
  const notes = readerState.notes.filter(note => (
    note.kind === "answer"
    && note.anchor
    && note.anchor.view === readerState.view
  ));
  const grouped = new Map();
  notes.forEach(note => {
    const key = JSON.stringify(note.anchor);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(note);
  });
  const entries = [];
  grouped.forEach(group => {
    const anchor = group[0].anchor;
    const root = rootForNoteAnchor(anchor, targetDocument);
    if (!root) return;
    const range = rangeForNoteAnchor(root, anchor);
    const rect = firstVisibleRangeRect(range);
    if (!rect || (!rect.width && !rect.height)) return;
    const textElement = range.startContainer?.nodeType === 1
      ? range.startContainer
      : range.startContainer?.parentElement;
    const computed = targetDocument.defaultView?.getComputedStyle(textElement || root);
    const fontSize = Number.parseFloat(computed?.fontSize) || 16;
    const currentLineHeight = Number.parseFloat(computed?.lineHeight) || fontSize * 1.5;
    const canAdjustLineHeight = !root.closest?.("[data-pdf-page]");
    const metrics = calculateNoteBadgeMetrics(fontSize, currentLineHeight, rect.height, canAdjustLineHeight);
    if (canAdjustLineHeight && metrics.lineHeight > currentLineHeight + 0.5) {
      const spacingElement = noteBadgeSpacingElement(range, root);
      const existing = Number.parseFloat(spacingElement.style.getPropertyValue("--reader-note-line-height")) || 0;
      spacingElement.classList.add("reader-note-spaced");
      spacingElement.style.setProperty("--reader-note-line-height", `${Math.max(existing, metrics.lineHeight)}px`);
    }
    entries.push({ group, root, range, metrics });
  });

  entries.forEach(({ group, range, metrics }) => {
    const rect = firstVisibleRangeRect(range);
    if (!rect || (!rect.width && !rect.height)) return;
    const host = targetDocument === document ? $("#paperContent") : targetDocument.body;
    const hostRect = host.getBoundingClientRect();
    if (targetDocument.defaultView?.getComputedStyle(host).position === "static") host.style.position = "relative";
    const badges = targetDocument.createElement("span");
    badges.className = "reader-note-badges";
    badges.style.setProperty("--reader-note-badge-font", `${metrics.badgeFont}px`);
    badges.style.setProperty("--reader-note-badge-height", `${metrics.badgeHeight}px`);
    badges.style.left = `${Math.min(Math.max(16, rect.right - hostRect.left), Math.max(16, host.scrollWidth - 4))}px`;
    badges.style.top = `${rect.top - hostRect.top + host.scrollTop}px`;
    badges.setAttribute("aria-label", "此选区的缓存解读");
    group.forEach(note => {
      const button = targetDocument.createElement("button");
      button.type = "button";
      button.className = "reader-note-badge";
      button.textContent = note.action || "解读";
      button.title = note.question
        ? `查看已缓存的“${note.action || "划词解读"}”结果；补充问题：${note.question}`
        : `查看已缓存的“${note.action || "划词解读"}”结果`;
      ["mousedown", "mouseup", "touchend"].forEach(type => {
        button.addEventListener(type, event => event.stopPropagation());
      });
      button.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        showCachedAnswer(note);
      });
      badges.append(button);
    });
    host.append(badges);
  });
}

function hideSelectionMenu() {
  $("#selectionMenu").classList.add("hidden");
}

function blockForRange(range, element) {
  const direct = element?.closest?.("[data-block-id]");
  if (direct) return direct;
  return [...$("#paperContent").querySelectorAll("[data-block-id]")].find(node => {
    try { return range.intersectsNode(node); } catch { return false; }
  });
}

function selectedReaderFormula(range, element, block) {
  const directlySelected = element?.closest?.(".math-source, [data-latex]");
  if (directlySelected?.dataset.latex) return directlySelected.dataset.latex;
  if (block?.type === "math") return block.content;
  const formulas = [...$("#paperContent").querySelectorAll(".math-source")].filter(node => range.intersectsNode(node)).map(node => node.dataset.latex).filter(Boolean);
  return formulas.length ? formulas.join("\n") : "";
}

function selectionContextFor(element, sourceDocument) {
  const pdfPage = element?.closest?.("[data-pdf-page]");
  if (pdfPage) return {
    context: (pdfPage.dataset.pageText || pdfPage.innerText).slice(0, 24000),
    section: `第 ${pdfPage.dataset.pdfPage} 页`,
    page: Number(pdfPage.dataset.pdfPage),
  };
  if (sourceDocument !== document) {
    const nodes = htmlSemanticNodes(sourceDocument);
    const semantic = element?.closest?.("h1,h2,h3,h4,h5,h6,p,li,figcaption,blockquote,td,th");
    const index = Math.max(0, nodes.indexOf(semantic));
    const context = nodes.slice(Math.max(0, index - 2), index + 3)
      .map(node => node.textContent.trim())
      .join("\n\n")
      .slice(0, 24000);
    return { context, section: htmlSection(nodes, index), page: null };
  }
  return { context: "", section: "", page: null };
}

function nodeInside(root, node) {
  const element = node?.nodeType === 1 ? node : node?.parentElement;
  return Boolean(root && element && (root === element || root.contains(element)));
}

function rangeOffsetsWithin(root, range) {
  if (!nodeInside(root, range.startContainer) || !nodeInside(root, range.endContainer)) return null;
  const before = range.cloneRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);
  return {
    start: before.toString().length,
    end: before.toString().length + range.toString().length,
  };
}

function selectionAnchorFor(range, element, sourceDocument, block, pdfPage) {
  let root;
  let kind;
  let key = "";
  if (sourceDocument !== document) {
    const semanticNodes = htmlSemanticNodes(sourceDocument);
    const semantic = element?.closest?.("h1,h2,h3,h4,h5,h6,p,li,figcaption,blockquote,td,th");
    root = semantic && nodeInside(semantic, range.startContainer) && nodeInside(semantic, range.endContainer)
      ? semantic
      : sourceDocument.body;
    const semanticIndex = semanticNodes.indexOf(root);
    kind = semanticIndex >= 0 ? "html-semantic" : "html-document";
    key = semanticIndex >= 0 ? String(semanticIndex) : "";
  } else if (block && nodeInside(block, range.startContainer) && nodeInside(block, range.endContainer)) {
    root = block;
    kind = "block";
    key = block.dataset.blockId || "";
  } else if (pdfPage && nodeInside(pdfPage, range.startContainer) && nodeInside(pdfPage, range.endContainer)) {
    root = pdfPage;
    kind = "pdf-page";
    key = pdfPage.dataset.pdfPage || "";
  } else {
    root = $("#paperContent");
    kind = "document";
  }
  const offsets = rangeOffsetsWithin(root, range);
  if (!offsets) return null;
  return {
    version: 1,
    view: readerState.view,
    kind,
    key,
    start: offsets.start,
    end: offsets.end,
    quote: range.toString().trim().slice(0, 12000),
  };
}

function textPointAtOffset(root, requestedOffset) {
  const sourceDocument = root.ownerDocument;
  const showText = sourceDocument.defaultView?.NodeFilter?.SHOW_TEXT || 4;
  const walker = sourceDocument.createTreeWalker(root, showText);
  let remaining = Math.max(0, Number(requestedOffset) || 0);
  let node = walker.nextNode();
  let last = null;
  while (node) {
    last = node;
    const length = node.nodeValue?.length || 0;
    if (remaining <= length) return { node, offset: remaining };
    remaining -= length;
    node = walker.nextNode();
  }
  return last ? { node: last, offset: last.nodeValue?.length || 0 } : null;
}

function rootForNoteAnchor(anchor, targetDocument) {
  if (!anchor || anchor.view !== readerState.view) return null;
  if (targetDocument !== document) {
    if (anchor.kind === "html-document") return targetDocument.body;
    if (anchor.kind === "html-semantic") return htmlSemanticNodes(targetDocument)[Number(anchor.key)] || null;
    return null;
  }
  if (anchor.kind === "block") return [...$("#paperContent").querySelectorAll("[data-block-id]")]
    .find(node => node.dataset.blockId === anchor.key) || null;
  if (anchor.kind === "pdf-page") return [...$("#paperContent").querySelectorAll("[data-pdf-page]")]
    .find(node => node.dataset.pdfPage === anchor.key) || null;
  if (anchor.kind === "document") return $("#paperContent");
  return null;
}

function rangeForNoteAnchor(root, anchor) {
  let start = Number(anchor.start);
  let end = Number(anchor.end);
  const rootText = root.textContent || "";
  const quote = String(anchor.quote || "");
  if (quote && rootText.slice(start, end).trim() !== quote) {
    const located = rootText.indexOf(quote);
    if (located >= 0) {
      start = located;
      end = located + quote.length;
    }
  }
  const startPoint = textPointAtOffset(root, start);
  const endPoint = textPointAtOffset(root, end);
  if (!startPoint || !endPoint) return null;
  const range = root.ownerDocument.createRange();
  range.setStart(startPoint.node, startPoint.offset);
  range.setEnd(endPoint.node, endPoint.offset);
  return range;
}

function formatAnchorIdentity(anchor) {
  if (!anchor) return "";
  return JSON.stringify({
    view: anchor.view,
    kind: anchor.kind,
    key: anchor.key || "",
    start: Number(anchor.start),
    end: Number(anchor.end),
    quote: anchor.quote || "",
  });
}

function currentTextFormat() {
  const identity = formatAnchorIdentity(readerState.selectionAnchor);
  return readerState.textFormats.find(item => formatAnchorIdentity(item.anchor) === identity) || null;
}

function normalizeFormatColor(value) {
  const color = String(value || "").trim().toLowerCase();
  return /^#[0-9a-f]{6}$/.test(color) ? color : "";
}

function isShortTranslationSelection(value) {
  const word = String(value || "").trim();
  if (!word || word.length > 40 || /\s/u.test(word)) return false;
  if (!/[\p{L}\p{M}]/u.test(word)) return false;
  return /^[\p{L}\p{M}\p{N}][\p{L}\p{M}\p{N}'’._-]{0,39}$/u.test(word);
}

function textFormatHasStyle(format) {
  return Boolean(
    normalizeFormatColor(format.highlightColor)
    || normalizeFormatColor(format.textColor)
    || format.bold
    || format.italic
    || format.underline
    || format.strike
  );
}

function setSelectionFormatStatus(message) {
  const status = $("#selectionFormatStatus");
  if (!status) return;
  status.textContent = message;
  clearTimeout(setSelectionFormatStatus.timer);
  setSelectionFormatStatus.timer = setTimeout(() => { status.textContent = ""; }, 1600);
}

function renderSelectionFormatControls() {
  const format = currentTextFormat() || {};
  $("#selectionHighlightColor").value = normalizeFormatColor(format.highlightColor) || "#fff0a8";
  $("#selectionTextColor").value = normalizeFormatColor(format.textColor) || "#b42318";
  $("#selectionFormatStatus").textContent = "";
  $("#selectionMenu").querySelectorAll("[data-format-toggle]").forEach(button => {
    const active = Boolean(format[button.dataset.formatToggle]);
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function clearTextFormatWrappers(targetDocument) {
  targetDocument.querySelectorAll(".reader-note-badges").forEach(node => node.remove());
  const parents = new Set();
  targetDocument.querySelectorAll("mark.reader-text-format").forEach(wrapper => {
    if (wrapper.parentNode) parents.add(wrapper.parentNode);
    wrapper.replaceWith(...wrapper.childNodes);
  });
  parents.forEach(parent => parent.normalize());
}

function textSegmentsForRange(root, range) {
  const sourceDocument = root.ownerDocument;
  const showText = sourceDocument.defaultView?.NodeFilter?.SHOW_TEXT || 4;
  const walker = sourceDocument.createTreeWalker(root, showText);
  const segments = [];
  let node = walker.nextNode();
  while (node) {
    const parent = node.parentElement;
    const skipped = parent?.closest?.("script,style,button,.reader-note-badges,.math-source,mjx-container");
    if (!skipped) {
      try {
        if (range.intersectsNode(node)) {
          const start = node === range.startContainer ? range.startOffset : 0;
          const end = node === range.endContainer ? range.endOffset : (node.nodeValue?.length || 0);
          if (end > start) segments.push({ node, start, end });
        }
      } catch {
        // Ignore a detached text node while a document is being rerendered.
      }
    }
    node = walker.nextNode();
  }
  return segments;
}

function wrapTextSegment(segment, format) {
  const { node, start, end } = segment;
  let selected = node;
  if (end < (selected.nodeValue?.length || 0)) selected.splitText(end);
  if (start > 0) selected = selected.splitText(start);
  const wrapper = selected.ownerDocument.createElement("mark");
  wrapper.className = "reader-text-format";
  wrapper.dataset.textFormatId = format.id;
  wrapper.style.backgroundColor = normalizeFormatColor(format.highlightColor) || "transparent";
  const insidePdfTextLayer = Boolean(selected.parentElement?.closest?.(".textLayer"));
  const needsVisibleGlyphs = format.bold || format.italic || format.underline || format.strike;
  wrapper.style.color = normalizeFormatColor(format.textColor)
    || (insidePdfTextLayer && needsVisibleGlyphs ? "#172326" : "inherit");
  wrapper.style.fontWeight = format.bold ? "800" : "inherit";
  wrapper.style.fontStyle = format.italic ? "italic" : "inherit";
  const decorations = [format.underline ? "underline" : "", format.strike ? "line-through" : ""].filter(Boolean);
  wrapper.style.textDecorationLine = decorations.join(" ") || "none";
  wrapper.style.textDecorationThickness = decorations.length ? "1.5px" : "";
  wrapper.style.textDecorationColor = normalizeFormatColor(format.textColor) || "currentColor";
  wrapper.style.textUnderlineOffset = format.underline ? "1.5px" : "";
  selected.replaceWith(wrapper);
  wrapper.append(selected);
}

function renderTextFormats(targetDocument = document) {
  clearTextFormatWrappers(targetDocument);
  readerState.textFormats
    .filter(format => format.anchor?.view === readerState.view && textFormatHasStyle(format))
    .forEach(format => {
      const root = rootForNoteAnchor(format.anchor, targetDocument);
      if (!root) return;
      const range = rangeForNoteAnchor(root, format.anchor);
      if (!range) return;
      textSegmentsForRange(root, range).reverse().forEach(segment => wrapTextSegment(segment, format));
    });
}

function clearPromptMarks(targetDocument) {
  const parents = new Set();
  targetDocument.querySelectorAll("mark.reader-prompt-mark").forEach(wrapper => {
    if (wrapper.parentNode) parents.add(wrapper.parentNode);
    wrapper.replaceWith(...wrapper.childNodes);
  });
  parents.forEach(parent => parent.normalize());
  targetDocument.querySelectorAll(".reader-prompt-math").forEach(node => {
    node.classList.remove("reader-prompt-math");
    node.style.removeProperty("--reader-prompt-underline-offset");
  });
  targetDocument.querySelectorAll(".reader-prompt-spaced").forEach(node => {
    node.classList.remove("reader-prompt-spaced");
    node.style.removeProperty("--reader-prompt-line-height");
  });
}

function isVocabularyAnswerNote(note) {
  return Boolean(
    note?.kind === "answer"
    && (note.actionId === "translate" || note.action === "翻译选区")
    && isShortTranslationSelection(note.selection)
  );
}

function visiblePromptUnderlineNotes(notes, view) {
  const visible = (Array.isArray(notes) ? notes : []).filter(note => (
    note?.kind === "answer"
    && note.anchor
    && note.anchor.view === view
  ));
  const latestVocabulary = new Map();
  const prompted = [];
  visible.forEach(note => {
    if (!isVocabularyAnswerNote(note)) {
      prompted.push(note);
      return;
    }
    const key = formatAnchorIdentity(note.anchor);
    const current = latestVocabulary.get(key);
    if (!current || (Number(note.updatedAt || note.createdAt) || 0) >= (Number(current.updatedAt || current.createdAt) || 0)) {
      latestVocabulary.set(key, note);
    }
  });
  return [...prompted, ...latestVocabulary.values()];
}

function assignPromptUnderlineLanes(notes) {
  const assignments = new Map();
  const grouped = new Map();
  (Array.isArray(notes) ? notes : []).forEach((note, index) => {
    const anchor = note?.anchor;
    if (!anchor) return;
    const key = JSON.stringify([anchor.view, anchor.kind, anchor.key || ""]);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push({ note, index });
  });
  grouped.forEach(entries => {
    const laneEnds = [];
    entries.sort((left, right) => (
      Number(left.note.anchor.start) - Number(right.note.anchor.start)
      || Number(left.note.anchor.end) - Number(right.note.anchor.end)
      || left.index - right.index
    )).forEach(({ note }) => {
      const start = Number(note.anchor.start) || 0;
      const end = Math.max(start + 1, Number(note.anchor.end) || start + 1);
      let lane = laneEnds.findIndex(laneEnd => laneEnd <= start);
      if (lane < 0) lane = laneEnds.length;
      laneEnds[lane] = end;
      assignments.set(note, lane);
    });
  });
  return assignments;
}

function calculatePromptUnderlineOffset(lane, hasSolidUnderline = false) {
  return (hasSolidUnderline ? 5 : 2) + Math.max(0, Number(lane) || 0) * 3;
}

function promptUnderlineOffset(note, lane) {
  const hasSolidUnderline = readerState.textFormats.some(format => (
    format.underline
    && format.anchor
    && anchorsOverlap(format.anchor, note.anchor)
  ));
  return calculatePromptUnderlineOffset(lane, hasSolidUnderline);
}

function ensurePromptUnderlineSpacing(range, root, targetDocument, underlineOffset) {
  if (root.closest?.("[data-pdf-page]")) return;
  const rect = firstVisibleRangeRect(range);
  if (!rect) return;
  const start = range.startContainer;
  const textElement = start?.nodeType === 1 ? start : start?.parentElement;
  const computed = targetDocument.defaultView?.getComputedStyle(textElement || root);
  const fontSize = Number.parseFloat(computed?.fontSize) || 16;
  const lineHeight = Number.parseFloat(computed?.lineHeight) || fontSize * 1.5;
  const required = Math.ceil(rect.height + underlineOffset + 3);
  if (required <= lineHeight + 0.5) return;
  const spacingElement = noteBadgeSpacingElement(range, root);
  const existing = Number.parseFloat(spacingElement.style.getPropertyValue("--reader-prompt-line-height")) || 0;
  spacingElement.classList.add("reader-prompt-spaced");
  spacingElement.style.setProperty("--reader-prompt-line-height", `${Math.max(existing, required)}px`);
}

function wrapPromptSegment(segment, note, underlineOffset) {
  const { node, start, end } = segment;
  let selected = node;
  if (end < (selected.nodeValue?.length || 0)) selected.splitText(end);
  if (start > 0) selected = selected.splitText(start);
  const wrapper = selected.ownerDocument.createElement("mark");
  wrapper.className = "reader-prompt-mark";
  wrapper.dataset.promptNoteId = note.id;
  wrapper.style.setProperty("--reader-prompt-underline-offset", `${underlineOffset}px`);
  wrapper.title = `已执行 Prompt：${note.action || "划词解读"}；单击定位对应笔记`;
  wrapper.addEventListener("click", event => {
    if (!wrapper.ownerDocument.getSelection()?.isCollapsed) return;
    event.preventDefault();
    event.stopPropagation();
    showCachedAnswer(note, answerNotesForAnchor(note.anchor));
  });
  selected.replaceWith(wrapper);
  wrapper.append(selected);
}

function renderPromptMarks(targetDocument = document) {
  clearPromptMarks(targetDocument);
  const visibleNotes = visiblePromptUnderlineNotes(readerState.notes, readerState.view);
  readerState.promptUnderlineLanes = assignPromptUnderlineLanes(visibleNotes);
  visibleNotes.forEach(note => {
    const root = rootForNoteAnchor(note.anchor, targetDocument);
    if (!root) return;
    const range = rangeForNoteAnchor(root, note.anchor);
    if (!range) return;
    const lane = readerState.promptUnderlineLanes.get(note) || 0;
    const underlineOffset = promptUnderlineOffset(note, lane);
    ensurePromptUnderlineSpacing(range, root, targetDocument, underlineOffset);
    if (isVocabularyAnswerNote(note)) return;
    const segments = textSegmentsForRange(root, range);
    if (segments.length) {
      segments.reverse().forEach(segment => wrapPromptSegment(segment, note, underlineOffset));
      return;
    }
    const mathTarget = root.matches?.(".math-source,mjx-container")
      ? root
      : root.querySelector?.(".math-source,mjx-container");
    if (mathTarget) {
      mathTarget.classList.add("reader-prompt-math");
      mathTarget.style.setProperty("--reader-prompt-underline-offset", `${underlineOffset}px`);
      mathTarget.title = `已执行 Prompt：${note.action || "划词解读"}`;
    }
  });
}

function clearVocabularyMarks(targetDocument) {
  const parents = new Set();
  targetDocument.querySelectorAll("mark.reader-vocabulary-mark").forEach(wrapper => {
    if (wrapper.parentNode) parents.add(wrapper.parentNode);
    wrapper.replaceWith(...wrapper.childNodes);
  });
  parents.forEach(parent => parent.normalize());
  targetDocument.getElementById("readerVocabularyTooltip")?.remove();
}

function vocabularyTooltip(targetDocument) {
  let tooltip = targetDocument.getElementById("readerVocabularyTooltip");
  if (tooltip) return tooltip;
  tooltip = targetDocument.createElement("aside");
  tooltip.id = "readerVocabularyTooltip";
  tooltip.className = "reader-vocabulary-tooltip";
  tooltip.hidden = true;
  tooltip.setAttribute("role", "tooltip");
  tooltip.addEventListener("mouseenter", () => clearTimeout(tooltip.hideTimer));
  tooltip.addEventListener("mouseleave", () => {
    tooltip.hideTimer = setTimeout(() => { tooltip.hidden = true; }, 120);
  });
  targetDocument.documentElement.append(tooltip);
  return tooltip;
}

function showVocabularyTooltip(mark, note) {
  const targetDocument = mark.ownerDocument;
  const tooltip = vocabularyTooltip(targetDocument);
  clearTimeout(tooltip.hideTimer);
  tooltip.innerHTML = `
    <div class="reader-vocabulary-tooltip-heading">
      <strong>${escapeHtml(note.selection)}</strong>
      <span>已缓存翻译</span>
    </div>
    <div class="reader-vocabulary-tooltip-answer">${answerMarkdownHtml(note.text)}</div>
    <small>点击单词可在右侧打开完整结果</small>
  `;
  tooltip.hidden = false;
  const rect = mark.getBoundingClientRect();
  const view = targetDocument.defaultView;
  const tooltipRect = tooltip.getBoundingClientRect();
  const left = Math.min(
    Math.max(10, rect.left + rect.width / 2 - tooltipRect.width / 2),
    Math.max(10, view.innerWidth - tooltipRect.width - 10),
  );
  const top = rect.top - tooltipRect.height - 9 >= 8
    ? rect.top - tooltipRect.height - 9
    : Math.min(view.innerHeight - tooltipRect.height - 8, rect.bottom + 9);
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(8, top)}px`;
}

function hideVocabularyTooltip(mark) {
  const tooltip = mark.ownerDocument.getElementById("readerVocabularyTooltip");
  if (!tooltip) return;
  clearTimeout(tooltip.hideTimer);
  tooltip.hideTimer = setTimeout(() => { tooltip.hidden = true; }, 140);
}

function wrapVocabularySegment(segment, note) {
  const { node, start, end } = segment;
  let selected = node;
  if (end < (selected.nodeValue?.length || 0)) selected.splitText(end);
  if (start > 0) selected = selected.splitText(start);
  const wrapper = selected.ownerDocument.createElement("mark");
  wrapper.className = "reader-vocabulary-mark";
  wrapper.dataset.vocabularyNoteId = note.id;
  const lane = readerState.promptUnderlineLanes.get(note) || 0;
  wrapper.style.setProperty("--reader-prompt-underline-offset", `${promptUnderlineOffset(note, lane)}px`);
  wrapper.tabIndex = 0;
  wrapper.setAttribute("aria-label", `${note.selection}，已有缓存翻译`);
  wrapper.addEventListener("mouseenter", () => showVocabularyTooltip(wrapper, note));
  wrapper.addEventListener("mouseleave", () => hideVocabularyTooltip(wrapper));
  wrapper.addEventListener("focus", () => showVocabularyTooltip(wrapper, note));
  wrapper.addEventListener("blur", () => hideVocabularyTooltip(wrapper));
  ["mousedown", "mouseup", "touchend"].forEach(type => {
    wrapper.addEventListener(type, event => event.stopPropagation());
  });
  wrapper.addEventListener("click", event => {
    event.preventDefault();
    event.stopPropagation();
    showCachedAnswer(note);
  });
  selected.replaceWith(wrapper);
  wrapper.append(selected);
}

function renderVocabularyMarks(targetDocument = document) {
  clearVocabularyMarks(targetDocument);
  const translations = new Map();
  readerState.notes.forEach(note => {
    const isTranslation = note.actionId === "translate" || note.action === "翻译选区";
    if (
      note.kind !== "answer"
      || !isTranslation
      || !note.anchor
      || note.anchor.view !== readerState.view
      || !isShortTranslationSelection(note.selection)
    ) return;
    const key = formatAnchorIdentity(note.anchor);
    const current = translations.get(key);
    if (!current || Number(note.updatedAt || note.createdAt) >= Number(current.updatedAt || current.createdAt)) {
      translations.set(key, note);
    }
  });
  translations.forEach(note => {
    const root = rootForNoteAnchor(note.anchor, targetDocument);
    if (!root) return;
    const range = rangeForNoteAnchor(root, note.anchor);
    if (!range) return;
    textSegmentsForRange(root, range).reverse().forEach(segment => wrapVocabularySegment(segment, note));
  });
}

function renderReaderDecorations(targetDocument = document) {
  targetDocument.querySelectorAll(".reader-prompt-spaced").forEach(node => {
    node.classList.remove("reader-prompt-spaced");
    node.style.removeProperty("--reader-prompt-line-height");
  });
  targetDocument.querySelectorAll(".reader-note-spaced").forEach(node => {
    node.classList.remove("reader-note-spaced");
    node.style.removeProperty("--reader-note-line-height");
  });
  renderTextFormats(targetDocument);
  renderPromptMarks(targetDocument);
  renderVocabularyMarks(targetDocument);
  renderNoteAnnotations(targetDocument);
}

function currentContentDocument() {
  return readerState.selectionAnchor?.kind?.startsWith("html-")
    ? ($("#readerDocumentFrame")?.contentDocument || document)
    : document;
}

function updateCurrentTextFormat(patch, message) {
  const anchor = readerState.selectionAnchor;
  if (!anchor) return setSelectionFormatStatus("请先划定一段文字。");
  const identity = formatAnchorIdentity(anchor);
  let format = readerState.textFormats.find(item => formatAnchorIdentity(item.anchor) === identity);
  if (!format) {
    format = {
      id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      anchor: { ...anchor },
      createdAt: Date.now(),
    };
    readerState.textFormats.push(format);
  }
  Object.assign(format, patch, { updatedAt: Date.now() });
  if (!textFormatHasStyle(format)) {
    readerState.textFormats = readerState.textFormats.filter(item => item !== format);
  }
  readerState.textFormats = readerState.textFormats.slice(-500);
  const persisted = persistDocumentNotes();
  const targetDocument = currentContentDocument();
  renderReaderDecorations(targetDocument);
  renderSelectionFormatControls();
  setSelectionFormatStatus(persisted ? message : "格式已应用，但浏览器未能持久保存。");
}

function anchorsOverlap(left, right) {
  return Boolean(
    left
    && right
    && left.view === right.view
    && left.kind === right.kind
    && (left.key || "") === (right.key || "")
    && Number(left.start) < Number(right.end)
    && Number(right.start) < Number(left.end)
  );
}

function clearCurrentTextFormats() {
  const anchor = readerState.selectionAnchor;
  if (!anchor) return setSelectionFormatStatus("请先划定一段文字。");
  const before = readerState.textFormats.length;
  readerState.textFormats = readerState.textFormats.filter(format => !anchorsOverlap(format.anchor, anchor));
  const persisted = persistDocumentNotes();
  const targetDocument = currentContentDocument();
  renderReaderDecorations(targetDocument);
  renderSelectionFormatControls();
  const changed = before !== readerState.textFormats.length;
  setSelectionFormatStatus(
    persisted
      ? (changed ? "已清除与这个选区重叠的格式。" : "这个选区没有已保存的格式。")
      : "格式已清除，但浏览器未能持久保存。",
  );
}

function captureSelection(sourceDocument = document, frame = null) {
  const selection = sourceDocument.getSelection();
  let text = selection?.toString().trim() || "";
  if (!selection?.rangeCount) return hideSelectionMenu();
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  const block = sourceDocument === document ? blockForRange(range, element) : null;
  const pdfPage = element?.closest?.("[data-pdf-page]");
  if (sourceDocument === document && !block && !pdfPage) return hideSelectionMenu();
  const sourceBlock = block ? activeBlocks().find(item => item.id === block.dataset.blockId) : null;
  const latex = sourceDocument === document ? selectedReaderFormula(range, element, sourceBlock) : "";
  if (!text && !latex) return hideSelectionMenu();
  if (sourceBlock?.type === "math" || element?.closest?.(".math-source")) text = latex;
  else if (latex && !text.includes(latex)) text = `${text}\n\n[选区包含的原始 LaTeX]\n${latex}`;
  if (text.length > 12000) {
    renderAnswer("选区超过 12,000 个字符，请缩小选择范围。");
    return hideSelectionMenu();
  }
  const context = selectionContextFor(element, sourceDocument);
  readerState.selection = text;
  readerState.blockId = block?.dataset.blockId || "";
  readerState.selectionContext = context.context;
  readerState.selectionSection = context.section;
  readerState.selectionPage = context.page;
  readerState.selectionAnchor = selectionAnchorFor(range, element, sourceDocument, block, pdfPage);
  const rect = range.getBoundingClientRect();
  const frameRect = frame?.getBoundingClientRect();
  const menu = $("#selectionMenu");
  $("#selectionPreview").textContent = text.length > 100 ? `${text.slice(0, 100)}…` : text;
  renderSelectionFormatControls();
  const left = rect.left + (frameRect?.left || 0);
  const bottom = rect.bottom + (frameRect?.top || 0);
  menu.classList.remove("hidden");
  const menuWidth = menu.offsetWidth || 360;
  const menuHeight = menu.offsetHeight || 280;
  const top = bottom + menuHeight + 12 <= window.innerHeight
    ? bottom + 8
    : rect.top + (frameRect?.top || 0) - menuHeight - 8;
  menu.style.left = `${Math.min(window.innerWidth - menuWidth - 12, Math.max(12, left))}px`;
  menu.style.top = `${Math.min(window.innerHeight - menuHeight - 12, Math.max(12, top))}px`;
  const cachedNotes = answerNotesForAnchor(readerState.selectionAnchor);
  if (cachedNotes.length) {
    locateReaderNotes(
      cachedNotes,
      cachedNotes.length > 1
        ? `这个选区有 ${cachedNotes.length} 条缓存笔记，已在右侧标出。`
        : "已在右侧定位这个选区的缓存笔记。",
    );
  }
  if (readerState.autoSelectionAction && readerState.defaultSelectionAction) {
    setTimeout(() => ask(readerState.defaultSelectionAction, { keepMenu: true }), 0);
  }
}

async function ask(action, { keepMenu = false } = {}) {
  if (!readerState.selection) return;
  if (readerState.assistantOverlay) {
    readerState.assistantOverlayOpen = true;
    readerState.tableOfContentsOverlayOpen = false;
    applyResponsiveReaderLayout();
  }
  let llm;
  try {
    llm = llmForAction(action);
  } catch (error) {
    renderAnswer(error.message);
    toggleSelectionSettings(true);
    return;
  }
  const requestId = ++readerState.questionRequestId;
  const customQuestion = $("#customQuestion").value.trim();
  const answerContext = {
    actionId: action,
    action: actionLabel(action),
    selection: readerState.selection,
    section: readerState.selectionSection || (readerState.selectionPage ? `第 ${readerState.selectionPage} 页` : "未命名章节"),
    view: readerState.view,
    question: customQuestion,
    anchor: readerState.selectionAnchor ? { ...readerState.selectionAnchor } : null,
  };
  readerState.questionAbortController?.abort();
  const controller = new AbortController();
  readerState.questionAbortController = controller;
  readerState.asking = true;
  readerState.lastAnswer = null;
  $("#saveAnswerNote").disabled = true;
  $("#saveAnswerNote").textContent = "正在生成配对笔记…";
  if (!keepMenu) hideSelectionMenu();
  renderAnswer(`正在使用${mappedPreset(action)?.name || "兜底 LLM"}执行“${actionLabel(action)}”…`);
  try {
    const response = await fetch(`/api/reader/documents/${readerState.document.id}/questions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        selection: readerState.selection,
        blockId: readerState.blockId,
        context: readerState.selectionContext,
        section: readerState.selectionSection,
        pageNumber: readerState.selectionPage,
        action,
        question: customQuestion,
        llm,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "解读请求失败。");
    if (requestId === readerState.questionRequestId) {
      const cached = cacheAnswerNote({ ...answerContext, text: data.answer });
      readerState.lastAnswer = cached?.note || { ...answerContext, text: data.answer };
      $("#saveAnswerNote").disabled = true;
      $("#saveAnswerNote").textContent = cached?.persisted ? "已自动配对并保存到本地" : "已配对，但浏览器未能持久保存";
      renderAnswer(data.answer);
    }
  } catch (error) {
    if (error.name !== "AbortError" && requestId === readerState.questionRequestId) {
      $("#saveAnswerNote").textContent = "本次请求未生成笔记";
      renderAnswer(`请求失败：${error.message}`);
    }
  } finally {
    if (requestId === readerState.questionRequestId) {
      readerState.asking = false;
      readerState.questionAbortController = null;
    }
  }
}

function renderActions() {
  $("#actionButtons").innerHTML = (readerState.config?.actions || []).map(action => {
    const model = mappedPreset(action);
    const modelName = model?.name || "兜底 LLM";
    return `<button data-action="${action.id}"><span>${escapeHtml(action.label)}</span><small>${escapeHtml(modelName)}</small></button>`;
  }).join("");
  $("#actionButtons").querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => ask(button.dataset.action)));
}

async function loadContent() {
  const response = await fetch(`/api/reader/documents/${readerState.document.id}/content`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取论文。");
  readerState.content = data;
  readerState.view = "original";
  readerState.pdfDocument = null;
  $("#documentTitle").textContent = data.title;
  $("#paperDocumentTitle").textContent = data.title;
  const hasTranslation = Boolean(data.translatedBlocks || data.translatedDocumentUrl);
  const capabilities = data.capabilities || {};
  const canLiveTranslate = "liveParagraphTranslation" in capabilities
    ? capabilities.liveParagraphTranslation === true
    : ["markdown", "html"].includes(data.renderKind || "markdown");
  $("#viewSwitch").hidden = !hasTranslation && !canLiveTranslate;
  const translatedViewButton = $("#viewSwitch").querySelector('[data-view="translated"]');
  if (translatedViewButton) translatedViewButton.hidden = !hasTranslation;
  if ($("#liveTranslationViewButton")) {
    $("#liveTranslationViewButton").hidden = !canLiveTranslate;
  }
  if ($("#interleavedViewButton")) {
    $("#interleavedViewButton").hidden = capabilities.interleavedView === false
      || (!("interleavedView" in capabilities) && (!hasTranslation || data.renderKind === "pdf"));
  }
  if ($("#sideBySideViewButton")) {
    $("#sideBySideViewButton").hidden = capabilities.sideBySideView === false
      || (!("sideBySideView" in capabilities) && (!hasTranslation || data.renderKind === "pdf"));
  }
  $("#viewSwitch").querySelectorAll("button").forEach(button => button.classList.toggle("active", button.dataset.view === "original"));
  $("#readingStatus").textContent = "已就绪";
  await renderPaper();
  updateSpeechControl();
}

function stopPolling() {
  if (readerState.poller) {
    clearInterval(readerState.poller);
    readerState.poller = null;
  }
}

function alignmentProgressView(data = {}) {
  const progress = data.alignmentProgress;
  if (!progress || typeof progress !== "object") return { visible: false };
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  const completed = Math.max(0, Number(progress.completed) || 0);
  const total = Math.max(0, Number(progress.total) || 0);
  return {
    visible: true,
    percent,
    label: String(progress.label || data.message || "正在执行 LLM 对齐。"),
    count: total > 0 ? `当前阶段 ${Math.min(completed, total)}/${total}` : "正在等待对齐任务开始",
    failed: progress.stage === "failed",
  };
}

function renderAlignmentProgress(data = {}) {
  const panel = $("#alignmentProgress");
  if (!panel) return;
  const view = alignmentProgressView(data);
  panel.classList.toggle("hidden", !view.visible);
  if (!view.visible) return;
  $("#alignmentProgressLabel").textContent = view.label;
  $("#alignmentProgressPercent").textContent = `${Math.round(view.percent)}%`;
  $("#alignmentProgressCount").textContent = view.count;
  const bar = $("#alignmentProgressBar");
  bar.setAttribute("aria-valuenow", String(Math.round(view.percent)));
  bar.setAttribute("aria-valuetext", `${view.label} ${Math.round(view.percent)}%`);
  bar.querySelector("span").style.width = `${view.percent}%`;
  panel.classList.toggle("failed", view.failed);
}

const THREE_PASS_PHASES = ["pass1", "pass2", "pass3", "synthesis"];
const THREE_PASS_ACTIVE = new Set(["queued", ...THREE_PASS_PHASES, "responding"]);

function selectedThreePassModel() {
  return (readerState.codexStatus?.models || []).find(model => model.id === $("#threePassModel")?.value);
}

function selectedThreePassBackend() {
  return $("#threePassBackend")?.value || "codex";
}

function threePassBackendUsable() {
  if (!readerState.document) return false;
  if (selectedThreePassBackend() === "api") {
    return Boolean($("#threePassApiPreset")?.value && $("#threePassBillingConfirmed")?.checked);
  }
  return Boolean(readerState.codexStatus?.chatgptAuthenticated && readerState.codexStatus?.models?.length);
}

function updateThreePassStartAvailability() {
  const active = Boolean(readerState.threePassAnalysis && (THREE_PASS_ACTIVE.has(readerState.threePassAnalysis.status) || readerState.threePassAnalysis.busy));
  $("#startThreePass").disabled = active || !threePassBackendUsable();
}

function threePassEffortView(model, preferred = "high") {
  const efforts = model?.supportedEfforts?.length
    ? [...model.supportedEfforts]
    : [model?.defaultEffort || preferred || "high"];
  return {
    efforts,
    selected: efforts.includes(preferred) ? preferred : (model?.defaultEffort || efforts[0]),
  };
}

function threePassEffortLabel(model, effort) {
  const modelId = model?.id || "";
  const supportsInstant = modelId === "gpt-5.6" || modelId.startsWith("gpt-5.6-") || ["gpt-6-sol", "gpt-6-luna"].includes(modelId);
  return effort === "none" && supportsInstant
    ? "Instant（none）"
    : effort;
}

function codexRuntimeText(runtime) {
  if (!runtime?.version) return "";
  const label = runtime.managed ? "项目内 Codex " + runtime.version : "Codex " + runtime.version;
  return runtime.restartRequired && runtime.availableUpdateVersion
    ? label + " · " + runtime.availableUpdateVersion + " 空闲时刷新后启用"
    : label;
}

function renderThreePassEfforts(preferred = "high") {
  const select = $("#threePassEffort");
  const model = selectedThreePassModel();
  const { efforts, selected } = threePassEffortView(model, preferred);
  select.innerHTML = efforts.map(effort => `<option value="${escapeHtml(effort)}">${escapeHtml(threePassEffortLabel(model, effort))}</option>`).join("");
  select.value = selected;
}

function compactRateLimitText(rateLimits) {
  if (!rateLimits || typeof rateLimits !== "object") return "";
  const snapshots = Array.isArray(rateLimits.rateLimits)
    ? rateLimits.rateLimits
    : rateLimits.rateLimitsByLimitId && typeof rateLimits.rateLimitsByLimitId === "object"
      ? Object.values(rateLimits.rateLimitsByLimitId)
      : rateLimits.rateLimit ? [rateLimits.rateLimit] : [];
  const first = snapshots[0];
  if (!first || typeof first !== "object") return "";
  const windows = [first.primary, first.secondary].filter(Boolean);
  if (!windows.length) return "";
  return windows.map(window => `${Math.max(0, 100 - (Number(window.usedPercent) || 0))}% 剩余`).join(" · ");
}

async function loadCodexStatus(forceRefresh = false) {
  const statusNode = $("#codexStatus");
  statusNode.textContent = "正在检查本机 Codex…";
  try {
    const response = await fetch(forceRefresh ? "/api/codex/status/refresh" : "/api/codex/status", forceRefresh ? { method: "POST" } : undefined);
    const data = await response.json();
    readerState.codexStatus = data;
    const usable = response.ok && data.chatgptAuthenticated && (data.models || []).length;
    updateThreePassStartAvailability();
    if (!usable) {
      statusNode.textContent = data.error || "Codex 尚未通过 ChatGPT 登录。请运行 codex login。";
      $("#threePassModel").innerHTML = "<option value=\"\">不可用</option>";
      renderThreePassEfforts();
      return;
    }
    $("#threePassModel").innerHTML = data.models.map(model => (
      `<option value="${escapeHtml(model.id)}">${escapeHtml(model.displayName || model.id)}</option>`
    )).join("");
    $("#threePassModel").value = data.defaultModel || data.models[0].id;
    renderThreePassEfforts(data.defaultReasoningEffort || "high");
    const quota = compactRateLimitText(data.rateLimits);
    const runtime = codexRuntimeText(data.runtime);
    statusNode.textContent = `ChatGPT ${data.planType || "账户"} 已连接${runtime ? ` · ${runtime}` : ""}${quota ? ` · ${quota}` : ""}`;
  } catch (error) {
    readerState.codexStatus = null;
    updateThreePassStartAvailability();
    statusNode.textContent = `Codex 不可用：${error.message}`;
  }
}

function renderThreePassApiPresets() {
  const presets = readerState.analysisProviders?.api?.presets || [];
  const select = $("#threePassApiPreset");
  const previous = select.value;
  select.innerHTML = presets.length
    ? presets.map(preset => `<option value="${escapeHtml(preset.id)}">${escapeHtml(preset.name)} · ${escapeHtml(preset.model)} · ${escapeHtml(preset.resolvedProtocol)}</option>`).join("")
    : "<option value=\"\">尚无 API 预设</option>";
  if (presets.some(item => item.id === previous)) select.value = previous;
  const selected = presets.find(item => item.id === select.value);
  $("#threePassApiStatus").textContent = selected
    ? `${selected.resolvedProtocol === "responses" ? "Responses API" : "Chat Completions"} · 并发 ${selected.concurrency || 1}${selected.contextWindow ? ` · 上下文 ${selected.contextWindow}` : " · 未声明上下文，使用保守分块预算"}`
    : "请先在全局设置中添加 API 预设。";
  $("#threePassApiModel").placeholder = selected ? `留空使用 ${selected.model}` : "留空使用预设模型";
  if (selectedThreePassBackend() === "api") loadThreePassApiModels().catch(() => {});
  updateThreePassStartAvailability();
}

async function loadThreePassApiModels() {
  const presetId = $("#threePassApiPreset").value;
  const requestId = ++readerState.apiModelRequestId;
  const list = $("#threePassApiModels");
  const selected = (readerState.analysisProviders?.api?.presets || []).find(item => item.id === presetId);
  list.innerHTML = selected ? `<option value="${escapeHtml(selected.model)}"></option>` : "";
  if (!presetId) return;
  try {
    const response = await fetch(`/api/llm-presets/${encodeURIComponent(presetId)}/models`);
    const data = await response.json();
    if (requestId !== readerState.apiModelRequestId) return;
    if (!response.ok) throw new Error(data.error || "模型列表不可用");
    list.innerHTML = (data.models || []).map(model => `<option value="${escapeHtml(model)}"></option>`).join("");
  } catch (error) {
    if (requestId === readerState.apiModelRequestId && selected) {
      $("#threePassApiStatus").textContent += ` · 动态模型列表不可用，仍可手动输入（${error.message}）`;
    }
  }
}

function renderThreePassBackend() {
  const api = selectedThreePassBackend() === "api";
  $("#threePassCodexOptions").classList.toggle("hidden", api);
  $("#threePassApiOptions").classList.toggle("hidden", !api);
  $("#threePassMessage").textContent = api
    ? "API 分析将使用本地预设并产生独立费用。"
      : "Codex 分析使用当前 ChatGPT/Codex 套餐额度。";
  if (api) loadThreePassApiModels().catch(() => {});
  updateThreePassStartAvailability();
}

function threePassLengthSummary(config, language = "zh-CN") {
  if (!config?.phases) return "";
  const names = { pass1:"P1", pass2:"P2", pass3:"P3", synthesis:"综合" };
  const unit = language === "en-US" ? "words" : "字";
  const summary = Object.entries(names).map(([phase, name]) => `${name} ${config.phases[phase]?.target || "—"}`).join(" · ");
  return `全局目标：${summary} ${unit}（约 ±${config.tolerancePercent || 20}%）`;
}

function renderThreePassLengthSummary() {
  const config = readerState.analysisProviders?.threePass;
  const node = $("#threePassLengthSummary");
  if (!node || !config?.phases) return;
  const summary = threePassLengthSummary(config, $("#threePassLanguage")?.value);
  node.innerHTML = `${escapeHtml(summary)} · <a href="/#settings/three-pass">调整篇幅 →</a>`;
}

async function loadAnalysisProviders() {
  try {
    const response = await fetch("/api/reader/analysis-providers");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取 Three-Pass 后端。 ");
    readerState.analysisProviders = data;
    readerState.codexStatus = data.codex;
    renderThreePassApiPresets();
    renderThreePassLengthSummary();
  } catch (error) {
    readerState.analysisProviders = { api: { presets: readerState.config?.llmPresets || [] } };
    renderThreePassApiPresets();
    $("#threePassMessage").textContent = error.message;
  }
}

async function startCodexLogin(flow = "browser") {
  const loginWindow = flow === "browser" ? window.open("about:blank", "paper-lens-codex-login") : null;
  if (loginWindow) loginWindow.opener = null;
  const response = await fetch("/api/codex/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ flow }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法启动 Codex 登录。 ");
  const details = $("#codexLoginDetails");
  details.classList.remove("hidden");
  if (flow === "browser") {
    details.textContent = "已打开 ChatGPT 登录页面；完成后此处会自动刷新。";
    if (data.authUrl && loginWindow) loginWindow.location.href = data.authUrl;
    else if (data.authUrl) window.open(data.authUrl, "_blank", "noopener");
  } else {
    details.innerHTML = `打开 <a href="${escapeHtml(data.verificationUrl || "https://auth.openai.com/codex/device")}" target="_blank" rel="noopener">设备登录页面</a>，输入代码：<strong>${escapeHtml(data.userCode || "")}</strong>`;
  }
  clearInterval(readerState.codexLoginPoller);
  readerState.codexLoginPoller = setInterval(async () => {
    try {
      const progressResponse = await fetch(`/api/codex/login/${encodeURIComponent(data.loginId)}`);
      const progress = await progressResponse.json();
      if (progress.status === "pending") return;
      clearInterval(readerState.codexLoginPoller);
      readerState.codexLoginPoller = null;
      if (!progress.success) throw new Error(progress.error || "Codex 登录失败。 ");
      details.textContent = "登录成功。";
      await loadCodexStatus();
      await loadAnalysisProviders();
    } catch (error) {
      clearInterval(readerState.codexLoginPoller);
      readerState.codexLoginPoller = null;
      details.textContent = error.message;
    }
  }, 1500);
}

async function logoutCodex() {
  const response = await fetch("/api/codex/logout", { method: "POST" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Codex 登出失败。 ");
  await loadCodexStatus();
  await loadAnalysisProviders();
}

function threePassProgressState(analysis, phase) {
  if (!analysis) return "waiting";
  const effectivePhase = analysis.terminalPhase || analysis.phase;
  const current = THREE_PASS_PHASES.indexOf(effectivePhase);
  const target = THREE_PASS_PHASES.indexOf(phase);
  if (analysis.status === "ready" || current > target || analysis.results?.[phase]) return "done";
  if (["failed", "cancelled"].includes(analysis.status) && current === target) return analysis.status;
  if (analysis.phase === phase) return "running";
  return "waiting";
}

function threePassAnswerMarkdown(analysis) {
  if (!analysis) return "";
  const labels = {
    pass1: "Pass 1 · 快速定位",
    pass2: "Pass 2 · 结构与证据",
    pass3: "Pass 3 · 深读与批判",
  };
  const sections = ["pass1", "pass2", "pass3"].flatMap(phase => {
    const completed = String(analysis.results?.[phase] || "").trim();
    const streaming = analysis.phase === phase ? String(analysis.preview || "").trim() : "";
    const content = completed || streaming;
    return content ? [`## ${labels[phase]}\n\n${content}`] : [];
  });
  if (!sections.length) return "";
  return `# Three-Pass 结构化解读\n\n${sections.join("\n\n")}`;
}

function publishThreePassAnswer(analysis) {
  const markdown = threePassAnswerMarkdown(analysis);
  if (!markdown) return;
  renderAnswer(markdown);
  const complete = analysis?.status === "ready"
    && ["pass1", "pass2", "pass3"].every(phase => String(analysis.results?.[phase] || "").trim());
  if (!complete) return;
  const existing = readerState.notes.find(note => note.kind === "answer" && note.analysisId === analysis.id);
  if (existing?.text === markdown) return;
  const cached = cacheAnswerNote({
    analysisId: analysis.id,
    actionId: "three_pass_document",
    action: "整篇 Three-Pass",
    selection: readerState.document?.title || "当前论文",
    section: "全文分析",
    view: "original",
    question: "",
    anchor: null,
    text: markdown,
  });
  readerState.lastAnswer = cached?.note || null;
  const saveButton = $("#saveAnswerNote");
  if (saveButton) {
    saveButton.disabled = true;
    saveButton.textContent = cached?.persisted ? "Three-Pass 已保存到结构化阅读笔记" : "Three-Pass 已生成，但本地保存失败";
  }
}

function renderThreePassMessages(messages = []) {
  const target = $("#threePassMessages");
  target.innerHTML = messages.map(item => `
    <article class="three-pass-message-pair">
      <strong>${escapeHtml(item.question || "追问")}</strong>
      <div>${answerMarkdownHtml(item.answer || "")}</div>
    </article>
  `).join("");
}

function renderThreePassAnalysis(analysis) {
  readerState.threePassAnalysis = analysis || null;
  $("#threePassProgress").querySelectorAll("[data-analysis-phase]").forEach(item => {
    const state = threePassProgressState(analysis, item.dataset.analysisPhase);
    item.dataset.state = state;
  });
  const active = Boolean(analysis && (THREE_PASS_ACTIVE.has(analysis.status) || analysis.busy));
  $("#cancelThreePass").disabled = !active;
  $("#startThreePass").disabled = active || !threePassBackendUsable();
  $("#threePassModel").disabled = active;
  $("#threePassEffort").disabled = active;
  $("#threePassLanguage").disabled = active;
  $("#threePassBackend").disabled = active;
  $("#threePassApiPreset").disabled = active;
  const message = $("#threePassMessage");
  if (!analysis) {
    message.textContent = selectedThreePassBackend() === "api"
      ? "选择 API 预设与关注点后开始；四阶段上下文保存在本机。"
      : "选择模型与关注点后开始；四个阶段在同一个 Codex thread 中运行。";
  } else if (analysis.error) {
    message.textContent = analysis.error;
  } else {
    const provider = analysis.backend === "api" ? `API · ${analysis.providerName || analysis.providerId || analysis.protocol}` : "Codex";
    const queue = analysis.status === "queued" && analysis.options?.queuePosition ? ` · 队列 ${analysis.options.queuePosition}` : "";
    const warning = analysis.warnings?.length ? ` · ${analysis.warnings.at(-1)}` : "";
    message.textContent = `${provider} · ${analysis.phaseLabel || analysis.status}${queue}${warning}`;
  }
  const report = analysis?.results?.synthesis;
  const preview = report || analysis?.preview || readerState.threePassPreview;
  const previewNode = $("#threePassPreview");
  previewNode.classList.toggle("hidden", !preview);
  if (preview) {
    previewNode.innerHTML = report ? answerMarkdownHtml(report) : `<pre>${escapeHtml(preview)}</pre>`;
  }
  const reportActions = $("#threePassReportActions");
  reportActions.classList.toggle("hidden", !analysis?.reportReady);
  if (analysis?.reportUrl) $("#downloadThreePassReport").href = analysis.reportUrl;
  $("#threePassFollowup").classList.toggle("hidden", analysis?.status !== "ready" || analysis?.busy);
  renderThreePassMessages(analysis?.messages || []);
  publishThreePassAnswer(analysis);
}

async function refreshThreePassAnalysis(analysisId = readerState.threePassAnalysis?.id) {
  if (!analysisId) return;
  const response = await fetch(`/api/reader/analyses/${encodeURIComponent(analysisId)}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 Three-Pass 分析。 ");
  readerState.threePassPreview = data.preview || "";
  renderThreePassAnalysis(data);
}

function connectThreePassEvents(analysisId) {
  readerState.threePassEventSource?.close();
  clearInterval(readerState.threePassPoller);
  readerState.threePassPoller = null;
  readerState.threePassPreview = "";
  const source = new EventSource(`/api/reader/analyses/${encodeURIComponent(analysisId)}/events`);
  readerState.threePassEventSource = source;
  const refresh = event => {
    const data = JSON.parse(event.data);
    readerState.threePassPreview = data.preview || readerState.threePassPreview;
    renderThreePassAnalysis(data);
    if (["ready", "failed", "cancelled"].includes(data.status) && !data.busy) {
      source.close();
      if (readerState.threePassEventSource === source) readerState.threePassEventSource = null;
    }
  };
  source.addEventListener("snapshot", refresh);
  source.addEventListener("phase", refresh);
  source.addEventListener("completed", refresh);
  source.addEventListener("cancelled", refresh);
  source.addEventListener("followup_cancelled", refresh);
  source.addEventListener("error", event => {
    if (event.data) {
      refresh(event);
      return;
    }
    $("#threePassMessage").textContent = "实时连接中断，正在使用定时状态查询…";
    source.close();
    if (readerState.threePassEventSource === source) readerState.threePassEventSource = null;
    if (!readerState.threePassPoller) {
      readerState.threePassPoller = setInterval(() => {
        refreshThreePassAnalysis(analysisId).then(() => {
          const current = readerState.threePassAnalysis;
          if (current && ["ready", "failed", "cancelled"].includes(current.status) && !current.busy) {
            clearInterval(readerState.threePassPoller);
            readerState.threePassPoller = null;
          }
        }).catch(error => { $("#threePassMessage").textContent = `状态查询失败：${error.message}`; });
      }, 2500);
    }
  });
  source.addEventListener("delta", event => {
    const data = JSON.parse(event.data);
    readerState.threePassPreview += data.delta || "";
    const current = readerState.threePassAnalysis || { id: analysisId, status: data.phase, phase: data.phase };
    renderThreePassAnalysis({ ...current, preview: readerState.threePassPreview });
  });
  source.addEventListener("phase_completed", () => refreshThreePassAnalysis(analysisId).catch(() => {}));
  source.addEventListener("message", () => refreshThreePassAnalysis(analysisId).catch(() => {}));
}

async function loadThreePassAnalyses() {
  if (!readerState.document) return;
  try {
    const response = await fetch(`/api/reader/documents/${encodeURIComponent(readerState.document.id)}/analyses`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取分析历史。 ");
    const analyses = data.analyses || [];
    const history = $("#threePassHistory");
    history.innerHTML = analyses.length
      ? analyses.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(new Date(item.createdAt * 1000).toLocaleString("zh-CN"))} · ${escapeHtml(item.backend === "api" ? `API/${item.providerName || item.providerId}` : "Codex")} · ${escapeHtml(item.model)} · ${escapeHtml(item.phaseLabel)}</option>`).join("")
      : "<option value=\"\">尚无历史分析</option>";
    if (analyses.length) {
      renderThreePassAnalysis(analyses[0]);
      connectThreePassEvents(analyses[0].id);
    } else {
      renderThreePassAnalysis(null);
    }
  } catch (error) {
    $("#threePassMessage").textContent = error.message;
  }
}

async function createThreePassRequest(confirmedChunkedCalls = false) {
  const backend = selectedThreePassBackend();
  const temperatureValue = $("#threePassTemperature").value.trim();
  const common = {
    backend,
    language: $("#threePassLanguage").value,
    focuses: [...$("#threePassFocus").querySelectorAll("input:checked")].map(input => input.value),
    customFocus: $("#threePassCustomFocus").value.trim(),
  };
  if (backend === "api") {
    return {
      ...common,
      codex: null,
      api: {
        presetId: $("#threePassApiPreset").value,
        model: $("#threePassApiModel").value.trim() || null,
        reasoningEffort: $("#threePassApiEffort").value,
        maxOutputTokens: Number($("#threePassMaxOutput").value || 12000),
        temperature: temperatureValue === "" ? null : Number(temperatureValue),
        confirmedApiBilling: $("#threePassBillingConfirmed").checked,
        confirmedChunkedCalls,
      },
    };
  }
  return {
    ...common,
    codex: { model: $("#threePassModel").value, reasoningEffort: $("#threePassEffort").value },
    api: null,
  };
}

async function startThreePassAnalysis(confirmedChunkedCalls = false) {
  if (!readerState.document) return;
  const focuses = [...$("#threePassFocus").querySelectorAll("input:checked")].map(input => input.value);
  if (!focuses.length) {
    $("#threePassMessage").textContent = "请至少选择一个第三遍关注重点。";
    return;
  }
  $("#startThreePass").disabled = true;
  $("#threePassMessage").textContent = "正在创建 Three-Pass 分析…";
  try {
    const response = await fetch(`/api/reader/documents/${encodeURIComponent(readerState.document.id)}/analyses`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(await createThreePassRequest(confirmedChunkedCalls)),
    });
    const data = await response.json();
    if (response.status === 409 && data.requiresChunkConfirmation) {
      const plan = data.chunkPlan || {};
      const accepted = window.confirm(`这篇论文预计分为 ${plan.chunkCount || "多"} 块，约调用 API ${plan.estimatedCalls || "多"} 次。继续将产生额外 API 费用，是否确认？`);
      if (accepted) return startThreePassAnalysis(true);
      throw new Error("已取消分块 API 分析。 ");
    }
    if (!response.ok) throw new Error(data.error || "无法创建 Three-Pass 分析。 ");
    $("#threePassPanel").open = true;
    renderThreePassAnalysis(data);
    await loadThreePassAnalyses();
    $("#threePassHistory").value = data.id;
  } catch (error) {
    $("#threePassMessage").textContent = error.message;
  } finally {
    updateThreePassStartAvailability();
  }
}

async function cancelThreePassAnalysis() {
  const analysis = readerState.threePassAnalysis;
  if (!analysis) return;
  $("#cancelThreePass").disabled = true;
  const response = await fetch(`/api/reader/analyses/${encodeURIComponent(analysis.id)}/cancel`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法取消分析。 ");
  renderThreePassAnalysis(data);
}

async function submitThreePassFollowup(event) {
  event.preventDefault();
  const analysis = readerState.threePassAnalysis;
  const question = $("#threePassQuestion").value.trim();
  if (!analysis || !question) return;
  const response = await fetch(`/api/reader/analyses/${encodeURIComponent(analysis.id)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法发送追问。 ");
  $("#threePassQuestion").value = "";
  renderThreePassAnalysis(data);
  connectThreePassEvents(data.id);
}

async function showReadyDocument(data, restoredFromCache = false) {
  readerState.document = data;
  renderAlignmentProgress(data);
  await loadDocumentNotes();
  $("#uploadCard").classList.add("hidden");
  $("#readerShell").classList.remove("hidden");
  document.body.classList.add("reader-active");
  applyTableOfContentsVisibility();
  lockReaderPaperWidth();
  applyAssistantWidth();
  await loadContent();
  await loadThreePassAnalyses();
  if (restoredFromCache) $("#readingStatus").textContent = "本地内容 · 最新功能";
}

async function openReaderDocumentFromQuery() {
  const documentId = new URLSearchParams(window.location.search).get("document");
  if (!documentId) return false;
  const response = await fetch(`/api/reader/documents/${encodeURIComponent(documentId)}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法打开导入的阅读器文档。");
  readerState.document = data;
  if (data.status === "ready") {
    await showReadyDocument(data, (data.cacheHits || []).length > 0);
  } else if (data.status === "failed") {
    throw new Error(data.error || "导入的阅读器文档不可用。");
  } else {
    $("#uploadStatus").textContent = data.message || "正在准备导入的阅读器文档…";
    renderAlignmentProgress(data);
    pollDocument();
  }
  return true;
}

function pollDocument() {
  stopPolling();
  const poll = async () => {
    try {
      const response = await fetch(`/api/reader/documents/${readerState.document.id}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "无法查询文档状态。");
      readerState.document = data;
      $("#uploadStatus").textContent = data.message;
      renderAlignmentProgress(data);
      if (data.status === "ready") {
        stopPolling();
        await showReadyDocument(data, (data.cacheHits || []).length > 0);
      } else if (data.status === "failed") {
        stopPolling();
        $("#uploadStatus").textContent = data.error || "文档处理失败。";
      }
    } catch (error) {
      stopPolling();
      $("#uploadStatus").textContent = error.message;
    }
  };
  poll();
  readerState.poller = setInterval(poll, 1000);
}

function localLibraryDate(timestamp) {
  if (!timestamp) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}

function renderLocalLibrary(documents) {
  const target = $("#localLibraryList");
  if (!documents.length) {
    target.innerHTML = `<p class="local-library-empty">本机还没有可恢复的阅读文档。导入任意支持的文档后都会自动缓存在这里。</p>`;
    return;
  }
  target.innerHTML = documents.map(item => {
    const suffix = String(item.sourceName || "").split(".").pop().toUpperCase();
    const sourceBadge = item.renderKind === "pdf"
      ? "PDF"
      : item.renderKind === "markdown"
        ? (suffix === "MMD" ? "MMD" : "Markdown")
        : item.useOcr ? "OCR" : "HTML";
    const canTranslate = item.hasTranslation || item.canTranslateDocument !== false;
    return `
    <article class="local-document">
      <div class="local-document-row">
        <button class="local-document-open" type="button" data-cache-key="${escapeHtml(item.id)}">
          <span class="local-document-copy">
            <strong>${escapeHtml(item.title)}</strong>
            <small>${escapeHtml(item.sourceName)} · ${escapeHtml(localLibraryDate(item.updatedAt))}</small>
          </span>
          <span class="local-document-badges">
            <span>${sourceBadge}</span>${item.hasTranslation ? `<span>有译文</span>` : ""}
            ${item.readerUpdatePolicy === "latest" ? `<span class="latest-reader-badge">功能自动更新</span>` : ""}
          </span>
        </button>
        <button
          class="local-document-rename"
          type="button"
          data-rename-cache-key="${escapeHtml(item.id)}"
          aria-label="重命名 ${escapeHtml(item.title)}"
        >重命名</button>
      </div>
      <form class="local-document-rename-form hidden" data-rename-form="${escapeHtml(item.id)}">
        <input type="text" value="${escapeHtml(item.title)}" maxlength="160" aria-label="新的项目名称" required>
        <button type="submit">保存</button>
        <button type="button" data-cancel-rename>取消</button>
      </form>
      <label class="local-translation-choice ${canTranslate ? "" : "hidden"}">
        <input type="checkbox" data-local-translation data-has-translation="${String(item.hasTranslation)}" ${canTranslate ? "" : "disabled"}>
        <span>${item.hasTranslation ? "同时打开已有译文" : "同时生成整篇译文"}</span>
      </label>
    </article>
  `;
  }).join("");
  target.querySelectorAll("[data-cache-key]").forEach(button => {
    button.addEventListener("click", () => openLocalDocument(button.dataset.cacheKey, button));
  });
  target.querySelectorAll("[data-rename-cache-key]").forEach(button => {
    button.addEventListener("click", () => {
      const card = button.closest(".local-document");
      const form = card?.querySelector("[data-rename-form]");
      form?.classList.remove("hidden");
      const input = form?.querySelector("input");
      input?.focus();
      input?.select();
    });
  });
  target.querySelectorAll("[data-cancel-rename]").forEach(button => {
    button.addEventListener("click", () => {
      button.closest("[data-rename-form]")?.classList.add("hidden");
    });
  });
  target.querySelectorAll("[data-rename-form]").forEach(form => {
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const title = form.querySelector("input").value.trim();
      if (!title) return;
      form.querySelectorAll("input,button").forEach(control => { control.disabled = true; });
      try {
        const response = await fetch(
          `/api/reader/library/${encodeURIComponent(form.dataset.renameForm)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title }),
          },
        );
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "无法重命名本地项目。");
        $("#uploadStatus").textContent = `已重命名为“${data.document.title}”。`;
        await loadLocalLibrary();
      } catch (error) {
        $("#uploadStatus").textContent = error.message;
        form.querySelectorAll("input,button").forEach(control => { control.disabled = false; });
        form.querySelector("input")?.focus();
      }
    });
  });
  target.querySelectorAll("[data-local-translation]").forEach(checkbox => {
    checkbox.addEventListener("change", updateProcessingControls);
  });
}

async function loadLocalLibrary() {
  const target = $("#localLibraryList");
  target.innerHTML = `<p class="local-library-empty">正在检索本地文档…</p>`;
  try {
    const response = await fetch("/api/reader/library");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取本地文档库。");
    renderLocalLibrary(data.documents || []);
  } catch (error) {
    target.innerHTML = `<p class="local-library-empty">本地文档检索失败：${escapeHtml(error.message)}</p>`;
  }
}

async function openLocalDocument(cacheKey, button) {
  const card = button.closest(".local-document");
  const translation = card?.querySelector("[data-local-translation]");
  const includeTranslation = translation?.checked === true;
  const hasTranslation = translation?.dataset.hasTranslation === "true";
  const payload = { includeTranslation };
  if (includeTranslation && !hasTranslation) {
    try {
      payload.llm = llmConfig($("#uploadLlm"));
    } catch (error) {
      $("#uploadStatus").textContent = error.message;
      return;
    }
  }
  $("#localLibraryList").querySelectorAll("button,input").forEach(item => { item.disabled = true; });
  $("#uploadStatus").textContent = "正在载入本地内容并应用最新阅读功能…";
  try {
    const response = await fetch(`/api/reader/library/${encodeURIComponent(cacheKey)}/open`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法恢复本地文档。");
    $("#uploadStatus").textContent = data.message;
    readerState.document = data;
    if (data.status === "ready") await showReadyDocument(data, true);
    else pollDocument();
  } catch (error) {
    $("#uploadStatus").textContent = error.message;
    $("#localLibraryList").querySelectorAll("button,input").forEach(item => { item.disabled = false; });
    button?.focus();
  }
}

function updateProcessingControls() {
  const name = readerState.sourceFile?.name || "";
  const isPdf = name.toLowerCase().endsWith(".pdf");
  const isMarkdown = /\.(?:md|mmd)$/i.test(name);
  const isHtml = /\.html?$/i.test(name);
  const hasExistingTranslation = Boolean(readerState.translationFile);
  const alignmentFamily = readerFileFamily(readerState.sourceFile);
  const canAlignTranslation = hasExistingTranslation
    && new Set(["markdown", "html"]).has(alignmentFamily)
    && readerFileFamily(readerState.translationFile) === alignmentFamily;
  const alignTranslation = $("#alignTranslation");
  $("#alignmentOption")?.classList.toggle("hidden", !canAlignTranslation);
  if (alignTranslation) {
    alignTranslation.disabled = !canAlignTranslation;
    if (!canAlignTranslation) alignTranslation.checked = false;
  }
  $("#useOcr").disabled = !isPdf || hasExistingTranslation;
  $("#ocrOption").classList.toggle("hidden", !isPdf || hasExistingTranslation);
  $("#existingTranslationOption")?.classList.toggle("hidden", !name);
  if (hasExistingTranslation) {
    $("#useOcr").checked = false;
    $("#generateTranslation").checked = false;
  }
  if (!isPdf) $("#useOcr").checked = false;
  const canGenerateTranslation = isMarkdown || isHtml || (isPdf && $("#useOcr").checked);
  if (!canGenerateTranslation && $("#generateTranslation").checked) $("#generateTranslation").checked = false;
  $("#translationOption").classList.toggle("hidden", hasExistingTranslation || !canGenerateTranslation);
  $("#translationOption").classList.toggle("disabled", !canGenerateTranslation);
  $("#generateTranslation").disabled = hasExistingTranslation || !canGenerateTranslation;
  const hint = $("#pdfTextHint");
  hint.textContent = hasExistingTranslation
    ? (canAlignTranslation && alignTranslation?.checked
      ? "已选择现成译文：将调用所选 LLM 修复错误换行造成的 1:N / N:1 段落错位。"
      : "已选择现成译文：将按现有段落直接建立原文/译文对照，不调用 OCR 或 LLM。")
    : isPdf
    ? ($("#useOcr").checked ? "OCR 已开启：Mathpix 将生成 HTML，并可交给 AI-Markdown-Translator 翻译。" : readerState.pdfInspectionHint || "直接阅读 PDF：不会发送给 OCR 服务。")
    : isMarkdown
    ? "Markdown / MMD 可由 AI-Markdown-Translator 后端生成整篇译文。"
    : "HTML / HTM 可作为原始文本分块交给 AI-Markdown-Translator 生成整篇译文。";
  hint.classList.toggle("warning", isPdf && !$("#useOcr").checked && readerState.pdfInspectionWarning);
  const localTranslationNeedsLlm = [...document.querySelectorAll("[data-local-translation]")]
    .some(input => input.checked && input.dataset.hasTranslation !== "true");
  const pairAlignmentNeedsLlm = canAlignTranslation && alignTranslation?.checked;
  $("#uploadLlm").classList.toggle(
    "hidden",
    !$("#generateTranslation").checked && !localTranslationNeedsLlm && !pairAlignmentNeedsLlm,
  );
}

async function inspectPdfTextLayer(file) {
  const token = ++readerState.inspectionToken;
  readerState.pdfInspectionHint = "正在本机检测 PDF 文字层…";
  readerState.pdfInspectionWarning = false;
  updateProcessingControls();
  try {
    const pdfjs = await loadPdfJs();
    const task = pdfjs.getDocument(pdfDocumentOptions({ data: new Uint8Array(await file.arrayBuffer()) }));
    const pdf = await task.promise;
    const pages = Math.min(3, pdf.numPages);
    let characters = 0;
    for (let pageNumber = 1; pageNumber <= pages; pageNumber += 1) {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      characters += content.items.map(item => item.str || "").join("").replace(/\s/g, "").length;
    }
    await task.destroy();
    if (token !== readerState.inspectionToken) return;
    const minimumCharacters = pages * 50;
    readerState.pdfInspectionWarning = characters < minimumCharacters;
    readerState.pdfInspectionHint = characters < minimumCharacters
      ? "前 3 页未检测到稳定文字层，可能是扫描件；建议手动开启 OCR。"
      : `已检测到可选文字层（抽样 ${pages} 页），可直接阅读，无需 OCR。`;
  } catch (error) {
    if (token !== readerState.inspectionToken) return;
    readerState.pdfInspectionWarning = true;
    readerState.pdfInspectionHint = `无法预检文字层：${error.message}。你仍可手动决定是否 OCR。`;
  }
  updateProcessingControls();
}

async function submitUpload(event) {
  event.preventDefault();
  const source = readerState.sourceFile || $("#sourceFile").files[0];
  if (!source) return;
  const form = new FormData();
  form.append("file", source);
  if (readerState.translationFile) form.append("translationFile", readerState.translationFile);
  const assets = [...($("#markdownAssets")?.files || [])];
  if (assets.length) {
    form.append("assetManifest", JSON.stringify(assets.map(asset => ({
      relativePath: asset.webkitRelativePath || asset.name,
    }))));
    assets.forEach(asset => form.append("assets", asset, asset.name));
  }
  form.append("useOcr", String($("#useOcr").checked));
  form.append("generateTranslation", String($("#generateTranslation").checked));
  form.append("useLocalCache", String($("#useLocalCache").checked));
  form.append("alignTranslation", String($("#alignTranslation")?.checked === true));
  if ($("#generateTranslation").checked || $("#alignTranslation")?.checked) {
    try {
      form.append("llm", JSON.stringify(llmConfig($("#uploadLlm"))));
    } catch (error) {
      $("#uploadStatus").textContent = error.message;
      return;
    }
  }
  $("#uploadStatus").textContent = "正在提交本地任务…";
  try {
    const response = await fetch("/api/reader/documents", { method: "POST", body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "上传失败。");
    readerState.document = data;
    renderAlignmentProgress(data);
    pollDocument();
  } catch (error) {
    $("#uploadStatus").textContent = error.message;
  }
}

function setReaderSource(file) {
  if (!file) return;
  const suffix = `.${file.name.split(".").pop().toLowerCase()}`;
  if (!new Set([".pdf", ".md", ".mmd", ".html", ".htm"]).has(suffix)) {
    $("#uploadStatus").textContent = "仅支持 PDF、Markdown、MMD 或 HTML 文件。";
    return;
  }
  readerState.sourceFile = file;
  readerState.translationFile = null;
  if ($("#translationFile")) $("#translationFile").value = "";
  if ($("#translationFileSummary")) $("#translationFileSummary").textContent = "选择已有译文";
  const isMarkdown = suffix === ".md" || suffix === ".mmd";
  const isHtml = suffix === ".html" || suffix === ".htm";
  const supportsImageDirectory = isMarkdown || isHtml;
  $("#markdownAssetsOption").classList.toggle("hidden", !supportsImageDirectory);
  if ($("#readerAssetsTitle")) {
    $("#readerAssetsTitle").textContent = isHtml ? "HTML 图片资源目录（可选）" : "Markdown 图片目录（可选）";
  }
  if ($("#readerAssetsHint")) {
    $("#readerAssetsHint").textContent = isHtml
      ? "请选择 HTML 中相对路径对应的 images、assets 或同类图片目录；目录名与层级会原样保留。"
      : "若正文使用相对图片路径，请选择对应的 .assets、images 或图片目录；目录结构会随文档一同缓存。";
  }
  if (!supportsImageDirectory) {
    $("#markdownAssets").value = "";
    $("#markdownAssetsSummary").textContent = "选择图片目录";
  }
  readerState.pdfInspectionHint = "";
  readerState.pdfInspectionWarning = false;
  $("#fileName").textContent = file.name;
  $("#uploadStatus").textContent = "";
  updateProcessingControls();
  if (suffix === ".pdf") inspectPdfTextLayer(file);
}

function readerFileFamily(file) {
  const suffix = `.${String(file?.name || "").split(".").pop().toLowerCase()}`;
  if (suffix === ".pdf") return "pdf";
  if (suffix === ".md" || suffix === ".mmd") return "markdown";
  if (suffix === ".html" || suffix === ".htm") return "html";
  return "";
}

function setReaderTranslation(file) {
  if (!file) {
    readerState.translationFile = null;
    if ($("#translationFileSummary")) $("#translationFileSummary").textContent = "选择已有译文";
    updateProcessingControls();
    return;
  }
  if (!readerState.sourceFile || readerFileFamily(file) !== readerFileFamily(readerState.sourceFile)) {
    readerState.translationFile = null;
    if ($("#translationFile")) $("#translationFile").value = "";
    if ($("#translationFileSummary")) $("#translationFileSummary").textContent = "选择已有译文";
    $("#uploadStatus").textContent = "译文必须与原文使用相同格式：PDF、Markdown/MMD 或 HTML。";
    updateProcessingControls();
    return;
  }
  readerState.translationFile = file;
  if ($("#alignTranslation")) {
    $("#alignTranslation").checked = new Set(["markdown", "html"]).has(readerFileFamily(file));
  }
  if ($("#translationFileSummary")) $("#translationFileSummary").textContent = file.name;
  $("#uploadStatus").textContent = "";
  renderAlignmentProgress({});
  updateProcessingControls();
}

function resetReader() {
  stopPolling();
  stopParagraphSpeech();
  readerState.threePassEventSource?.close();
  readerState.threePassEventSource = null;
  clearInterval(readerState.threePassPoller);
  readerState.threePassPoller = null;
  readerState.threePassAnalysis = null;
  readerState.threePassPreview = "";
  readerState.document = null;
  updateSpeechControl();
  ++readerState.renderToken;
  readerState.pdfLoadingTask?.destroy?.().catch(() => {});
  readerState.pdfLoadingTask = null;
  readerState.pdfDocument = null;
  readerState.content = null;
  readerState.selection = "";
  readerState.selectionAnchor = null;
  readerState.notes = [];
  readerState.textFormats = [];
  readerState.lastAnswer = null;
  readerState.pdfRenderWidth = 0;
  readerState.tableOfContentsOverlayOpen = false;
  readerState.assistantOverlayOpen = false;
  unlockReaderPaperWidth();
  $("#readerShell").classList.add("hidden");
  document.body.classList.remove("reader-active");
  $("#uploadCard").classList.remove("hidden");
  $("#uploadForm").reset();
  readerState.sourceFile = null;
  readerState.translationFile = null;
  ++readerState.inspectionToken;
  readerState.pdfInspectionHint = "";
  readerState.pdfInspectionWarning = false;
  $("#fileName").textContent = "尚未选择文件";
  if ($("#translationFileSummary")) $("#translationFileSummary").textContent = "选择已有译文";
  $("#uploadStatus").textContent = "";
  toggleSelectionSettings(false);
  updateProcessingControls();
  loadLocalLibrary();
}

async function initReader() {
  loadSelectionSettings();
  applyTableOfContentsVisibility();
  applyReaderTypography();
  applyReaderAxisOffset();
  applyAssistantWidth();
  initAssistantResize();
  initPaperResize();
  initResponsiveReaderLayout();
  const response = await fetch("/api/reader/config");
  readerState.config = await response.json();
  if (!response.ok) throw new Error(readerState.config.error || "无法读取阅读器配置。");
  refreshLlmConfigs();
  renderSelectionSettings();
  renderActions();
  await loadCodexStatus();
  await loadAnalysisProviders();
  await loadLocalLibrary();
  $("#sourceFile").addEventListener("change", event => setReaderSource(event.target.files[0]));
  $("#translationFile")?.addEventListener("change", event => setReaderTranslation(event.target.files[0]));
  $("#markdownAssets").addEventListener("change", event => {
    const files = [...event.target.files];
    $("#markdownAssetsSummary").textContent = files.length
      ? `已选择 ${files.length} 张图片`
      : "选择图片目录";
  });
  const dropzone = $("#readerDropzone");
  ["dragenter", "dragover"].forEach(type => dropzone.addEventListener(type, event => {
    event.preventDefault();
    dropzone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach(type => dropzone.addEventListener(type, event => {
    event.preventDefault();
    dropzone.classList.remove("dragover");
  }));
  dropzone.addEventListener("drop", event => setReaderSource(event.dataTransfer.files[0]));
  $("#useOcr").addEventListener("change", updateProcessingControls);
  $("#generateTranslation").addEventListener("change", updateProcessingControls);
  $("#alignTranslation")?.addEventListener("change", updateProcessingControls);
  $("#refreshLocalLibrary").addEventListener("click", loadLocalLibrary);
  $("#uploadForm").addEventListener("submit", submitUpload);
  $("#threePassModel").addEventListener("change", () => renderThreePassEfforts("high"));
  $("#threePassBackend").addEventListener("change", renderThreePassBackend);
  $("#threePassLanguage").addEventListener("change", renderThreePassLengthSummary);
  $("#threePassApiPreset").addEventListener("change", renderThreePassApiPresets);
  $("#threePassBillingConfirmed").addEventListener("change", updateThreePassStartAvailability);
  $("#codexLogin").addEventListener("click", () => startCodexLogin("browser").catch(error => { $("#codexLoginDetails").classList.remove("hidden"); $("#codexLoginDetails").textContent = error.message; }));
  $("#codexDeviceLogin").addEventListener("click", () => startCodexLogin("device_code").catch(error => { $("#codexLoginDetails").classList.remove("hidden"); $("#codexLoginDetails").textContent = error.message; }));
  $("#refreshCodexStatus").addEventListener("click", () => loadCodexStatus(true).then(loadAnalysisProviders).catch(error => { $("#codexStatus").textContent = error.message; }));
  $("#codexLogout").addEventListener("click", () => logoutCodex().catch(error => { $("#codexStatus").textContent = error.message; }));
  $("#startThreePass").addEventListener("click", () => {
    startThreePassAnalysis().catch(error => {
      $("#threePassMessage").textContent = error.message || "Three-Pass 启动失败。";
      updateThreePassStartAvailability();
    });
  });
  $("#cancelThreePass").addEventListener("click", () => {
    cancelThreePassAnalysis().catch(error => { $("#threePassMessage").textContent = error.message; });
  });
  $("#threePassHistory").addEventListener("change", event => {
    const analysisId = event.target.value;
    if (!analysisId) return renderThreePassAnalysis(null);
    refreshThreePassAnalysis(analysisId)
      .then(() => connectThreePassEvents(analysisId))
      .catch(error => { $("#threePassMessage").textContent = error.message; });
  });
  $("#threePassFollowup").addEventListener("submit", event => {
    submitThreePassFollowup(event).catch(error => { $("#threePassMessage").textContent = error.message; });
  });
  $("#saveSelectionNote").addEventListener("click", saveCurrentSelectionNote);
  $("#copyNotes").addEventListener("click", copyNotesMarkdown);
  $("#exportNotes").addEventListener("click", exportNotesMarkdown);
  $("#clearNotes").addEventListener("click", () => {
    if (!readerState.notes.length || !window.confirm("确定清空这篇文档的全部阅读笔记吗？")) return;
    readerState.notes = [];
    persistDocumentNotes();
    renderNotes();
    renderReaderDecorations($("#readerDocumentFrame")?.contentDocument || document);
    setNotesStatus("已清空当前文档的阅读笔记。");
  });
  $("#paperContent").addEventListener("mouseup", () => setTimeout(() => captureSelection(), 0));
  $("#paperContent").addEventListener("touchend", () => setTimeout(() => captureSelection(), 0));
  $("#paperContent").addEventListener("click", event => handleParagraphSpeechClick(event));
  $("#startSpeech").addEventListener("click", toggleParagraphSpeechClick);
  $("#speechPlaybackRate").addEventListener("change", event => updateSpeechPlaybackRate(event.target.value));
  $("#stopSpeech").addEventListener("click", () => stopParagraphSpeech("已停止朗读"));
  $("#selectionHighlightColor").addEventListener("change", event => {
    updateCurrentTextFormat({ highlightColor: normalizeFormatColor(event.target.value) }, "选区底色已保存。");
  });
  $("#selectionTextColor").addEventListener("change", event => {
    updateCurrentTextFormat({ textColor: normalizeFormatColor(event.target.value) }, "选区文字颜色已保存。");
  });
  $("#selectionMenu").querySelectorAll("[data-clear-format-color]").forEach(button => {
    button.addEventListener("click", () => {
      const property = button.dataset.clearFormatColor;
      updateCurrentTextFormat({ [property]: "" }, property === "highlightColor" ? "已清除选区底色。" : "已恢复选区文字颜色。");
    });
  });
  $("#selectionMenu").querySelectorAll("[data-format-toggle]").forEach(button => {
    button.addEventListener("click", () => {
      const property = button.dataset.formatToggle;
      const enabled = !Boolean(currentTextFormat()?.[property]);
      const labels = { bold: "粗体", italic: "斜体", underline: "下划线", strike: "删除线" };
      updateCurrentTextFormat({ [property]: enabled }, `已${enabled ? "启用" : "取消"}${labels[property]}。`);
    });
  });
  $("#clearSelectionFormat").addEventListener("click", () => {
    clearCurrentTextFormats();
  });
  $("#selectionSettingsButton").addEventListener("click", () => toggleSelectionSettings());
  $("#closeSelectionSettings").addEventListener("click", () => toggleSelectionSettings(false));
  $("#showTableOfContents").addEventListener("change", event => {
    readerState.showTableOfContents = event.target.checked;
    applyTableOfContentsVisibility();
    persistSelectionSettings(event.target.checked
      ? "文档目录已显示。"
      : "文档目录已关闭，正文已扩展到左侧。");
  });
  ["chineseFont", "englishFont", "mathFont"].forEach(id => {
    $(`#${id}`).addEventListener("change", () => updateTypographyFromInputs());
  });
  $("#resetTypography").addEventListener("click", () => {
    readerState.typography = { ...DEFAULT_TYPOGRAPHY };
    renderTypographySettings();
    applyReaderTypography();
    persistSelectionSettings("阅读字体已恢复默认。");
  });
  [
    "readerFontSize",
    "readerLetterSpacing",
    "readerLineHeight",
    "readerParagraphSpacing",
  ].forEach(id => {
    $(`#${id}`).addEventListener("change", () => updateReadingLayoutFromInputs());
  });
  document.querySelectorAll("[data-page-margin-zone][data-page-margin-side]").forEach(input => {
    input.addEventListener("change", () => updateReadingLayoutFromInputs("页面区域边距已更新。"));
  });
  $("#resetReadingLayout").addEventListener("click", () => {
    readerState.readingLayout = normalizeReadingLayout();
    renderReadingLayoutSettings();
    applyReaderTypography();
    persistSelectionSettings("字号、间距与页面边距已恢复默认。");
  });
  $("#autoSelectionAction").addEventListener("change", event => {
    readerState.autoSelectionAction = event.target.checked;
    $("#defaultSelectionAction").disabled = !event.target.checked;
    persistSelectionSettings(event.target.checked ? "已启用划词默认行为。" : "已关闭划词默认行为。");
  });
  $("#defaultSelectionAction").addEventListener("change", event => {
    readerState.defaultSelectionAction = event.target.value;
    persistSelectionSettings(`默认行为已设为“${actionLabel(event.target.value)}”。`);
  });
  document.addEventListener("mousedown", event => {
    if (!event.target.closest("#selectionMenu")) hideSelectionMenu();
    if (!event.target.closest("#selectionSettingsPanel, #selectionSettingsButton")) toggleSelectionSettings(false);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      hideSelectionMenu();
      toggleSelectionSettings(false);
      closeResponsivePanels();
    }
  });
  window.addEventListener("pagehide", () => {
    const documentId = readerState.document?.id;
    if (!documentId || !noteStorageKey() || !readerState.notesUpdatedAt) return;
    clearTimeout(schedulePersistentReaderState.timer);
    fetch(`/api/reader/documents/${documentId}/state`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readerStatePayload()),
      keepalive: true,
    }).catch(() => {});
  });
  $("#viewSwitch").querySelectorAll("button").forEach(button => button.addEventListener("click", async () => {
    readerState.view = button.dataset.view;
    $("#viewSwitch").querySelectorAll("button").forEach(item => item.classList.toggle("active", item === button));
    await renderPaper();
  }));
  $("#zoomOut").addEventListener("click", () => {
    readerState.pdfScale = Math.max(0.75, readerState.pdfScale - 0.15);
    renderPaper();
  });
  $("#zoomIn").addEventListener("click", () => {
    readerState.pdfScale = Math.min(2.2, readerState.pdfScale + 0.15);
    renderPaper();
  });
  $("#newPaper").addEventListener("click", resetReader);
  renderThreePassBackend();
  updateProcessingControls();
  await openReaderDocumentFromQuery();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    anchorsOverlap,
    assignPromptUnderlineLanes,
    answerMarkdownHtml,
    calculateNoteBadgeMetrics,
    calculatePromptUnderlineOffset,
    htmlInterleavableBlocks,
    alignmentProgressView,
    interleavedBlockPairs,
    interleavedHtmlBlockPairs,
    isShortTranslationSelection,
    liveSourceFingerprint,
    localAsset,
    markdownInline,
    notesMatchingAnchor,
    normalizeAssistantWidth,
    normalizePaperWidth,
    normalizeReaderAxisOffset,
    normalizeFontFamily,
    normalizeFormatColor,
    normalizeReadingLayout,
    normalizeSpeechPlaybackRate,
    notesToMarkdown,
    pdfParagraphGroups,
    pdfFitScale,
    pdfReflowNeeded,
    readerSpeechAvailable,
    readerLayoutForWidth,
    readerPaperColumnWidth,
    responsiveReaderWidth,
    threePassEffortView,
    threePassAnswerMarkdown,
    threePassLengthSummary,
    threePassProgressState,
    compactRateLimitText,
    textFormatHasStyle,
  };
}

if (typeof document !== "undefined") {
  initReader().catch(error => {
    $("#uploadStatus").textContent = `初始化失败：${error.message}`;
  });
}
