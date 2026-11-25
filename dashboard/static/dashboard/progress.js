// Trace events are append-only. Leave expanded details open as new events arrive.
const timeline = document.querySelector("[data-progress-url]");
if (timeline) {
  const events = document.getElementById("timeline-events");
  const report = timeline.closest(".report-page");
  const stage = document.getElementById("readiness-title");
  const stageDetail = document.getElementById("execution-detail");
  const state = document.getElementById("run-state");
  const elapsed = document.getElementById("execution-elapsed");
  const errorNotice = document.getElementById("progress-error");
  let rendered = Number(events.dataset.count);
  let elapsedMs = elapsed.dataset.elapsedMs === "" ? null : Number(elapsed.dataset.elapsedMs);
  let clockAnchor = performance.now();

  function text(element, value) {
    if (element.textContent !== value) element.textContent = value;
  }

  function showElapsed() {
    if (elapsedMs === null) return;
    const seconds = Math.floor((elapsedMs + performance.now() - clockAnchor) / 1000);
    text(elapsed, Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0"));
  }

  function element(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }

  function appendEvent(event) {
    const row = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.append(element("strong", event.label));
    for (const value of [event.tool, event.resource_type,
      event.resource_count == null ? null : event.resource_count + " returned", event.status]) {
      if (value) summary.append(element("span", value));
    }
    details.append(summary);
    if (event.timestamp) details.append(element("p", event.timestamp));
    if (event.duration_ms != null) details.append(element("p", "Duration " + event.duration_ms + " ms"));
    for (const [key, value] of Object.entries(event.parameters)) {
      const parameter = element("p", key + " · " + value);
      parameter.className = "identifier";
      details.append(parameter);
    }
    if (event.error) details.append(element("p", event.error));
    if (event.readiness) details.append(element("p", event.readiness));
    row.append(details);
    events.append(row);
  }

  function showActivity(run) {
    report.dataset.executionMode = run.activity.mode;
    text(state, run.activity.mode === "waiting" ? "Waiting" : run.state);
    text(stage, run.activity.label);
    text(stageDetail, run.activity.detail);
    stageDetail.hidden = !run.activity.detail;
  }

  const clock = window.setInterval(showElapsed, 250);
  showElapsed();

  async function poll() {
    const request = new AbortController();
    const deadline = window.setTimeout(() => request.abort(), 8000);
    try {
      const response = await fetch(timeline.dataset.progressUrl, {
        headers: { Accept: "application/json" },
        signal: request.signal,
      });
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error("Progress unavailable");
      const run = await response.json();
      showActivity(run);
      if (run.terminal) {
        window.clearInterval(clock);
        window.location.reload();
        return;
      }
      if (run.elapsed_ms !== null) {
        const localEstimate = elapsedMs === null ? 0 : elapsedMs + performance.now() - clockAnchor;
        // Keep the visible counter monotonic when network round-trip time varies.
        elapsedMs = Math.max(run.elapsed_ms, localEstimate);
        clockAnchor = performance.now();
        showElapsed();
      }
      if (run.events.length > rendered) {
        if (rendered === 0) events.replaceChildren();
        events.querySelector(".latest-event")?.classList.remove("latest-event");
        run.events.slice(rendered).forEach(appendEvent);
        events.lastElementChild?.classList.add("latest-event");
        rendered = run.events.length;
      }
      errorNotice.hidden = true;
    } catch {
      report.dataset.executionMode = "interrupted";
      errorNotice.hidden = false;
    } finally {
      window.clearTimeout(deadline);
    }
    window.setTimeout(poll, 1000);
  }

  poll();
}
