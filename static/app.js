const dropzone = document.querySelector("#dropzone");
const fileInput = document.querySelector("#fileInput");
const statusEl = document.querySelector("#status");
const messageEl = document.querySelector("#message");
const frontSheetField = document.querySelector("#frontSheetField");
const frontSheetSelect = document.querySelector("#frontSheetSelect");
const backSheetField = document.querySelector("#backSheetField");
const backSheetSelect = document.querySelector("#backSheetSelect");
const frontSelect = document.querySelector("#frontSelect");
const backSelect = document.querySelector("#backSelect");
const controls = document.querySelector("#controls");
const generateButton = document.querySelector("#generateButton");

let currentToken = null;
let hasSheets = false;

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.classList.toggle("error", isError);
}

function setStatus(text) {
  statusEl.textContent = text;
}

function fillSelect(select, values, preferredIndex = 0) {
  select.innerHTML = "";
  values.forEach((value, index) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    if (index === preferredIndex) {
      option.selected = true;
    }
    select.appendChild(option);
  });
}

function updateGenerateAvailability() {
  generateButton.disabled = !(
    currentToken &&
    frontSelect.value &&
    backSelect.value
  );
}

function setColumnSelect(select, columns, preferredIndex = 0) {
  fillSelect(select, columns, preferredIndex);
  updateGenerateAvailability();
}

function setColumns(columns) {
  setColumnSelect(frontSelect, columns, 0);
  setColumnSelect(backSelect, columns, Math.min(1, columns.length - 1));
}

function setSheetFieldsVisible(isVisible) {
  hasSheets = isVisible;
  frontSheetField.classList.toggle("hidden", !isVisible);
  backSheetField.classList.toggle("hidden", !isVisible);
  if (!isVisible) {
    frontSheetSelect.innerHTML = "";
    backSheetSelect.innerHTML = "";
  }
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function uploadFile(file) {
  if (!file) return;

  const formData = new FormData();
  formData.append("file", file);
  setStatus("读取中");
  setMessage("");
  generateButton.disabled = true;

  try {
    const data = await requestJson("/api/inspect", {
      method: "POST",
      body: formData,
    });

    currentToken = data.token;
    if (data.sheets.length) {
      setSheetFieldsVisible(true);
      fillSelect(frontSheetSelect, data.sheets, 0);
      fillSelect(backSheetSelect, data.sheets, 0);
    } else {
      setSheetFieldsVisible(false);
    }

    setColumns(data.columns);
    setStatus("已载入");
    setMessage(`${data.filename} 已载入，选择正面和背面来源后生成。`);
  } catch (error) {
    currentToken = null;
    setSheetFieldsVisible(false);
    frontSelect.innerHTML = "";
    backSelect.innerHTML = "";
    setStatus("读取失败");
    setMessage(error.message, true);
    updateGenerateAvailability();
  }
}

async function loadSheetColumns(side) {
  if (!currentToken || !hasSheets) return;

  const sheetSelect = side === "front" ? frontSheetSelect : backSheetSelect;
  const columnSelect = side === "front" ? frontSelect : backSelect;
  const preferredIndex = side === "front" ? 0 : 1;
  if (!sheetSelect.value) return;

  setStatus("读取列");
  setMessage("");
  generateButton.disabled = true;

  try {
    const data = await requestJson("/api/columns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: currentToken,
        sheet: sheetSelect.value,
      }),
    });
    setColumnSelect(columnSelect, data.columns, Math.min(preferredIndex, data.columns.length - 1));
    setStatus("已载入");
  } catch (error) {
    columnSelect.innerHTML = "";
    setStatus("读取失败");
    setMessage(error.message, true);
    updateGenerateAvailability();
  }
}

async function generateCards(event) {
  event.preventDefault();
  if (!currentToken) return;

  setStatus("生成中");
  setMessage("");
  generateButton.disabled = true;

  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: currentToken,
        sheet: hasSheets ? frontSheetSelect.value : null,
        frontSheet: hasSheets ? frontSheetSelect.value : null,
        backSheet: hasSheets ? backSheetSelect.value : null,
        front: frontSelect.value,
        back: backSelect.value,
      }),
    });

    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || "生成失败");
    }

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : "anki_cards.txt";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus("已生成");
    setMessage("Anki TXT 已生成并下载。");
  } catch (error) {
    setStatus("生成失败");
    setMessage(error.message, true);
  } finally {
    updateGenerateAvailability();
  }
}

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("dragover");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("dragover");
});

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragover");
  uploadFile(event.dataTransfer.files[0]);
});

fileInput.addEventListener("change", () => {
  uploadFile(fileInput.files[0]);
});

frontSheetSelect.addEventListener("change", () => loadSheetColumns("front"));
backSheetSelect.addEventListener("change", () => loadSheetColumns("back"));
frontSelect.addEventListener("change", updateGenerateAvailability);
backSelect.addEventListener("change", updateGenerateAvailability);
controls.addEventListener("submit", generateCards);
