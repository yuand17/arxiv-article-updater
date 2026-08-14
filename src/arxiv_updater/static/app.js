(() => {
  const messages = {
    settings_saved: ["success", "设置已保存", "新的篇数从下一次三天精选生效。"],
    profile_started: ["info", "画像重建已启动", "完成后会用于后续推荐。"],
    author_added: ["success", "重点作者已添加", "已加入后续 Scholar 同步。"],
    author_removed: ["success", "重点作者已移除", "既有论文不会被立即删除。"],
    journal_added: ["success", "期刊已添加", "第一次同步已经启动。"],
    journal_removed: ["success", "期刊已移除", "既有论文将按保留规则处理。"],
    schedule_saved: ["success", "更新计划已保存", "下次运行时间已经重算。"],
    sync_started: ["info", "更新已启动", "可在活动记录中查看进度。"],
    service_saved: ["success", "外部服务设置已保存", "新的开关状态已立即生效。"],
    service_cleared: ["success", "API key 已清除", "服务已关闭，安全存储中的密钥已删除。"],
  };
  let sequence = 0;

  function removeToast(node) {
    if (node && node.parentNode) node.remove();
  }

  function showToast({ level = "info", title = "提示", message = "", persistent = false }) {
    const region = document.getElementById("toast-region");
    if (!region) return null;
    while (region.children.length >= 3) removeToast(region.firstElementChild);
    const toast = document.createElement("section");
    toast.className = `toast toast-${level}`;
    toast.dataset.toastId = String(++sequence);
    toast.setAttribute("role", level === "error" ? "alert" : "status");
    const icon = level === "success" ? "✓" : level === "error" ? "!" : "i";
    toast.innerHTML = `<span class="toast-icon" aria-hidden="true">${icon}</span><div><strong></strong><p></p></div><button type="button" aria-label="关闭通知">×</button>`;
    toast.querySelector("strong").textContent = title;
    toast.querySelector("p").textContent = message;
    toast.querySelector("button").addEventListener("click", () => removeToast(toast));
    region.appendChild(toast);
    const delay = level === "success" ? 6000 : level === "info" ? 10000 : 0;
    if (!persistent && delay) window.setTimeout(() => removeToast(toast), delay);
    return toast;
  }

  window.showAppToast = showToast;
  function updateServiceKeyFields(toggle) {
    const fieldId = toggle.getAttribute("aria-controls");
    const fields = fieldId ? document.getElementById(fieldId) : null;
    if (fields) fields.hidden = !toggle.checked;
  }

  document.addEventListener("change", (event) => {
    const toggle = event.target.closest("[data-service-toggle]");
    if (toggle) updateServiceKeyFields(toggle);
  });
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-confirm-message]");
    if (form && !window.confirm(form.dataset.confirmMessage)) event.preventDefault();
  });
  document.addEventListener("app:toast", (event) => showToast(event.detail || {}));
  document.addEventListener("app:sync-started", async (event) => {
    const { source, after } = event.detail || {};
    if (!source || !after) return;
    for (let attempt = 0; attempt < 180; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
      try {
        const url = `/settings/sync/${encodeURIComponent(source)}/status?after=${encodeURIComponent(after)}`;
        const response = await window.fetch(url, { headers: { Accept: "application/json" } });
        if (!response.ok) continue;
        const result = await response.json();
        if (result.status === "pending") continue;
        if (result.status === "success") {
          showToast({
            level: "success",
            title: "更新完成",
            message: result.message || `读取 ${result.items_seen} 条，新增 ${result.items_created} 篇。`,
          });
        } else {
          showToast({
            level: "error",
            title: "更新失败",
            message: result.message || "请在活动记录中查看详情。",
            persistent: true,
          });
        }
        return;
      } catch (_error) {
        // A transient local request failure should not interrupt the page.
      }
    }
    showToast({
      level: "error",
      title: "更新状态等待超时",
      message: "请在活动记录中查看同步是否完成。",
      persistent: true,
    });
  });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-service-toggle]").forEach(updateServiceKeyFields);
    const key = new URLSearchParams(window.location.search).get("toast");
    if (key && messages[key]) {
      const [level, title, message] = messages[key];
      showToast({ level, title, message });
      const url = new URL(window.location.href);
      url.searchParams.delete("toast");
      window.history.replaceState({}, "", url);
    }
  });
  document.addEventListener("htmx:beforeRequest", (event) => {
    const form = event.detail.elt.closest("[data-loading-toast]");
    if (!form) return;
    const button = form.querySelector("button[type='submit'], button:not([type])");
    if (button) {
      button.dataset.originalText = button.textContent;
      button.textContent = "查找中…";
      button.disabled = true;
    }
    form._loadingToast = showToast({
      level: "info",
      title: "正在查找期刊来源",
      message: form.dataset.loadingToast,
      persistent: true,
    });
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    const form = event.detail.elt.closest("[data-loading-toast]");
    if (!form) return;
    if (form._loadingToast) removeToast(form._loadingToast);
    const button = form.querySelector("button[type='submit'], button:not([type])");
    if (button) {
      button.textContent = button.dataset.originalText || "查找期刊";
      button.disabled = false;
    }
  });
})();
