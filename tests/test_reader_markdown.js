const assert = require("node:assert/strict");
const {
  anchorsOverlap,
  assignPromptUnderlineLanes,
  answerMarkdownHtml,
  calculateNoteBadgeMetrics,
  calculatePromptUnderlineOffset,
  alignmentProgressView,
  htmlInterleavableBlocks,
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
  threePassAnswerMarkdown,
  threePassEffortView,
  threePassLengthSummary,
  threePassProgressState,
  compactRateLimitText,
  textFormatHasStyle,
} = require("../static/reader.js");

assert.deepEqual(
  threePassEffortView({ supportedEfforts: ["medium", "high"], defaultEffort: "medium" }, "high"),
  { efforts: ["medium", "high"], selected: "high" },
);
assert.deepEqual(
  threePassEffortView({ supportedEfforts: ["low", "medium"], defaultEffort: "medium" }, "high"),
  { efforts: ["low", "medium"], selected: "medium" },
);
assert.equal(threePassProgressState({ status: "pass2", phase: "pass2", results: { pass1: "done" } }, "pass1"), "done");
assert.equal(threePassProgressState({ status: "pass2", phase: "pass2", results: { pass1: "done" } }, "pass2"), "running");
assert.equal(threePassProgressState({ status: "cancelled", phase: "pass3", results: {} }, "pass3"), "cancelled");
assert.equal(
  threePassAnswerMarkdown({
    phase: "pass3",
    results: { pass1: "定位主题", pass2: "连接证据" },
    preview: "检查假设",
  }),
  "# Three-Pass 结构化解读\n\n## Pass 1 · 快速定位\n\n定位主题\n\n## Pass 2 · 结构与证据\n\n连接证据\n\n## Pass 3 · 深读与批判\n\n检查假设",
);
assert.equal(
  compactRateLimitText({ rateLimits: [{ primary: { usedPercent: 37 }, secondary: { usedPercent: 82 } }] }),
  "63% 剩余 · 18% 剩余",
);
assert.equal(
  threePassLengthSummary({
    tolerancePercent: 20,
    phases: {
      pass1: { target: 250 },
      pass2: { target: 450 },
      pass3: { target: 1600 },
      synthesis: { target: 1800 },
    },
  }, "zh-CN"),
  "全局目标：P1 250 · P2 450 · P3 1600 · 综合 1800 字（约 ±20%）",
);
assert.match(
  threePassLengthSummary({ phases: { pass1:{ target:150 }, pass2:{ target:300 }, pass3:{ target:400 }, synthesis:{ target:600 } } }, "en-US"),
  /words/,
);

assert.equal(liveSourceFingerprint("  Same paragraph.  "), liveSourceFingerprint("Same paragraph."));
assert.notEqual(liveSourceFingerprint("First paragraph."), liveSourceFingerprint("Second paragraph."));

const pdfSpeechParagraphs = pdfParagraphGroups([
  { str: "First line", transform: [10, 0, 0, 10, 20, 100], width: 50, height: 10, hasEOL: true },
  { str: "continues.", transform: [10, 0, 0, 10, 20, 88], width: 60, height: 10, hasEOL: true },
  { str: "下一段", transform: [10, 0, 0, 10, 20, 60], width: 30, height: 10 },
  { str: "内容。", transform: [10, 0, 0, 10, 50, 60], width: 30, height: 10, hasEOL: true },
]);
assert.deepEqual(pdfSpeechParagraphs.map(item => item.text), [
  "First line continues.",
  "下一段内容。",
]);
for (const view of ["original", "translated", "interleaved", "sideBySide", "liveTranslation", "futureView"]) {
  assert.equal(readerSpeechAvailable({ sourceType: "pdf", view }), true);
}
assert.equal(readerSpeechAvailable({ sourceType: "markdown", view: "original" }), true);
assert.equal(readerSpeechAvailable({ sourceType: "html", view: "original" }), true);
assert.equal(readerSpeechAvailable({ sourceType: "docx", view: "original" }), false);
assert.equal(normalizeSpeechPlaybackRate(1.25), 1.25);
assert.equal(normalizeSpeechPlaybackRate(4), 2);
assert.equal(normalizeSpeechPlaybackRate("invalid"), 1);

