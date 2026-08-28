document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-copy]");
  if (!trigger) return;
  navigator.clipboard?.writeText(trigger.dataset.copy || "").then(() => {
    const old = trigger.textContent;
    trigger.textContent = "已复制";
    setTimeout(() => { trigger.textContent = old; }, 1000);
  });
});
