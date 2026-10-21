"use strict";

window.ReviewdeskUI.createComparison = ({$, activeStatuses, average, briefSources, button, createJob, empty, exploreProduct, jobAction, messageAt, mutateJob, navigate, node, number, plural, postJson, preserveJobFocus, productName, request, routerStatus, state, toast, usageSummary}) => {
  function updateComparisonControls() {
    const active = state.automation?.active_job, busy = Boolean(active && activeStatuses.has(active.status));
    const owned = busy && active.kind === "comparison" && active.options.dataset === state.dataset;
    for (const input of $("compare-form").querySelectorAll("input, select")) input.disabled = owned;
    $("compare-submit").disabled = !state.automation || state.mutation || busy || state.comparePreviewBusy;
    $("compare-submit").firstChild.textContent = state.comparePreview?.collection_planned ? "Collect and compare" : "Compare products";
    $("compare-example").disabled = owned || !state.overview || state.overview.products.length < 2;
    for (const control of document.querySelectorAll("[data-compare-focus]")) control.disabled = owned;
    $("compare-consent").disabled = owned || !routerStatus().configured;
    $("compare-collect").disabled = owned || state.dataset === "demo";
    if (state.dataset === "demo") $("compare-collect").checked = false;
    $("compare-pages").disabled = owned || !$("compare-collect").checked;
    const policy = state.provider?.require_zdr ? "Zero retention is required." : "Zero retention is off; enabled provider or account logging may retain shared text.";
    const quota = state.providerQuota, allowance = quota?.quota?.available && Date.now() < Number(quota.resets_at) * 1000 ? ` Last checked: ${number(quota.quota.remaining)} / ${number(quota.quota.limit)} account requests left. Refresh in AI settings.` : " Check remaining requests in AI settings.";
    const cooldown = state.provider?.retry_at && Date.now() < state.provider.retry_at * 1000 ? ` Free AI paused until ${new Date(state.provider.retry_at * 1000).toLocaleTimeString()}.` : "";
    $("compare-ai-status").textContent = state.showcase ? "This comparison uses local evidence. External AI is disabled in the showcase." : routerStatus().configured ? `At most one free AI attempt, with no automatic retry. ${policy}${allowance}${cooldown}` : "Local comparison works without a key. Connect OpenRouter in AI settings for optional interpretation.";
  }
  function comparisonChanged() {
    state.compareDirty = true; state.compareJobId = null; state.compareRenderKey = "";
    $("compare-consent").checked = false; messageAt("compare-error", ""); renderComparison(); scheduleComparisonPreview();
  }
  function comparisonPayload(external = false) {
    return {kind: "comparison", dataset: state.dataset, products: [$("compare-first").value.trim(), $("compare-second").value.trim()],
      collect_missing: $("compare-collect").checked && state.dataset === "workspace", max_pages: Number($("compare-pages").value),
      target_reviews: Number($("compare-target").value), focus: $("compare-focus").value.trim(), allow_external: external};
  }
  function scheduleComparisonPreview() {
    clearTimeout(state.comparePreviewTimer); const sequence = ++state.comparePreviewSeq;
    state.comparePreview = null; state.comparePreviewBusy = false;
    const payload = comparisonPayload();
    if (payload.products.some((value) => !value)) {
      $("compare-preview").replaceChildren(node("p", "field-help", "Paste both links to check product names, saved reviews and the plan. No collection or AI request starts here.")); updateComparisonControls(); return;
    }
    state.comparePreviewBusy = true; $("compare-preview").replaceChildren(node("p", "field-help", "Checking links and saved reviews...")); updateComparisonControls();
    state.comparePreviewTimer = setTimeout(async () => {
      try {
        const preview = await postJson("/api/comparison/preview", payload);
        if (sequence !== state.comparePreviewSeq) return;
        state.comparePreview = preview; const panel = $("compare-preview"); panel.replaceChildren(node("h3", "", "Before you start"));
        for (const product of preview.products) {
          const row = node("div", "compare-preview-product");
          row.append(node("strong", "", product.name || `New product ${product.asin}`), node("span", "product-id", product.asin),
            node("p", "field-help", `${plural(product.stored_reviews, "saved review")}. ${product.available_pages_ended ? "Available review pages ended; using the saved sample." : product.planned_pages ? `Up to ${plural(product.planned_pages, "new page")} planned.` : "Reusing saved evidence."}`)); panel.append(row);
        }
        panel.append(node("p", "field-help", preview.detail));
        if (preview.needs_session) {
          panel.append(node("p", "research-notice", "Connect Amazon before gathering new reviews. You can still compare any evidence already saved."), button("Connect Amazon", "button button-white", async () => { const job = await createJob({kind: "auth"}, "compare-error"); if (job) navigate("collection", true); }, "external"));
        } else if (preview.collection_planned) panel.append(node("p", "field-help", "Saved session available. Amazon checks it during collection; sign-in or CAPTCHA may need your attention."));
      } catch (error) {
        if (sequence !== state.comparePreviewSeq) return;
        $("compare-preview").replaceChildren(node("p", "research-notice", error.message || "Could not check the links. Your saved data is unchanged."));
      } finally { if (sequence === state.comparePreviewSeq) { state.comparePreviewBusy = false; updateComparisonControls(); } }
    }, 350);
  }
  function fillComparisonForm(options) {
    $("compare-first").value = options.urls?.[0] || options.asins?.[0] || "";
    $("compare-second").value = options.urls?.[1] || options.asins?.[1] || "";
    $("compare-collect").checked = Boolean(options.collect_missing);
    $("compare-pages").value = String(options.max_pages || 3);
    $("compare-target").value = String(options.target_reviews || 30); $("compare-focus").value = options.focus || "";
    $("compare-consent").checked = false;
    scheduleComparisonPreview();
  }
  async function submitComparison(event) {
    event.preventDefault();
    const job = await createJob(comparisonPayload($("compare-consent").checked && routerStatus().configured), "compare-error");
    if (job) {
      state.compareJobId = job.id; state.compareDirty = false; state.compareHydrated = true; state.compareRenderKey = "";
      state.compareAwaitingResult = true;
      fillComparisonForm(job.options); renderComparison();
    }
    updateComparisonControls();
  }
  function exportComparison(result) {
    const lines = ["Reviewdesk product comparison", `Priorities: ${result.focus || "General owner experience"}`,
      `Method: ${result.method === "openrouter" ? "AI interpretation with original owner sources" : "Local owner evidence, no accepted AI interpretation"}`,
      "This is a saved review sample, not complete Amazon coverage. Findings need human review.", ""];
    for (const product of result.products) {
      lines.push(product.name || productName(product.asin), `https://www.amazon.in/dp/${product.asin}`,
        `${product.stored_reviews} stored reviews; ${product.selected_reviews} selected for context; average rating ${average(product.average_rating)} / 5.`, "");
      for (const [field, title] of [["benefits", "Reason to consider"], ["concerns", "Check before buying"]]) {
        for (const finding of product[field] || []) {
          lines.push(`${title}: ${finding.aspect}`, finding.text);
          for (const source of finding.evidence || []) for (const quote of source.quotes || [source.quote]) lines.push(`Owner quote: ${quote}`);
          lines.push("");
        }
      }
      if (result.method !== "openrouter") for (const source of product.source_examples || []) lines.push(`Owner quote: ${source.quote}`);
      for (const note of product.uncertainties || []) lines.push(`Unknown: ${note}`);
      lines.push("");
    }
    if (result.coverage?.target_reviews) lines.push(`Sample target: ${result.coverage.target_reviews} reviews per product. Reached: ${result.coverage.target_met ? "yes" : "no"}.`);
    if (result.notice) lines.push(`AI notice: ${result.notice.message}`);
    lines.push("", "Evidence limits", ...result.limitations);
    const blob = new Blob([lines.join("\n")], {type: "text/plain;charset=utf-8"}), url = URL.createObjectURL(blob), link = node("a");
    link.href = url; link.download = `reviewdesk-comparison-${result.asins.join("-")}.txt`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  function comparisonDecisionMatrix(result) {
    const panel = node("section", "panel compare-decision");
    panel.append(node("h3", "", "What changes the decision"), node("p", "field-help", "Read the reason to consider each product beside its main concern. These are selected owner accounts, not verified specifications or measured failure rates."));
    const wrap = node("div", "table-scroll"), table = node("table", "brief-comparison-table"), head = node("thead"), header = node("tr");
    header.append(node("th", "", "Owner evidence"));
    for (const product of result.products) {
      const th = node("th"); th.scope = "col"; th.title = product.name || productName(product.asin);
      th.append(node("strong", "", th.title), node("span", "product-id", product.asin)); header.append(th);
    }
    head.append(header); table.append(head); const body = node("tbody");
    for (const [field, title] of [["benefits", "Reason to consider"], ["concerns", "Check before buying"]]) {
      const row = node("tr"), label = node("th", "", title); label.scope = "row"; row.append(label);
      for (const product of result.products) {
        const cell = node("td"), finding = product[field]?.[0];
        if (finding) {
          cell.append(node("strong", "brief-priority", finding.aspect), node("p", "comparison-headline", finding.text));
          const sources = node("details", "brief-source-details"); sources.append(node("summary", "", `Check ${plural(finding.evidence.length, "source review")}`), briefSources(finding.evidence, result.dataset, result.source_context)); cell.append(sources);
        } else cell.append(node("p", "field-help", "No supported finding was returned. This is a gap in the evidence, not proof that the issue is absent."));
        row.append(cell);
      }
      body.append(row);
    }
    const unknowns = node("tr"), unknownLabel = node("th", "", "Still unanswered"); unknownLabel.scope = "row"; unknowns.append(unknownLabel);
    for (const product of result.products) {
      const cell = node("td");
      for (const note of product.uncertainties?.length ? product.uncertainties : ["Current prices, specifications and long-term reliability were not independently verified."]) cell.append(node("p", "field-help", note));
      unknowns.append(cell);
    }
    body.append(unknowns); table.append(body); wrap.append(table); panel.append(wrap); return panel;
  }
  function renderComparison() {
    updateComparisonControls();
    const authKey = state.automation?.auth?.status;
    if (state.compareAuthKey !== authKey) { state.compareAuthKey = authKey; scheduleComparisonPreview(); }
    const jobs = (state.automation?.jobs || []).filter((job) => job.kind === "comparison" && job.options.dataset === state.dataset);
    const active = state.automation?.active_job;
    const current = active?.kind === "comparison" && active.options.dataset === state.dataset ? active : null;
    const job = state.compareJobId ? jobs.find((item) => item.id === state.compareJobId) || (current?.id === state.compareJobId ? current : null) : state.compareDirty ? null : jobs[0];
    $("compare-saved-shortcut").hidden = !job?.result?.products || activeStatuses.has(job.status);
    if (!state.compareHydrated && job && !state.compareDirty) { fillComparisonForm(job.options); state.compareHydrated = true; updateComparisonControls(); }
    $("compare-progress").hidden = !current;
    const progressKey = JSON.stringify(current);
    if ($("compare-progress").dataset.key !== progressKey) {
      $("compare-progress").dataset.key = progressKey;
      preserveJobFocus($("compare-progress"), () => {
        $("compare-progress").replaceChildren();
        if (current) {
          const stop = jobAction("Stop safely", current, () => mutateJob(current, "cancel"), "close"); stop.disabled = current.status === "cancelling";
          $("compare-progress").append(node("h3", "", current.phase === "collecting" ? "Collecting missing reviews" : current.phase === "analysing" ? "Comparing owner experiences" : "Preparing your comparison"), node("p", "field-help", current.detail), stop);
          if (current.progress?.asin) $("compare-progress").append(node("p", "field-help", `${productName(current.progress.asin)}: ${number(current.progress.pages_processed || 0)} pages processed, ${number(current.progress.inserted || 0)} new reviews in this product's current pass.`));
        }
      });
    }
    const key = JSON.stringify([job?.id, job?.updated_at, state.compareDirty, state.overview?.products]);
    if (state.compareRenderKey === key) return;
    state.compareRenderKey = key;
    const container = $("compare-results"); container.replaceChildren(); $("compare-empty").hidden = Boolean(job || current);
    if (!job || activeStatuses.has(job.status)) return;
    const result = job.result;
    container.append(window.ReviewdeskUI.execution(job));
    if (!result?.products) {
      const recovery = node("section", "panel brief-recovery");
      recovery.append(node("h3", "", "Pick up where you left off"), node("p", "research-notice", job.detail || "The comparison could not finish. Saved reviews remain available."), button("Prepare a safe resume", "button button-white", () => { fillComparisonForm({...job.options, collect_missing: state.dataset === "workspace"}); state.compareDirty = true; state.compareJobId = null; state.compareRenderKey = ""; navigate("compare", true); toast("Saved progress prepared. Review the plan, then start. AI sharing is off."); })); container.append(recovery); return;
    }
    const ai = result.method === "openrouter", summary = node("section", "panel brief-summary");
    summary.append(node("span", "badge", ai ? "AI INTERPRETATION + OWNER SOURCES" : "LOCAL OWNER EVIDENCE"), node("h2", "", "Your product comparison"));
    const relation = {alternatives: "The AI considers these potential alternatives. Check that their variants and intended uses match.", different_uses: "These products serve different purposes. Compare their owner experiences separately; there is no meaningful overall winner.", uncertain: "The evidence does not establish whether these products are direct alternatives."};
    summary.append(node("p", "field-help", relation[result.relationship] || relation.uncertain));
    summary.append(button("Save comparison with sources", "text-button", () => exportComparison(result), "download"));
    if (result.focus) summary.append(node("p", "compare-focus-note", `Your priorities: ${result.focus}`));
    const coverage = result.coverage;
    if (coverage?.target_reviews) {
      const note = node("section", "compare-coverage"); note.append(node("strong", "", coverage.target_met ? "Sample target reached" : "Your sample is still growing"));
      note.append(node("p", "field-help", result.products.map((product) => `${product.name || productName(product.asin)}: ${number(product.stored_reviews)} / ${number(coverage.target_reviews)} reviews`).join(". ")));
      if (!coverage.target_met && state.dataset === "workspace") {
        if (coverage.needs_more.some((asin) => !coverage.available_pages_ended?.includes(asin))) note.append(button("Prepare to resume collection", "text-button", () => { fillComparisonForm({...job.options, collect_missing: true}); comparisonChanged(); navigate("compare", true); toast("The plan will resume saved cursors for products below your target. AI sharing is off."); }, "refresh"));
        else note.append(node("p", "field-help", "Available review pages have ended. The target remains unmet; a larger requested target does not create more accessible reviews."));
      }
      summary.append(note);
    }
    if (result.cache_hit) summary.append(node("p", "field-help", "Reused a source-checked saved interpretation. No new AI request."));
    if (result.notice) summary.append(node("p", "research-notice", result.notice.message || "AI interpretation was unavailable. Local evidence is shown."));
    if (result.products.some((product) => product.critical_rated_reviews === 0)) summary.append(node("p", "research-notice", "At least one sample has no 1 or 2 star reviews. Negative experiences may be underrepresented; a high sample rating is not proof of reliability."));
    const timings = node("div", "compare-timings");
    for (const [label, value] of [["Collection", result.timings.collection_seconds], ["Preparation", result.timings.preparation_seconds], [result.cache_hit ? "AI cache lookup" : "AI + validation", result.timings.ai_seconds], ["Total job", result.timings.total_seconds]]) {
      const cell = node("div"); cell.append(node("span", "field-help", label), node("strong", "", `${Number(value || 0).toFixed(2)}s`)); timings.append(cell);
    }
    const timingDetails = node("details", "brief-source-details compare-timing-details");
    timingDetails.append(node("summary", "", `${Number(result.timings.total_seconds || 0).toFixed(2)}s total · See collection and AI timings`), timings, node("p", "field-help", result.collection_mode === "saved_reviews" ? "Used saved review samples. Collection time is zero for this run; these are not fresh-download timings." : "Measured from the start of this job, excluding queue time. Collection includes browser startup and saved-page writes.")); summary.append(timingDetails);
    if (result.collection) {
      const collection = node("section", "compare-collection"); collection.append(node("h3", "", "Collection outcome"));
      for (const product of result.collection.products || []) collection.append(node("p", "field-help", `${productName(product.asin)}: ${product.status}. ${number(product.inserted)} new reviews across ${number(product.pages_processed)} pages.${product.elapsed_seconds != null ? ` ${Number(product.elapsed_seconds).toFixed(2)}s.` : ""} ${product.detail || ""}`));
      if (result.collection.detail) collection.append(node("p", "research-notice", result.collection.detail));
      if (result.collection.status === "blocked") collection.append(button("Refresh Amazon sign-in", "button button-white", async () => { const job = await createJob({kind: "auth"}, "compare-error"); if (job) navigate("collection", true); }, "external"));
      if (result.collection.status !== "completed") collection.append(node("p", "field-help", "This is a bounded or interrupted sample. Any findings below use only reviews that were actually saved."));
      summary.append(collection);
    }
    container.append(summary);
    if (ai) container.append(comparisonDecisionMatrix(result));
    const wrap = node("div", "table-scroll panel"), table = node("table", "brief-comparison-table"), header = node("tr");
    header.append(node("th", "", "Saved evidence"));
    for (const product of result.products) { const th = node("th"); th.append(node("strong", "", product.name || productName(product.asin)), node("span", "product-id", product.asin)); th.scope = "col"; th.title = product.name || productName(product.asin); header.append(th); }
    const head = node("thead"); head.append(header); table.append(head);
    const body = node("tbody");
    for (const [label, format] of [["Stored reviews", (p) => number(p.stored_reviews)], ["Average of rated reviews", (p) => `${average(p.average_rating)} / 5 (${number(p.rated_reviews)} rated)`], ["1 or 2 star reviews", (p) => number(p.critical_rated_reviews)], ["Selected for context", (p) => number(p.selected_reviews)]]) {
      const row = node("tr"), title = node("th", "", label); title.scope = "row"; row.append(title);
      for (const product of result.products) row.append(node("td", "", format(product))); body.append(row);
    }
    table.append(body); wrap.append(table); container.append(wrap);
    const grid = node("div", "brief-product-grid");
    for (const product of result.products) {
      const panel = node("article", "panel brief-product"); panel.append(node("h3", "", product.name || productName(product.asin)), node("p", "product-id", `ASIN ${product.asin}`));
      if (product.critical_rated_reviews === 0) panel.append(node("p", "research-notice", "This sample has no 1 or 2 star reviews. It may understate negative experiences."));
      if (result.focus && product.focus_matched_reviews === 0) panel.append(node("p", "research-notice", "No lexical matches for your priorities were found. General context is shown; suitability remains unknown."));
      const count = state.overview?.products.find((item) => item.asin === product.asin)?.review_count;
      if (count != null && count !== product.stored_reviews) panel.append(node("p", "research-notice", "The saved sample has changed. Run again to compare the current evidence."));
      if (!product.selected_reviews) {
        panel.append(node("p", "research-notice", "No usable owner text is saved for this product. No AI request is made for an incomplete pair."), button("Open collection and sign-in", "text-button", () => { $("collection-urls").value = `https://www.amazon.in/dp/${product.asin}`; navigate("collection", true); }, "layers"));
      } else if (ai) {
        for (const [field, title] of [["benefits", "What owners appreciate"], ["concerns", "What deserves attention"]]) {
          const section = node("section", "brief-section"); section.append(node("h4", "", title));
          for (const finding of product[field] || []) {
            const card = node("div", "brief-finding"), source = node("details", "brief-source-details");
            card.append(node("strong", "brief-priority", finding.aspect), node("p", "", finding.text));
            source.append(node("summary", "", "Check owner evidence"), briefSources(finding.evidence, result.dataset, result.source_context)); card.append(source); section.append(card);
          }
          if (!product[field]?.length) section.append(node("p", "field-help", "No supported finding returned. This does not establish that none exists."));
          panel.append(section);
        }
        const unknowns = node("section", "brief-section"); unknowns.append(node("h4", "", "What remains uncertain"));
        for (const note of [...(product.uncertainties || []), "Prices, specifications and authenticity were not independently verified. Owner reports can conflict; inspect the original text for counterevidence."]) unknowns.append(node("p", "field-help", note)); panel.append(unknowns);
      } else {
        const sources = node("details", "brief-source-details");
        sources.append(node("summary", "", `Read ${number(product.source_examples?.length || 0)} selected owner reports`), briefSources(product.source_examples || [], result.dataset)); panel.append(sources);
      }
      if (product.stored_reviews) panel.append(button("Explore saved reviews", "text-button", () => exploreProduct(product.asin), "arrow")); grid.append(panel);
    }
    if (ai) {
      const details = node("details", "brief-product-details"); details.append(node("summary", "", "Explore the full findings for each product"), grid); container.append(details);
    } else container.append(grid);
    const limits = node("section", "panel brief-limits"), list = node("ul"); limits.append(node("h3", "", "How to read this comparison"));
    for (const limitation of result.limitations) list.append(node("li", "", limitation)); limits.append(list, usageSummary(result.usage, result.model)); container.append(limits);
    if (state.compareAwaitingResult && state.view === "compare") {
      state.compareAwaitingResult = false; container.scrollIntoView({behavior: "auto", block: "start"});
    }
  }
  return {updateComparisonControls, comparisonChanged, comparisonPayload, scheduleComparisonPreview, fillComparisonForm, submitComparison, exportComparison, comparisonDecisionMatrix, renderComparison};
};