assert.deepEqual(alignmentProgressView({}), { visible: false });
assert.deepEqual(
  alignmentProgressView({
    message: "Fallback",
    alignmentProgress: {
      stage: "aligning",
      label: "正在调用 LLM 对齐第 2/4 个文本区间。",
      completed: 1,
      total: 4,
      percent: 47.5,
    },
  }),
  {
    visible: true,
    percent: 47.5,
    label: "正在调用 LLM 对齐第 2/4 个文本区间。",
    count: "当前阶段 1/4",
    failed: false,
  },
);
assert.equal(
  alignmentProgressView({
    alignmentProgress: { stage: "failed", completed: 9, total: 2, percent: 120 },
  }).percent,
  100,
);

const rendered = answerMarkdownHtml([
  "# 结论",
  "",
  "这是 **粗体**、*斜体*、`代码` 与 $x^2$。",
  "",
  "1. 第一项",
  "2. 第二项",
  "",
  "> 引用内容",
  "",
  "| 参数 | 含义 |",
  "| --- | --- |",
  "| x | 输入 |",
  "",
  "```python",
  "print('<safe>')",
  "```",
  "",
  "<script>alert('unsafe')</script>",
].join("\n"));

assert.match(rendered, /<h3>结论<\/h3>/);
assert.match(rendered, /<strong>粗体<\/strong>/);
assert.match(rendered, /<em>斜体<\/em>/);
assert.match(rendered, /<ol><li/);
assert.match(rendered, /<blockquote>引用内容<\/blockquote>/);
assert.match(rendered, /<table>/);
assert.match(rendered, /<pre><code class="language-python">/);
assert.match(rendered, /&lt;safe&gt;/);
assert.match(rendered, /&lt;script&gt;/);
assert.doesNotMatch(rendered, /<script>/);

const superscript = answerMarkdownHtml("Reference ${ }^{1}$ and *emphasis*.");
assert.match(superscript, /class="math-source"/);
assert.match(superscript, /data-latex="\$\{ \}\^\{1\}\$"/);
assert.match(superscript, /<em>emphasis<\/em>/);
assert.doesNotMatch(superscript, /READER_FORMULA|@@READER|[\uE000-\uF8FF]/);

