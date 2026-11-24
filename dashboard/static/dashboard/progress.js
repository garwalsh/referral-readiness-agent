// Poll the trace-backed endpoint. Keep existing events open when new ones arrive.
const timeline = document.querySelector("[data-progress-url]");
if (timeline) {
  const events = document.getElementById("timeline-events");
  const state = document.getElementById("run-state");
  const errorNotice = document.getElementById("progress-error");
  let rendered = Number(events.dataset.count);

  function element(tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  }

  function appendEvent(event) {
    const row = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.append(element("strong", event.label));
    for (const text of [event.tool, event.resource_type,
      event.resource_count == null ? null : event.resource_count + " returned", event.status]) {
      if (text) summary.append(element("span", text));
    }
    details.append(summary);
    if (event.timestamp) details.append(element("p", event.timestamp));
    if (event.duration_ms != null) details.append(element("p", "Duration " + event.duration_ms + " ms"));
    for (const [key, value] of Object.entries(event.parameters)) {
      details.append(element("p", key + " · " + value));
    }
    if (event.error) details.append(element("p", event.error));
    if (event.readiness) details.append(element("p", event.readiness));
    row.append(details);
    events.append(row);
  }

  async function poll() {
    try {
      const response = await fetch(timeline.dataset.progressUrl, {headers: {Accept: "application/json"}});
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error("Progress unavailable");
      const run = await response.json();
      if (run.terminal) {
        window.location.reload();
        return;
      }
      state.textContent = run.state;
      if (run.events.length && rendered === 0) events.replaceChildren();
      run.events.slice(rendered).forEach(appendEvent);
      rendered = run.events.length;
      errorNotice.hidden = true;
    } catch (error) {
      errorNotice.hidden = false;
    }
    window.setTimeout(poll, 2000);
  }

  window.setTimeout(poll, 2000);
}
