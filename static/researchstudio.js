"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const active = new Set(["queued", "running", "cancelling"]);
  const labels = {queued:"排队中",running:"生成中",cancelling:"正在停止",cancelled:"已取消",completed:"产物已生成",incomplete:"产物不完整",failed:"执行失败",interrupted:"服务重启中断"};
  const descriptions = {
    "idea-spark":"从研究问题到有文献支撑的研究方案，包含方法、可证伪计划和多轮审查。",
    "paper-search":"跨 arXiv、DBLP、OpenAlex、OpenReview、Semantic Scholar、Crossref 检索文献。",
    "scoop-check":"填写研究问题和具体创新点，检查与已有研究的重合之处。",
    paper2assets:"提取共享论文素材：文字、图表、元数据、结构化证据与讲解脚本。",
    paper2poster:"生成 HTML、PDF、PNG 海报及可编辑的 PowerPoint。",
    paper2blog:"生成共享论文证据与图表的中文和英文 Word 博客。",
    paper2video:"生成可编辑演示文稿及带字幕的讲解视频。",
    paper2reel:"依次完成素材、海报、双语博客和视频，汇总为交互展示页。"
  };
  let config, selected = "", pollTimer, submitting = false;
  async function api(url, options = {}) {
    const response = await fetch(url, {...options, headers:{"X-ResearchStudio":"1", ...options.headers}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  }
  function option(value, title) {
    const el = document.createElement("option"); el.value = value; el.textContent = title; return el;
  }
  function moduleChanged() {
    const item = config?.modules.find(x => x.id === $("module").value);
    if (!item) return;
    $("description").textContent = descriptions[item.id];
    $("query").required = item.bundle === "Idea";
    $("queryOptional").textContent = item.bundle === "Idea" ? "（必填）" : "（可选）";
    $("paperRequirement").textContent = item.bundle === "Reel" ? "（必填）" : "（可选种子论文）";
    $("availability").textContent = item.ready ? "基础依赖已就绪，使用 AI 设置中的 Codex 默认模型与推理强度。" : `暂不可运行，缺少：${item.missing.join("、")}。请展开下方安装说明。`;
    $("availability").classList.toggle("missing", !item.ready);
    $("submit").disabled = !item.ready || submitting;
  }
  async function loadConfig(refresh = false) {
    config = await api(`/api/researchstudio/config${refresh ? "?refresh=1" : ""}`);
    const prior = $("module").value || new URLSearchParams(location.search).get("module") || "idea-spark";
    $("module").replaceChildren();
    for (const bundle of ["Idea", "Reel"]) {
      const group = document.createElement("optgroup"); group.label = bundle === "Idea" ? "Idea · 研究发现" : "Reel · 论文成果";
      config.modules.filter(x => x.bundle === bundle).forEach(x => group.append(option(x.id, x.label + (x.ready ? "" : " · 待安装依赖"))));
      $("module").append(group);
    }
    $("module").value = config.modules.some(x => x.id === prior) ? prior : "idea-spark";
    for (const [id, items, label] of [["paperId",config.papers,"不选择"],["projectId",config.projects,"不附加项目"]]) {
      const old = $(id).value;
      $(id).replaceChildren(option("",label));
      items.forEach(x => $(id).append(option(x.id,x.title || x.name)));
      if (items.some(x => x.id === old)) $(id).value = old;
    }
    moduleChanged();
  }
  async function loadJobs() {
    const data = await api("/api/researchstudio/jobs");
    $("jobSelect").replaceChildren();
    $("jobCount").textContent = `${data.jobs.length} 条记录`;
    if (!data.jobs.length) { $("jobSelect").append(option("","尚无任务")); return; }
    data.jobs.forEach(x => $("jobSelect").append(option(x.id,`${x.label} · ${new Date(x.createdAt * 1000).toLocaleString()} · ${labels[x.status] || x.status}`)));
    if (!data.jobs.some(x => x.id === selected)) selected = data.jobs[0].id;
    $("jobSelect").value = selected;
  }
  async function showJob() {
    clearTimeout(pollTimer);
    if (!selected) return;
    const id = selected;
    try {
      const job = await api(`/api/researchstudio/jobs/${id}`);
      if (id !== selected) return;
      $("empty").hidden = true; $("jobDetail").hidden = false;
      $("jobTitle").textContent = job.label;
      $("jobStatus").textContent = labels[job.status] || job.status;
      $("jobModel").textContent = `${job.model} · ${job.effort}`;
      $("jobError").textContent = job.error || "";
      $("jobLog").textContent = job.log.join("\n");
      $("cancel").hidden = !active.has(job.status);
      $("cancel").disabled = job.status === "cancelling";
      $("bundle").hidden = active.has(job.status) || !job.artifacts.length;
      $("bundle").href = `/api/researchstudio/jobs/${id}/bundle`;
      $("artifacts").replaceChildren();
      job.artifacts.forEach(file => {
        const li = document.createElement("li"), link = document.createElement("a"), size = document.createElement("span");
        link.textContent = file.path.replace(/^output\//, "");
        link.href = `/api/researchstudio/jobs/${id}/files/${file.path.split("/").map(encodeURIComponent).join("/")}`;
        size.textContent = file.size >= 1048576 ? `${(file.size / 1048576).toFixed(1)} MB` : `${Math.ceil(file.size / 1024)} KB`;
        li.append(link,size); $("artifacts").append(li);
      });
      if (active.has(job.status)) pollTimer = setTimeout(showJob,2500);
      else await loadJobs();
    } catch (error) {
      $("jobError").textContent = error.message;
      pollTimer = setTimeout(showJob,8000);
    }
  }
  $("module").addEventListener("change",moduleChanged);
  $("paper").addEventListener("change",() => { if ($("paper").files.length) $("paperId").value = ""; });
  $("paperId").addEventListener("change",() => { if ($("paperId").value) $("paper").value = ""; });
  $("jobSelect").addEventListener("change",() => { selected = $("jobSelect").value; showJob(); });
  $("refresh").addEventListener("click",async () => {
    $("refresh").disabled = true;
    try { await loadConfig(true); $("formError").textContent = ""; }
    catch (error) { $("formError").textContent = error.message; }
    finally { $("refresh").disabled = false; }
  });
  $("studioForm").addEventListener("submit",async event => {
    event.preventDefault();
    if (submitting) return;
    $("formError").textContent = "";
    const item = config.modules.find(x => x.id === $("module").value);
    if (item.bundle === "Reel" && !$("paper").files.length && !$("paperId").value) {
      $("formError").textContent = "请选择论文库中的 PDF 或上传 PDF。"; return;
    }
    submitting = true; moduleChanged();
    const data = new FormData($("studioForm"));
    data.set("confirmUsage",$("confirmUsage").checked ? "true" : "false");
    if (!$("paper").files.length) data.delete("paper");
    try {
      const job = await api("/api/researchstudio/jobs",{method:"POST",body:data});
      selected = job.id; await loadJobs(); await showJob();
    } catch (error) { $("formError").textContent = error.message; }
    finally { submitting = false; moduleChanged(); }
  });
  $("cancel").addEventListener("click",async () => {
    $("cancel").disabled = true;
    try { await api(`/api/researchstudio/jobs/${selected}/cancel`,{method:"POST"}); await showJob(); }
    catch (error) { $("jobError").textContent = error.message; $("cancel").disabled = false; }
  });
  async function start() {
    const results = await Promise.allSettled([loadConfig(),loadJobs()]);
    const failed = results.find(x => x.status === "rejected");
    if (failed) $("formError").textContent = failed.reason.message;
    await showJob();
  }
  start();
})();
