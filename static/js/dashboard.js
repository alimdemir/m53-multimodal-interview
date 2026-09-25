document.querySelectorAll(".copy-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Kopyalandı ✓";
    } catch (_) {
      const field = document.createElement("textarea");
      field.value = button.dataset.copy;
      document.body.appendChild(field);
      field.select();
      document.execCommand("copy");
      field.remove();
      button.textContent = "Kopyalandı ✓";
    }
    setTimeout(() => { button.textContent = original; }, 1800);
  });
});

setTimeout(() => {
  document.querySelectorAll(".toast").forEach((toast) => toast.remove());
}, 4500);