const notes = notesToMarkdown("Paper Title", [
  { kind: "selection", section: "Method", view: "translated", text: "中文译文" },
  { kind: "answer", section: "Method", action: "解释公式", question: "为什么成立？", selection: "x = 1", text: "**结论**：成立。" },
  { kind: "selection", section: "Results", view: "original", text: "Original result" },
]);
assert.match(notes, /^# Paper Title/m);
assert.match(notes, /^## Method/m);
assert.match(notes, /^### 译文摘录/m);
assert.match(notes, /^### 划词问答 · 解释公式/m);
assert.match(notes, /^#### 划词内容/m);
assert.match(notes, /^#### Prompt/m);
assert.match(notes, /- 动作：解释公式/);
assert.match(notes, /- 补充问题：为什么成立？/);
assert.match(notes, /^#### 返回结果/m);
assert.match(notes, /> x = 1/);
assert.match(notes, /^## Results/m);

assert.equal(normalizeFontFamily("", "Georgia"), "Georgia");
assert.equal(normalizeFontFamily("  Microsoft YaHei, SimSun  "), "Microsoft YaHei, SimSun");
assert.doesNotMatch(normalizeFontFamily("STIX; { color: red } <script>"), /[;{}<>]/);

assert.deepEqual(normalizeReadingLayout({
  fontSize: 22.26,
  letterSpacing: -9,
  lineHeight: 8,
  paragraphSpacing: 34,
  margins: {
    header: { top: 11, right: 22, bottom: 33, left: 44 },
    body: { top: -2, right: 999, bottom: "72", left: "bad" },
  },
}), {
  fontSize: 22.3,
  letterSpacing: -3,
  lineHeight: 3,
  paragraphSpacing: 34,
  margins: {
    header: { top: 11, right: 22, bottom: 33, left: 44 },
    body: { top: 0, right: 240, bottom: 72, left: 64 },
    footer: { top: 12, right: 48, bottom: 24, left: 48 },
  },
});

assert.equal(normalizeFormatColor("#A1B2C3"), "#a1b2c3");
assert.equal(normalizeFormatColor("red; background:url(x)"), "");
assert.equal(normalizeAssistantWidth(250), 280);
assert.equal(normalizeAssistantWidth(360.4), 360);
assert.equal(normalizeAssistantWidth(900), 420);
assert.equal(normalizeAssistantWidth(2800), 420);
assert.equal(normalizePaperWidth(320), 470);
assert.equal(normalizePaperWidth(920.6), 921);
assert.equal(normalizePaperWidth(1800), 1800);
assert.equal(normalizePaperWidth(2800), 2500);
assert.equal(normalizeReaderAxisOffset(-1300), -1200);
assert.equal(normalizeReaderAxisOffset(245.6), 246);
assert.equal(normalizeReaderAxisOffset(1800), 1200);
assert.equal(readerPaperColumnWidth(1576), 1000);
assert.equal(readerPaperColumnWidth(1576, 560), 900);
assert.equal(readerPaperColumnWidth(900), 470);
assert.deepEqual(readerLayoutForWidth(1600), { compactTableOfContents: false, assistantOverlay: false });
assert.deepEqual(readerLayoutForWidth(1200), { compactTableOfContents: true, assistantOverlay: false });
assert.deepEqual(readerLayoutForWidth(1120), { compactTableOfContents: true, assistantOverlay: false });
for (const width of [1100, 900, 760, 600, 420]) {
  assert.deepEqual(readerLayoutForWidth(width), { compactTableOfContents: true, assistantOverlay: true });
}
assert.equal(responsiveReaderWidth(1140, 1091), 1140);
assert.equal(responsiveReaderWidth(0, 1091), 1091);
assert.equal(pdfReflowNeeded(1090, 1105), false);
assert.equal(pdfReflowNeeded(1070, 1105), true);
assert.equal(pdfFitScale(800, 600), 4 / 3);
assert.ok(Math.abs(pdfFitScale(800, 600, 1.25) - (5 / 3)) < 1e-12);
assert.deepEqual(
  calculateNoteBadgeMetrics(17, 32.3, 20, true),
  { badgeFont: 7.5, badgeHeight: 10, lineHeight: 33 },
);
assert.deepEqual(
  calculateNoteBadgeMetrics(28, 36.4, 32, true),
  { badgeFont: 9, badgeHeight: 12, lineHeight: 47 },
);
assert.equal(calculateNoteBadgeMetrics(12, 18, 14, false).lineHeight, 18);
assert.equal(calculatePromptUnderlineOffset(0, false), 2);
assert.equal(calculatePromptUnderlineOffset(0, true), 5);
assert.equal(calculatePromptUnderlineOffset(2, true), 11);
const laneNotes = [
  { anchor: { view: "original", kind: "block", key: "p1", start: 0, end: 10 } },
  { anchor: { view: "original", kind: "block", key: "p1", start: 2, end: 5 } },
  { anchor: { view: "original", kind: "block", key: "p1", start: 5, end: 8 } },
  { anchor: { view: "original", kind: "block", key: "p1", start: 10, end: 12 } },
];
const laneAssignments = assignPromptUnderlineLanes(laneNotes);
assert.deepEqual(laneNotes.map(note => laneAssignments.get(note)), [0, 1, 1, 0]);
assert.equal(localAsset("./Paper%20assets/figure%20one.png"), "Paper%20assets/figure%20one.png");
assert.match(
  markdownInline("![Figure](<Paper assets/figure one.png>)"),
  /<img src="Paper%20assets\/figure%20one\.png" alt="Figure" loading="lazy">/,
);
assert.match(
  markdownInline("![[images/plot one.png|训练曲线]]"),
  /<img src="images\/plot%20one\.png" alt="训练曲线" loading="lazy">/,
);
assert.equal(textFormatHasStyle({ highlightColor: "#fff0a8" }), true);
assert.equal(textFormatHasStyle({ bold: false, textColor: "" }), false);
assert.equal(isShortTranslationSelection("diffusion"), true);
assert.equal(isShortTranslationSelection("U-Net"), true);
assert.equal(isShortTranslationSelection("模型"), true);
assert.equal(isShortTranslationSelection("two words"), false);
assert.equal(isShortTranslationSelection("x^2"), false);
assert.equal(isShortTranslationSelection("a".repeat(41)), false);
assert.deepEqual(
  interleavedBlockPairs(
    [{ id: "o1" }, { id: "o2" }, { id: "o3" }],
    [{ id: "t1" }, { id: "t2" }],
  ),
  [
    { index: 0, original: { id: "o1" }, translated: { id: "t1" } },
    { index: 1, original: { id: "o2" }, translated: { id: "t2" } },
    { index: 2, original: { id: "o3" }, translated: null },
  ],
);
assert.deepEqual(
  interleavedBlockPairs(
    [{ id: "o1" }, { id: "o2" }, { id: "o3" }],
    [
      { id: "a1", sourceIds: ["o1"], content: "译文一" },
      { id: "a2", sourceIds: ["o2", "o3"], content: "合并后的译文二" },
    ],
  ),
  [
    {
      index: 0,
      original: { id: "o1" },
      translated: { id: "a1", sourceIds: ["o1"], content: "译文一" },
      originals: [{ id: "o1" }],
      translations: [{ id: "a1", sourceIds: ["o1"], content: "译文一" }],
    },
    {
      index: 1,
      original: { id: "o2" },
      translated: { id: "a2", sourceIds: ["o2", "o3"], content: "合并后的译文二" },
      originals: [{ id: "o2" }, { id: "o3" }],
      translations: [{ id: "a2", sourceIds: ["o2", "o3"], content: "合并后的译文二" }],
    },
  ],
);
const originalHtmlBlocks = [
  { name: "original-1", dataset: { readerPairId: "pair-1" } },
  { name: "original-2", dataset: { readerPairId: "pair-2" } },
];
const translatedHtmlBlocks = [
  { name: "extra-translation", dataset: { readerPairId: "pair-extra" } },
  { name: "translated-2", dataset: { readerPairId: "pair-2" } },
  { name: "translated-1", dataset: { readerPairId: "pair-1" } },
];
assert.deepEqual(
  interleavedHtmlBlockPairs(originalHtmlBlocks, translatedHtmlBlocks),
  [
    { index: 0, original: originalHtmlBlocks[0], translated: translatedHtmlBlocks[2] },
    { index: 1, original: originalHtmlBlocks[1], translated: translatedHtmlBlocks[1] },
    { index: 2, original: null, translated: translatedHtmlBlocks[0] },
  ],
);
const groupedOriginalHtmlBlocks = [
  { name: "original-a", dataset: { readerPairId: "aligned-1" } },
  { name: "original-b", dataset: { readerPairId: "aligned-1" } },
];
const groupedTranslatedHtmlBlocks = [
  { name: "translated-a", dataset: { readerPairId: "aligned-1" } },
  { name: "translated-b", dataset: { readerPairId: "aligned-1" } },
  { name: "translated-c", dataset: { readerPairId: "aligned-1" } },
];
assert.deepEqual(
  interleavedHtmlBlockPairs(groupedOriginalHtmlBlocks, groupedTranslatedHtmlBlocks),
  [
    {
      index: 0,
      original: groupedOriginalHtmlBlocks[0],
      translated: groupedTranslatedHtmlBlocks[0],
      originals: groupedOriginalHtmlBlocks,
      translations: groupedTranslatedHtmlBlocks,
    },
  ],
);

function fakeTextNode(text) {
  return { nodeType: 3, textContent: text };
}

function fakeElement(tagName, childNodes = []) {
  const element = {
    nodeType: 1,
    tagName,
    childNodes,
    textContent: childNodes.map(node => node.textContent).join(""),
    innerHTML: "",
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  element.cloneNode = () => ({
    textContent: element.textContent,
    innerHTML: element.innerHTML,
    querySelectorAll: () => [],
  });
  return element;
}

const htmlDivFixture = fakeElement("BODY", [
  fakeElement("DIV", [
    fakeElement("DIV", [
      fakeElement("H2", [fakeTextNode("Preface")]),
      fakeElement("DIV", [fakeTextNode("First full paragraph.")]),
      fakeElement("DIV", [fakeTextNode("Second full paragraph.")]),
    ]),
  ]),
]);
const htmlDivBlocks = htmlInterleavableBlocks({
  body: htmlDivFixture,
  createElement: tagName => fakeElement(tagName.toUpperCase()),
});
assert.deepEqual(
  htmlDivBlocks.map(block => [block.tagName, block.textContent]),
  [
    ["H2", "Preface"],
    ["DIV", "First full paragraph."],
    ["DIV", "Second full paragraph."],
  ],
);

assert.equal(anchorsOverlap(
  { view: "original", kind: "block", key: "p1", start: 4, end: 10 },
  { view: "original", kind: "block", key: "p1", start: 8, end: 12 },
), true);
assert.equal(anchorsOverlap(
  { view: "original", kind: "block", key: "p1", start: 4, end: 10 },
  { view: "translated", kind: "block", key: "p1", start: 4, end: 10 },
), false);
assert.deepEqual(
  notesMatchingAnchor([
    { id: "matching", kind: "answer", anchor: { view: "original", kind: "block", key: "p1", start: 4, end: 10 } },
    { id: "selection-note", kind: "selection", anchor: { view: "original", kind: "block", key: "p1", start: 4, end: 10 } },
    { id: "other-view", kind: "answer", anchor: { view: "translated", kind: "block", key: "p1", start: 4, end: 10 } },
  ], { view: "original", kind: "block", key: "p1", start: 6, end: 8 }, "original").map(note => note.id),
  ["matching"],
);
