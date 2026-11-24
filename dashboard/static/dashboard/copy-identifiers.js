const feedback = document.getElementById("copy-feedback");

for (const button of document.querySelectorAll("[data-copy-id]")) {
  if (!navigator.clipboard) continue;
  const identifier = document.getElementById(button.dataset.copyId);
  if (!identifier) continue;
  button.hidden = false;
  button.addEventListener("click", async () => {
    const label = button.dataset.copyLabel;
    try {
      await navigator.clipboard.writeText(identifier.textContent);
      feedback.textContent = label + " copied.";
    } catch {
      feedback.textContent = "Copy unavailable. Select the " + label.toLowerCase() + " to copy it.";
    }
  });
}
