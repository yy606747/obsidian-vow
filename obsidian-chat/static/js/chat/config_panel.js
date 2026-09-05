// ── 配置弹窗 ──
let configPanelControlsBound = false;

function toggleConfig(e) {
  if (e) {
    e.preventDefault();
    e.stopPropagation();
  }
  const popup = $("configPopup");
  const willShow = !popup.classList.contains("show");
  popup.classList.toggle("show");
  if (willShow && typeof ensureTTSVoicesLoaded === "function") ensureTTSVoicesLoaded();
}

document.addEventListener("click", e => {
  const p = $("configPopup");
  if (p.classList.contains("show") && !p.contains(e.target)) p.classList.remove("show");
});

function setContextLimit(value) {
  $("contextValue").textContent = value + "条";
  localStorage.setItem("obsidian_context_limit", value);
}

function setTemperature(value) {
  $("tempValue").textContent = value;
  localStorage.setItem("obsidian_temperature", value);
}

function restoreConfigPanelValues() {
  const savedCtx = localStorage.getItem("obsidian_context_limit");
  if (savedCtx) {
    $("contextSlider").value = savedCtx;
    $("contextValue").textContent = savedCtx + "条";
  }
  const savedTemp = localStorage.getItem("obsidian_temperature");
  if (savedTemp) {
    $("tempSlider").value = savedTemp;
    $("tempValue").textContent = savedTemp;
  }
}

function bindConfigPanelControls() {
  if (configPanelControlsBound) return;
  configPanelControlsBound = true;

  const configBtn = document.querySelector('[data-action="toggleConfig"]');
  if (configBtn) configBtn.addEventListener("click", toggleConfig);

  const bind = (id, type, handler) => {
    const el = $(id);
    if (el) el.addEventListener(type, handler);
  };

  bind("modelSelect", "change", () => changeModel());
  bind("contextSlider", "input", event => setContextLimit(event.target.value));
  bind("tempSlider", "input", event => {
    setTemperature(event.target.value);
    syncTemperature();
  });
}

// 同步温度到后端 settings
async function syncTemperature() {
  const t = parseFloat($("tempSlider").value);
  await api("PUT", "/api/settings/temperature", { temperature: t });
}

restoreConfigPanelValues();
bindConfigPanelControls();

ChatApp.registerModule("configPanel", {
  toggle: toggleConfig,
  restoreValues: restoreConfigPanelValues,
  bindControls: bindConfigPanelControls,
  setContextLimit,
  setTemperature,
  syncTemperature,
});
