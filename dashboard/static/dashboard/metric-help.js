"use strict";

const metrics = document.querySelector(".evaluation-metrics");
let openHelp;

function closeHelp() {
  if (!openHelp) return;
  openHelp.querySelector('[role="tooltip"]').hidden = true;
  openHelp = null;
}

function showHelp(help) {
  closeHelp();
  help.querySelector('[role="tooltip"]').hidden = false;
  openHelp = help;
}

for (const help of metrics.querySelectorAll(".metric-help")) {
  const button = help.querySelector("button");
  help.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "mouse") showHelp(help);
  });
  help.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse" && openHelp === help && !button.matches(":focus-visible")) closeHelp();
  });
  button.addEventListener("focus", () => showHelp(help));
  button.addEventListener("blur", () => {
    if (openHelp === help) closeHelp();
  });
  button.addEventListener("click", () => showHelp(help));
}

document.addEventListener("click", (event) => {
  if (openHelp && !openHelp.contains(event.target)) closeHelp();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeHelp();
});
