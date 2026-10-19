"use strict";

window.ReviewdeskUI.execution = (job) => {
  const {node} = window.ReviewdeskUI;
  const details = node("details", "execution-details");
  details.dataset.executionJob = job.id;
  details.append(node("summary", "", "How this run worked"));
  const trace = job.execution;
  if (!trace?.events?.length) {
    details.append(node("p", "field-help", "Step timings were not recorded for this older run."));
    return details;
  }
  const labels = {queued: "Waiting for the worker", starting: "Opening the saved workspace", researching: "Selecting local review evidence", analysing: "Preparing and checking the analysis", collecting: "Collecting and saving review pages", signing_in: "Connecting the Amazon session", awaiting_login: "Waiting for manual sign-in", finished: "Run finished"};
  const list = node("ol", "execution-list");
  for (const event of trace.events) {
    const item = node("li"), title = node("strong", "", labels[event.phase] || event.phase.replaceAll("_", " "));
    const elapsed = event.elapsed_seconds == null ? ["queued", "running", "cancelling"].includes(job.status) ? "In progress" : "Not recorded" : `${event.elapsed_seconds.toFixed(3)}s`;
    item.append(title, node("span", "execution-duration", elapsed));
    const counters = Object.entries(event.counters || {}).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`);
    if (counters.length) item.append(node("p", "field-help", counters.join(" · ")));
    list.append(item);
  }
  details.append(list);
  if (job.status === "interrupted") details.append(node("p", "field-help", "A restart detected an unfinished job. The final active step's duration is unknown."));
  const result = job.result || {};
  if (result.cache_hit || result.cached) details.append(node("p", "field-help", "Reused saved work. No new model request was needed for the cached result."));
  if (result.notice) details.append(node("p", "field-help", "The AI answer was not accepted. The result contains labelled local evidence."));
  details.append(node("p", "field-help", "Timings include local processing and waiting. Source checks establish where a quote came from; they do not prove the interpretation."));
  if (trace.omitted_events) details.append(node("p", "field-help", `${trace.omitted_events} intermediate transitions were omitted to keep this record bounded.`));
  return details;
};
