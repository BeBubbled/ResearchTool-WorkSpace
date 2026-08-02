(() => {
  "use strict";

  const SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis";
  const STAGES = {
    HKCategoryValueSleepAnalysisAsleepDeep: "deep",
    HKCategoryValueSleepAnalysisAsleepREM: "rem",
    HKCategoryValueSleepAnalysisAsleepCore: "core",
    HKCategoryValueSleepAnalysisAsleepUnspecified: "other",
    HKCategoryValueSleepAnalysisAsleep: "other",
    HKCategoryValueSleepAnalysisAwake: "awake",
    HKCategoryValueSleepAnalysisInBed: "inBed",
  };
  const STAGE_LABELS = { deep: "深睡", rem: "REM", core: "核心", other: "未细分" };
  const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const dom = {};
  const state = {
    records: [],
    allNights: [],
    visibleNights: [],
    source: "all",
    range: "30",
    rowsShown: 12,
    rawCount: 0,
    filename: "",
  };

  function cacheDom() {
    [
      "healthFile", "dropzone", "importProgress", "progressLabel", "progressCount", "progressBar",
      "importError", "importSection", "dashboard", "archiveSummary", "sourceFilter", "rangeFilter",
      "exportCsv", "importAnother", "avgSleep", "avgSleepNote", "goalBar", "avgBedtime",
      "bedtimeNote", "avgEfficiency", "efficiencyNote", "nightCount", "nightCountNote",
      "trendChart", "stageDonut", "stageTotal", "stageLegend", "stageCoverage", "insightStrip",
      "nightRows", "showMore",
    ].forEach(id => { dom[id] = document.getElementById(id); });
  }

  function bindEvents() {
    dom.healthFile.addEventListener("change", event => {
      const file = event.target.files?.[0];
      if (file) importHealthFile(file);
    });
    ["dragenter", "dragover"].forEach(type => dom.dropzone.addEventListener(type, event => {
      event.preventDefault();
      dom.dropzone.classList.add("dragover");
    }));
    ["dragleave", "drop"].forEach(type => dom.dropzone.addEventListener(type, event => {
      event.preventDefault();
      dom.dropzone.classList.remove("dragover");
    }));
    dom.dropzone.addEventListener("drop", event => {
      const file = event.dataTransfer?.files?.[0];
      if (file) importHealthFile(file);
    });
    dom.sourceFilter.addEventListener("change", () => {
      state.source = dom.sourceFilter.value;
      state.rowsShown = 12;
      rebuildNights();
    });
    dom.rangeFilter.addEventListener("change", () => {
      state.range = dom.rangeFilter.value;
      state.rowsShown = 12;
      renderDashboard();
    });
    dom.importAnother.addEventListener("click", resetImport);
    dom.exportCsv.addEventListener("click", exportCsv);
    dom.showMore.addEventListener("click", () => {
      state.rowsShown += 20;
      renderNightRows();
    });
  }

  function resetImport() {
    state.records = [];
    state.allNights = [];
    state.visibleNights = [];
    state.rowsShown = 12;
    dom.healthFile.value = "";
    dom.dashboard.classList.add("hidden");
    dom.importSection.classList.remove("hidden");
    dom.importError.classList.add("hidden");
    dom.importProgress.classList.add("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function importHealthFile(file) {
    const lower = file.name.toLowerCase();
    if (!lower.endsWith(".zip") && !lower.endsWith(".xml")) {
      showError("请选择 Apple 健康导出的 ZIP 或 XML 文件。");
      return;
    }

    state.filename = file.name;
    state.records = [];
    state.rawCount = 0;
    dom.importError.classList.add("hidden");
    dom.importProgress.classList.remove("hidden");
    setProgress("正在检查导出文件…", "", 7);

    try {
      let stream;
      let expectedBytes = file.size;
      if (lower.endsWith(".zip")) {
        setProgress("正在打开 Apple 健康导出包…", "", 12);
        const entry = await getHealthXmlFromZip(file);
        stream = entry.stream;
        expectedBytes = entry.uncompressedSize;
        setProgress(`已找到 ${entry.displayName}`, formatBytes(entry.uncompressedSize), 20, true);
      } else {
        stream = file.stream();
        setProgress("正在读取 export.xml…", formatBytes(file.size), 14);
      }

      await parseSleepRecords(stream, expectedBytes, lower.endsWith(".xml"));
      if (!state.records.length) {
        throw new Error("没有找到 Apple 健康睡眠记录。请确认选择的是完整的健康导出文件。");
      }

      setProgress("正在合并多设备记录…", `${formatNumber(state.records.length)} 条`, 90);
      await nextFrame();
      buildSourceFilter();
      rebuildNights();
      if (!state.allNights.length) throw new Error("记录存在，但没有可用的睡眠时段。");
      setProgress("整理完成", `${formatNumber(state.allNights.length)} 晚`, 100);
      await new Promise(resolve => setTimeout(resolve, 180));
      dom.importSection.classList.add("hidden");
      dom.dashboard.classList.remove("hidden");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      showError(error instanceof Error ? error.message : "无法读取这个健康导出文件。");
    }
  }

  function showError(message) {
    dom.importProgress.classList.add("hidden");
    dom.importError.textContent = message;
    dom.importError.classList.remove("hidden");
  }

  function setProgress(label, count = "", percent = 0, indeterminate = false) {
    dom.progressLabel.textContent = label;
    dom.progressCount.textContent = count;
    dom.progressBar.style.width = `${Math.max(2, Math.min(100, percent))}%`;
    dom.progressBar.classList.toggle("indeterminate", indeterminate);
  }

  async function getHealthXmlFromZip(file) {
    const buffer = await file.arrayBuffer();
    const view = new DataView(buffer);
    const eocd = findSignature(view, 0x06054b50, Math.max(0, view.byteLength - 65557), view.byteLength - 22);
    if (eocd < 0) throw new Error("ZIP 文件结构不完整。可以先解压，再选择其中的 export.xml。");
    const entryCount = view.getUint16(eocd + 10, true);
    const centralOffset = view.getUint32(eocd + 16, true);
    const decoder = new TextDecoder("utf-8");
    const entries = [];
    let cursor = centralOffset;

    for (let index = 0; index < entryCount && cursor + 46 <= view.byteLength; index += 1) {
      if (view.getUint32(cursor, true) !== 0x02014b50) break;
      const flags = view.getUint16(cursor + 8, true);
      const method = view.getUint16(cursor + 10, true);
      const compressedSize = view.getUint32(cursor + 20, true);
      const uncompressedSize = view.getUint32(cursor + 24, true);
      const nameLength = view.getUint16(cursor + 28, true);
      const extraLength = view.getUint16(cursor + 30, true);
      const commentLength = view.getUint16(cursor + 32, true);
      const localOffset = view.getUint32(cursor + 42, true);
      const nameBytes = new Uint8Array(buffer, cursor + 46, nameLength);
      const name = decoder.decode(nameBytes);
      if (name.toLowerCase().endsWith(".xml") && !name.toLowerCase().endsWith("export_cda.xml")) {
        entries.push({ name, flags, method, compressedSize, uncompressedSize, localOffset });
      }
      cursor += 46 + nameLength + extraLength + commentLength;
    }

    const entry = entries.sort((a, b) => b.uncompressedSize - a.uncompressedSize)[0];
    if (!entry) throw new Error("ZIP 中没有找到 Apple 健康 export.xml。");
    if (entry.flags & 1) throw new Error("这个 ZIP 使用了密码保护，请解压后选择 export.xml。");
    if (view.getUint32(entry.localOffset, true) !== 0x04034b50) throw new Error("ZIP 中的 XML 条目无法读取。");
    const localNameLength = view.getUint16(entry.localOffset + 26, true);
    const localExtraLength = view.getUint16(entry.localOffset + 28, true);
    const dataStart = entry.localOffset + 30 + localNameLength + localExtraLength;
    const compressed = new Uint8Array(buffer, dataStart, entry.compressedSize);
    let stream = new Blob([compressed]).stream();
    if (entry.method === 8) {
      if (typeof DecompressionStream === "undefined") {
        throw new Error("当前浏览器不能直接解压 ZIP。请先解压，再选择其中的 export.xml。");
      }
      stream = stream.pipeThrough(new DecompressionStream("deflate-raw"));
    } else if (entry.method !== 0) {
      throw new Error("ZIP 使用了暂不支持的压缩方式。请先解压，再选择 export.xml。");
    }
    return {
      stream,
      uncompressedSize: entry.uncompressedSize,
      displayName: entry.name.split(/[\\/]/).pop() || "export.xml",
    };
  }

  function findSignature(view, signature, min, start) {
    for (let offset = start; offset >= min; offset -= 1) {
      if (view.getUint32(offset, true) === signature) return offset;
    }
    return -1;
  }

  async function parseSleepRecords(stream, expectedBytes, showByteProgress) {
    const reader = stream.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let bytesRead = 0;
    let updates = 0;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytesRead += value.byteLength;
      buffer += decoder.decode(value, { stream: true });
      buffer = consumeXmlBuffer(buffer);
      updates += 1;
      if (updates % 12 === 0) {
        const percent = showByteProgress && expectedBytes ? 18 + (bytesRead / expectedBytes) * 67 : 42;
        setProgress(
          "正在提取睡眠记录…",
          `${formatNumber(state.records.length)} 条`,
          percent,
          !showByteProgress,
        );
        await nextFrame();
      }
    }
    buffer += decoder.decode();
    consumeXmlBuffer(buffer, true);
  }

  function consumeXmlBuffer(text, final = false) {
    let cursor = 0;
    while (true) {
      const start = text.indexOf("<Record", cursor);
      if (start < 0) {
        if (final) return "";
        return text.slice(Math.max(cursor, text.length - 32));
      }
      const end = text.indexOf(">", start + 7);
      if (end < 0) return text.slice(start);
      const tag = text.slice(start, end + 1);
      if (tag.includes(`type="${SLEEP_TYPE}"`)) parseRecordTag(tag);
      cursor = end + 1;
    }
  }

  function parseRecordTag(tag) {
    const attributes = {};
    const pattern = /([A-Za-z][\w:]*)="([^"]*)"/g;
    let match;
    while ((match = pattern.exec(tag))) attributes[match[1]] = decodeXml(match[2]);
    const stage = STAGES[attributes.value];
    if (!stage || !attributes.startDate || !attributes.endDate) return;
    const startInfo = parseAppleDate(attributes.startDate);
    const endInfo = parseAppleDate(attributes.endDate);
    if (!startInfo || !endInfo || endInfo.time <= startInfo.time) return;
    const duration = endInfo.time - startInfo.time;
    if (duration > 36 * 60 * 60 * 1000) return;
    state.records.push({
      stage,
      source: attributes.sourceName || "未知来源",
      start: startInfo.time,
      end: endInfo.time,
      night: nightKey(startInfo.parts),
    });
  }

  function decodeXml(value) {
    return value
      .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
      .replace(/&#(\d+);/g, (_, number) => String.fromCodePoint(Number(number)))
      .replace(/&quot;/g, "\"")
      .replace(/&apos;/g, "'")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
  }

  function parseAppleDate(value) {
    const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\s*([+-])(\d{2})(\d{2}))?/);
    if (!match) return null;
    const parts = {
      year: Number(match[1]), month: Number(match[2]), day: Number(match[3]),
      hour: Number(match[4]), minute: Number(match[5]), second: Number(match[6]),
    };
    const sign = match[7] === "-" ? -1 : 1;
    const offset = match[7] ? sign * (Number(match[8]) * 60 + Number(match[9])) : -new Date().getTimezoneOffset();
    const time = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second) - offset * 60000;
    return { time, parts };
  }

  function nightKey(parts) {
    const date = new Date(Date.UTC(parts.year, parts.month - 1, parts.day));
    if (parts.hour < 12) date.setUTCDate(date.getUTCDate() - 1);
    return [
      date.getUTCFullYear(),
      String(date.getUTCMonth() + 1).padStart(2, "0"),
      String(date.getUTCDate()).padStart(2, "0"),
    ].join("-");
  }

  function buildSourceFilter() {
    const sources = [...new Set(state.records.map(record => record.source))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    dom.sourceFilter.innerHTML = '<option value="all">全部来源</option>';
    sources.forEach(source => {
      const option = document.createElement("option");
      option.value = source;
      option.textContent = source;
      dom.sourceFilter.append(option);
    });
    state.source = "all";
  }

  function rebuildNights() {
    const records = state.source === "all" ? state.records : state.records.filter(record => record.source === state.source);
    const grouped = new Map();
    records.forEach(record => {
      if (!grouped.has(record.night)) grouped.set(record.night, []);
      grouped.get(record.night).push(record);
    });
    state.allNights = [...grouped.entries()]
      .map(([date, items]) => pickMainSession(date, items))
      .filter(Boolean)
      .sort((a, b) => b.date.localeCompare(a.date));
    renderDashboard();
  }

  function pickMainSession(date, records) {
    const sorted = [...records].sort((a, b) => a.start - b.start);
    const sessions = [];
    const gapLimit = 3 * 60 * 60 * 1000;
    sorted.forEach(record => {
      const current = sessions[sessions.length - 1];
      if (!current || record.start > current.end + gapLimit) {
        sessions.push({ end: record.end, records: [record] });
      } else {
        current.records.push(record);
        current.end = Math.max(current.end, record.end);
      }
    });
    const results = sessions.map(session => aggregateSession(date, session.records)).filter(Boolean);
    results.sort((a, b) => {
      if (a.estimated !== b.estimated) return a.estimated ? 1 : -1;
      return b.totalSleep - a.totalSleep;
    });
    return results[0] || null;
  }

  function aggregateSession(date, records) {
    const hasSleep = records.some(record => ["deep", "rem", "core", "other"].includes(record.stage));
    const basis = hasSleep ? records.filter(record => record.stage !== "inBed") : records.filter(record => record.stage === "inBed");
    if (!basis.length) return null;
    const points = [...new Set(basis.flatMap(record => [record.start, record.end]))].sort((a, b) => a - b);
    const totals = { deep: 0, rem: 0, core: 0, other: 0, awake: 0, inBed: 0 };
    const priority = ["deep", "rem", "core", "awake", "other", "inBed"];
    for (let index = 0; index < points.length - 1; index += 1) {
      const start = points[index];
      const end = points[index + 1];
      if (end <= start) continue;
      const middle = start + (end - start) / 2;
      const active = new Set(basis.filter(record => record.start < middle && record.end > middle).map(record => record.stage));
      const stage = priority.find(item => active.has(item));
      if (stage) totals[stage] += end - start;
    }
    const totalSleep = hasSleep ? totals.deep + totals.rem + totals.core + totals.other : totals.inBed;
    if (totalSleep < 5 * 60 * 1000) return null;
    const start = Math.min(...basis.map(record => record.start));
    const end = Math.max(...basis.map(record => record.end));
    const windowDuration = Math.max(totalSleep, end - start);
    const estimated = !hasSleep;
    return {
      date,
      start,
      end,
      totalSleep,
      estimated,
      efficiency: estimated ? null : Math.min(1, totalSleep / windowDuration),
      stages: totals,
      sources: [...new Set(records.map(record => record.source))].sort((a, b) => a.localeCompare(b, "zh-CN")),
      recordCount: records.length,
    };
  }

  function selectedNights() {
    if (state.range === "all") return state.allNights;
    return state.allNights.slice(0, Number(state.range));
  }

  function renderDashboard() {
    const nights = selectedNights();
    state.visibleNights = nights;
    const dateRange = nights.length ? `${formatDate(nights[nights.length - 1].date)} — ${formatDate(nights[0].date)}` : "无可用记录";
    dom.archiveSummary.textContent = `${dateRange} · ${formatNumber(state.records.length)} 条睡眠记录`;
    renderMetrics(nights);
    renderTrend(nights);
    renderStages(nights);
    renderInsights(nights);
    renderNightRows();
  }

  function renderMetrics(nights) {
    const avg = average(nights.map(night => night.totalSleep));
    const detailed = nights.filter(night => !night.estimated && night.efficiency !== null);
    const efficiency = average(detailed.map(night => night.efficiency));
    const bedtime = circularAverage(nights.map(night => localHour(night.start)), true);
    dom.avgSleep.textContent = nights.length ? formatDuration(avg) : "—";
    dom.avgSleepNote.textContent = nights.length ? `${avg >= 7 * 3600000 ? "达到" : "低于"} 7 小时参考线` : "等待数据";
    dom.goalBar.style.width = `${Math.min(100, (avg / (8 * 3600000)) * 100)}%`;
    dom.avgBedtime.textContent = nights.length ? formatHour(bedtime) : "—";
    dom.bedtimeNote.textContent = nights.length ? `波动约 ±${Math.round(bedtimeDeviation(nights))} 分钟` : "以主睡眠段开始计";
    dom.avgEfficiency.textContent = detailed.length ? `${Math.round(efficiency * 100)}%` : "—";
    dom.efficiencyNote.textContent = detailed.length ? `${detailed.length} 晚有分期可计算` : "旧记录仅能按在床估算";
    dom.nightCount.textContent = formatNumber(nights.length);
    dom.nightCountNote.textContent = `${formatNumber(nights.reduce((sum, night) => sum + night.recordCount, 0))} 条原始记录`;
  }

  function renderTrend(nights) {
    const recent = nights.slice(0, 14).reverse();
    dom.trendChart.innerHTML = "";
    recent.forEach(night => {
      const wrap = document.createElement("div");
      wrap.className = "trend-bar-wrap";
      const totalHeight = Math.max(2, Math.min(100, (night.totalSleep / (12 * 3600000)) * 100));
      const bar = document.createElement("div");
      bar.className = "trend-bar";
      bar.style.height = `${totalHeight}%`;
      bar.tabIndex = 0;
      if (night.estimated) {
        const segment = document.createElement("i");
        segment.className = "estimated";
        segment.style.height = "100%";
        bar.append(segment);
      } else {
        ["deep", "rem", "core", "other"].forEach(stage => {
          if (!night.stages[stage]) return;
          const segment = document.createElement("i");
          segment.className = stage;
          segment.style.height = `${(night.stages[stage] / night.totalSleep) * 100}%`;
          bar.append(segment);
        });
      }
      const tooltip = document.createElement("span");
      tooltip.className = "trend-tooltip";
      tooltip.textContent = `${formatDate(night.date)} · ${formatDuration(night.totalSleep)}${night.estimated ? "（估算）" : ""}`;
      const label = document.createElement("span");
      label.className = "trend-label";
      label.textContent = shortDate(night.date);
      wrap.append(bar, tooltip, label);
      dom.trendChart.append(wrap);
    });
    dom.trendChart.setAttribute("aria-label", recent.length ? `最近 ${recent.length} 晚平均睡眠 ${formatDuration(average(recent.map(n => n.totalSleep)))}` : "没有睡眠记录");
  }

  function renderStages(nights) {
    const detailed = nights.filter(night => !night.estimated);
    const totals = { deep: 0, rem: 0, core: 0, other: 0 };
    detailed.forEach(night => Object.keys(totals).forEach(stage => { totals[stage] += night.stages[stage]; }));
    const all = Object.values(totals).reduce((sum, value) => sum + value, 0);
    dom.stageLegend.innerHTML = "";
    if (!all) {
      dom.stageDonut.style.background = "conic-gradient(#e4e5ea 0 100%)";
      dom.stageTotal.textContent = "—";
      dom.stageCoverage.textContent = "当前范围没有可用的睡眠分期。";
      return;
    }
    let cursor = 0;
    const colors = { deep: "var(--deep)", rem: "var(--rem)", core: "var(--core)", other: "var(--other)" };
    const slices = [];
    ["deep", "rem", "core", "other"].forEach(stage => {
      const percent = (totals[stage] / all) * 100;
      slices.push(`${colors[stage]} ${cursor}% ${cursor + percent}%`);
      cursor += percent;
      const item = document.createElement("div");
      item.innerHTML = `<i style="background:${colors[stage]}"></i><span>${STAGE_LABELS[stage]}</span><b>${Math.round(percent)}%</b>`;
      dom.stageLegend.append(item);
    });
    dom.stageDonut.style.background = `conic-gradient(${slices.join(",")})`;
    dom.stageTotal.textContent = formatDuration(all / detailed.length);
    dom.stageCoverage.textContent = `${detailed.length} / ${nights.length} 晚包含可用睡眠分期。`;
    dom.stageDonut.setAttribute("aria-label", Object.keys(totals).map(stage => `${STAGE_LABELS[stage]} ${Math.round(totals[stage] / all * 100)}%`).join("，"));
  }

  function renderInsights(nights) {
    dom.insightStrip.innerHTML = "";
    if (!nights.length) return;
    const avg = average(nights.map(night => night.totalSleep));
    const deviation = bedtimeDeviation(nights);
    const recent = nights.slice(0, 7);
    const previous = nights.slice(7, 14);
    const delta = previous.length ? average(recent.map(n => n.totalSleep)) - average(previous.map(n => n.totalSleep)) : 0;
    const detailed = nights.filter(night => !night.estimated);
    const insights = [
      {
        icon: "◷",
        title: avg >= 7 * 3600000 ? "睡眠时长较充足" : "平均睡眠仍有缺口",
        body: avg >= 7 * 3600000
          ? `当前范围平均 ${formatDuration(avg)}，继续关注连续性与白天感受。`
          : `距 8 小时目标平均每晚还差 ${formatDuration(Math.max(0, 8 * 3600000 - avg))}。`,
      },
      {
        icon: "≈",
        title: deviation <= 45 ? "入睡节律较稳定" : "入睡时间波动偏大",
        body: `主睡眠开始时间的典型波动约为 ±${Math.round(deviation)} 分钟。`,
      },
      {
        icon: delta >= 0 ? "↗" : "↘",
        title: previous.length ? `近 7 晚${delta >= 0 ? "有所增加" : "有所减少"}` : "睡眠分期覆盖",
        body: previous.length
          ? `相比之前 7 晚，平均每晚${delta >= 0 ? "增加" : "减少"} ${formatDuration(Math.abs(delta))}。`
          : `${detailed.length} / ${nights.length} 晚包含手表或设备提供的睡眠分期。`,
      },
    ];
    insights.forEach(item => {
      const article = document.createElement("article");
      article.className = "insight";
      article.innerHTML = `<span class="insight-icon">${item.icon}</span><div><strong>${item.title}</strong><p>${item.body}</p></div>`;
      dom.insightStrip.append(article);
    });
  }

  function renderNightRows() {
    const nights = state.visibleNights;
    const shown = nights.slice(0, state.rowsShown);
    dom.nightRows.innerHTML = "";
    shown.forEach(night => {
      const row = document.createElement("tr");
      const totalStages = night.totalSleep || 1;
      const stageHtml = night.estimated
        ? '<span class="muted-value">无分期数据</span>'
        : `<span class="stage-line" aria-label="睡眠结构">${["deep", "rem", "core", "other"].map(stage => `<i class="${stage}" style="width:${night.stages[stage] / totalStages * 100}%"></i>`).join("")}</span>`;
      row.innerHTML = `
        <td><span class="night-date"><strong>${formatDate(night.date)}</strong><span>${weekday(night.date)}</span></span></td>
        <td><span class="window-time">${formatClock(night.start)} — ${formatClock(night.end)}</span></td>
        <td><span class="duration-cell"><strong>${formatDuration(night.totalSleep)}</strong>${night.estimated ? '<i class="estimate-tag">在床估算</i>' : ""}</span></td>
        <td>${stageHtml}</td>
        <td>${night.efficiency === null ? '<span class="muted-value">—</span>' : `${Math.round(night.efficiency * 100)}%`}</td>
        <td><span class="source-list">${night.sources.map(source => `<i class="source-tag">${escapeHtml(source)}</i>`).join("")}</span></td>`;
      dom.nightRows.append(row);
    });
    dom.showMore.classList.toggle("hidden", state.rowsShown >= nights.length);
    dom.showMore.textContent = `显示更多夜晚（剩余 ${Math.max(0, nights.length - state.rowsShown)}）`;
  }

  function exportCsv() {
    const headers = ["夜晚", "开始时间", "结束时间", "总睡眠_小时", "深睡_小时", "REM_小时", "核心_小时", "未细分_小时", "效率_百分比", "估算", "来源"];
    const rows = state.visibleNights.map(night => [
      night.date,
      isoLocal(night.start),
      isoLocal(night.end),
      hours(night.totalSleep),
      hours(night.stages.deep),
      hours(night.stages.rem),
      hours(night.stages.core),
      hours(night.stages.other),
      night.efficiency === null ? "" : Math.round(night.efficiency * 100),
      night.estimated ? "是" : "否",
      night.sources.join(" | "),
    ]);
    const csv = "\ufeff" + [headers, ...rows].map(row => row.map(csvCell).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `apple-sleep-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function csvCell(value) {
    const text = String(value ?? "");
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function average(values) {
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
  }

  function localHour(timestamp) {
    const date = new Date(timestamp);
    return date.getHours() + date.getMinutes() / 60;
  }

  function circularAverage(values, bedtime = false) {
    if (!values.length) return 0;
    const shifted = bedtime ? values.map(value => value < 12 ? value + 24 : value) : values;
    const avg = average(shifted);
    return avg >= 24 ? avg - 24 : avg;
  }

  function bedtimeDeviation(nights) {
    if (nights.length < 2) return 0;
    const values = nights.map(night => {
      const hour = localHour(night.start);
      return (hour < 12 ? hour + 24 : hour) * 60;
    });
    const mean = average(values);
    return Math.sqrt(average(values.map(value => (value - mean) ** 2)));
  }

  function formatDuration(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "—";
    const totalMinutes = Math.round(milliseconds / 60000);
    const hour = Math.floor(totalMinutes / 60);
    const minute = totalMinutes % 60;
    return minute ? `${hour}时 ${minute}分` : `${hour}小时`;
  }

  function formatHour(hour) {
    if (!Number.isFinite(hour)) return "—";
    const total = Math.round(hour * 60) % 1440;
    return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  }

  function formatClock(timestamp) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(timestamp));
  }

  function isoLocal(timestamp) {
    const date = new Date(timestamp);
    const pad = value => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function formatDate(key) {
    const [, month, day] = key.split("-");
    return `${Number(month)}月${Number(day)}日`;
  }

  function shortDate(key) {
    const [, month, day] = key.split("-");
    return `${Number(month)}/${Number(day)}`;
  }

  function weekday(key) {
    const [year, month, day] = key.split("-").map(Number);
    return WEEKDAYS[new Date(year, month - 1, day).getDay()];
  }

  function hours(milliseconds) {
    return (milliseconds / 3600000).toFixed(2);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN").format(value);
  }

  function formatBytes(bytes) {
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  function nextFrame() {
    return new Promise(resolve => requestAnimationFrame(() => resolve()));
  }

  cacheDom();
  bindEvents();
})();
