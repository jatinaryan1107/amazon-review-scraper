"use strict";

window.ReviewdeskUI.createInspection = ({$, activeStatuses, average, button, chooseDataset, createJob, dateLabel, empty, exploreProduct, jobAction, messageAt, mutateJob, navigate, node, number, openEvidence, openImport, plural, preserveJobFocus, productLabel, productName, request, routerStatus, state, timeLabel, toast, usageSummary}) => {
  const buyingPriorityNames = {usability: "Ease of use", cleaning: "Cleaning", value: "Value for money", calls: "Clear calls", comfort: "Comfort", sound: "Sound", battery: "Battery life", durability: "Durability", overall: "Everyday use"};
  function buyingPriorities() { return [...document.querySelectorAll('[name="buying-priority"]:checked')].map((input) => input.value).sort(); }
  function setBuyingCompare(enabled) {
    $("buying-second-wrap").hidden = !enabled; $("buying-second").required = enabled;
    $("buying-compare").setAttribute("aria-expanded", String(enabled));
    $("buying-compare").textContent = enabled ? "Remove comparison" : "Compare another product";
    if (!enabled) $("buying-second").value = "";
  }
  function fillBuyingForm(options) {
    $("buying-product").value = options.asins?.[0] || "";
    setBuyingCompare(Boolean(options.asins?.[1])); $("buying-second").value = options.asins?.[1] || "";
    for (const input of document.querySelectorAll('[name="buying-priority"]')) input.checked = Boolean(options.priorities?.includes(input.value));
    $("buying-consent").checked = false;
    updateBuyingControls();
  }
  function syncBuyingProducts() {
    const products = state.overview?.products || [], select = $("buying-suggestions");
    const key = JSON.stringify(products.map((product) => [product.asin, product.name]));
    if (select.dataset.products === key) return;
    select.dataset.products = key; state.briefRenderKey = "";
    select.replaceChildren(...products.map((product) => { const option = node("option"); option.value = product.asin; option.label = productLabel(product); return option; }));
  }
  function inspectionAsin(value) {
    if (/^[A-Za-z0-9]{10}$/.test(value)) return value.toUpperCase();
    if (/[\s\\\u0000-\u001f\u007f]/.test(value)) return "";
    const link = value.match(/^https:\/\/(?:www\.)?amazon\.in(\/[^?#]*)/i);
    if (!link) return "";
    const path = link[1];
    const match = path.match(/^\/(?:[^/]+\/)?(?:dp|product-reviews)\/([A-Za-z0-9]{10})(?:\/|$)/i)
      || path.match(/^\/portal\/customer-reviews\/([A-Za-z0-9]{10})(?:\/ref=[A-Za-z0-9_-]{1,200})?\/?$/i);
    return match ? match[1].toUpperCase() : "";
  }
  function renderInspectionPreview(inputId, previewId) {
    const preview = $(previewId), value = $(inputId).value.trim(), asin = inspectionAsin(value);
    const product = state.overview?.products.find((item) => item.asin === asin);
    const key = JSON.stringify([Boolean(value), asin, Boolean(state.overview), Boolean(product), state.dataset, product?.name, product?.review_count]);
    if (preview.dataset.key === key) return;
    preview.dataset.key = key;
    preview.replaceChildren();
    if (!value) {
      preview.append(node("p", "field-help", "Choose a product to see its saved name and review count. This preview uses workspace data only."));
      return;
    }
    if (!asin) {
      preview.append(node("p", "field-help", "Use a ten-character ASIN or a full HTTPS amazon.in product link. Short links need their full product URL. The server checks the product before a run."));
      return;
    }
    preview.append(node("strong", "", product ? product.name || "Name not saved" : state.overview ? "Product not saved in this workspace" : "Saved product details unavailable"), node("span", "product-id", `ASIN ${asin}`));
    if (!state.overview) {
      preview.append(node("p", "field-help", "Saved workspace details are unavailable. This preview does not fetch a product name or reviews."));
    } else if (!product) {
      preview.append(node("p", "field-help", "No saved reviews for this product in this workspace. Collect reviews or import a CSV before inspection. No live product details were fetched."));
    } else {
      const count = product.review_count == null ? "Saved review count unavailable." : `${plural(product.review_count, "saved review")}.`;
      preview.append(node("p", "field-help", `${count} ${state.dataset === "demo" ? "Fictional demo sample." : "Details from this workspace, with no live product lookup."}`));
    }
  }
  function updateBuyingControls() {
    renderInspectionPreview("buying-product", "buying-product-preview");
    renderInspectionPreview("buying-second", "buying-second-preview");
    const active = state.automation?.active_job, busy = Boolean(active && activeStatuses.has(active.status));
    const router = routerStatus(), ownsJob = busy && active.kind === "brief" && active.options.dataset === state.dataset;
    $("buying-submit").disabled = !state.automation || state.mutation || busy;
    for (const input of $("buying-form").querySelectorAll("input")) input.disabled = ownsJob;
    $("buying-consent").disabled = ownsJob || !router.configured;
    $("buying-submit-label").textContent = $("buying-consent").checked && router.configured ? "Build AI interpretation" : "Inspect saved reviews";
    $("buying-compare").disabled = ownsJob; $("buying-example").disabled = ownsJob || !state.overview?.products.length;
    $("buying-demo").disabled = busy; $("buying-ai-setup").hidden = router.configured || Boolean(state.showcase);
    const policy = state.provider?.require_zdr === true ? "Zero retention is required." : state.provider?.require_zdr === false ? "Zero retention is off; provider and account logging settings may retain shared text." : "Check the saved AI privacy settings before sharing.";
    $("buying-ai-status").textContent = state.showcase ? "This showcase uses local evidence. External AI is disabled, so no key is needed." : router.configured ? `Optional AI uses at most one free request for this brief, including a comparison. ${policy}` : "Local evidence works without an API key. AI interpretation is optional and can be set up in the research workspace.";
  }
  function buyingChanged() {
    state.briefDirty = true; state.briefJobId = null; $("buying-consent").checked = false;
    messageAt("buying-error", ""); renderBuying();
  }
  async function startBuying(products, priorities, external) {
    const job = await createJob({kind: "brief", dataset: state.dataset, products, priorities, allow_external: external}, "buying-error");
    if (job) {
      state.briefJobId = job.id; state.briefDirty = false; state.briefHydrated = true; state.briefAwaitingResult = true;
      fillBuyingForm(job.options); state.briefRenderKey = ""; renderBuying();
    }
    updateBuyingControls();
  }
  async function submitBuying(event) {
    event.preventDefault(); const products = [$("buying-product").value.trim()];
    if (!$("buying-second-wrap").hidden) products.push($("buying-second").value.trim());
    await startBuying(products, buyingPriorities(), $("buying-consent").checked && routerStatus().configured);
  }
  function briefSources(sources, dataset, contexts = []) {
    const container = node("div", "brief-sources");
    for (const source of sources) {
      const entry = node("div", "evidence-card");
      entry.append(node("strong", "source-product-name", productName(source.asin)), node("span", "product-id", `ASIN ${source.asin}`));
      const quotes = source.quotes?.length ? source.quotes : [source.quote];
      for (const quote of quotes) entry.append(node("blockquote", "", quote));
      const context = contexts.find((item) => item.asin === source.asin && item.review_id === source.review_id);
      if (context) {
        const full = node("details", "brief-source-details");
        full.append(node("summary", "", "Read the full excerpt shown to AI"), node("p", "field-help", context.title), node("blockquote", "", context.body));
        entry.append(full);
      }
      if (source.rating) entry.append(node("p", "field-help", `Source review rating: ${average(source.rating)} / 5. The rating alone does not describe this passage's sentiment.`));
      const open = button("Read original review", "text-button", () => openEvidence(source, dataset, open), "arrow");
      entry.append(open); container.append(entry);
    }
    return container;
  }
  function briefFinding(finding, dataset) {
    const card = node("div", "brief-finding");
    card.append(node("span", "brief-priority", buyingPriorityNames[finding.priority] || "Owner experience"), node("p", "", finding.text));
    const details = node("details", "brief-source-details");
    details.append(node("summary", "", `Check ${plural(finding.evidence.length, "source review")}`), briefSources(finding.evidence, dataset));
    card.append(details); return card;
  }
  function collectForBuying(asin) {
    if (state.dataset !== "workspace") chooseDataset("workspace");
    $("buying-product").value = asin; state.briefDirty = true;
    $("collection-urls").value = `https://www.amazon.in/dp/${asin}`;
    $("collection-pages").value = "10"; $("collection-resume").checked = true;
    navigate("collection", true); $("collection-urls").focus();
    toast("Product prepared for collection. Check sign-in and the page limit, then start when ready. Return to Buying brief afterwards.");
  }
  function buyingComparison(result) {
    const panel = node("section", "panel brief-comparison"); panel.append(node("h3", "", "Your priorities, side by side"), node("p", "field-help", "Compare selected owner reports on the same priorities. These are examples, not a measurement of how common an issue is or which product is best."));
    const wrap = node("div", "table-scroll"), table = node("table", "brief-comparison-table"), head = node("thead"), header = node("tr");
    const priority = node("th", "", "Your priority"); priority.scope = "col"; header.append(priority);
    for (const product of result.products) {
      const cell = node("th"); cell.scope = "col";
      const name = node("strong", "", product.name || productName(product.asin)); name.title = name.textContent;
      cell.append(name, node("span", "product-id", product.asin), node("p", "field-help", `${number(product.selected_reviews)} of ${plural(product.stored_reviews, "stored review")} selected for context`)); header.append(cell);
    }
    head.append(header); table.append(head); const body = node("tbody");
    for (const key of result.priorities.length ? result.priorities : ["overall"]) {
      const row = node("tr"), heading = node("th", "", buyingPriorityNames[key]); heading.scope = "row"; row.append(heading);
      for (const product of result.products) {
        const cell = node("td"); let found = false;
        for (const [section, label] of [["benefits", "Appreciated"], ["concerns", "Reported concern"]]) {
          const finding = product[section]?.find((item) => item.priority === key); if (!finding) continue;
          found = true; const item = node("div", "comparison-finding"); item.append(node("strong", "brief-priority", label), node("p", "", finding.text));
          const sources = node("details", "brief-source-details"); sources.append(node("summary", "", "Check owner evidence"), briefSources(finding.evidence, result.dataset)); item.append(sources); cell.append(item);
        }
        if (!found) cell.append(node("p", "field-help", "No supported finding was returned for this priority. More evidence is needed."));
        row.append(cell);
      }
      body.append(row);
    }
    table.append(body); wrap.append(table); panel.append(wrap);
    for (const finding of result.tradeoffs) panel.append(briefFinding(finding, result.dataset));
    return panel;
  }
  function renderBuying() {
    if (!$("buying-results")) return;
    updateBuyingControls();
    const jobs = (state.automation?.jobs || []).filter((job) => job.kind === "brief" && job.options?.dataset === state.dataset);
    const active = state.automation?.active_job;
    const job = state.briefJobId ? jobs.find((item) => item.id === state.briefJobId) || (active?.id === state.briefJobId ? active : null) : state.briefDirty ? null : jobs[0];
    $("buying-saved-shortcut").hidden = job?.status !== "succeeded" || !job.result?.products;
    if (!state.briefHydrated && job && !state.briefDirty) { fillBuyingForm(job.options); state.briefHydrated = true; }
    const progress = active?.kind === "brief" && active.options.dataset === state.dataset && activeStatuses.has(active.status) ? active : null;
    $("buying-progress").hidden = !progress;
    const progressKey = JSON.stringify(progress);
    if ($("buying-progress").dataset.key !== progressKey) {
      $("buying-progress").dataset.key = progressKey;
      preserveJobFocus($("buying-progress"), () => {
        $("buying-progress").replaceChildren();
        if (progress) {
          const heading = node("h3", "", progress.phase === "analysing" ? "Writing your buying brief" : "Finding owner evidence");
          const stop = jobAction("Stop safely", progress, () => mutateJob(progress, "cancel"), "close"); stop.disabled = progress.status === "cancelling";
          $("buying-progress").append(heading, node("p", "field-help", progress.options.allow_external ? "Your saved reviews stay available while the optional AI request runs. You can stop safely." : "Selecting saved review evidence locally. No AI request is being made."), stop);
        }
      });
    }
    const key = JSON.stringify([job?.id, job?.status, job?.updated_at, job?.result, state.briefDirty]);
    if (state.briefRenderKey === key) return;
    state.briefRenderKey = key; const container = $("buying-results"); container.replaceChildren();
    $("buying-empty").hidden = Boolean(job || progress);
    if (!job || activeStatuses.has(job.status)) return;
    if (job.status !== "succeeded" || !job.result?.products) {
      const panel = node("section", "panel brief-recovery");
      panel.append(node("h2", "", "This brief could not finish"), node("p", "", job.detail || "Your saved reviews remain available."), button("Show local owner evidence", "button button-white", () => startBuying(job.options.asins, job.options.priorities, false), "search"));
      container.append(panel); return;
    }
    const result = job.result, ai = result.method === "openrouter";
    container.append(window.ReviewdeskUI.execution(job));
    const intro = node("section", "brief-result-heading");
    intro.append(node("span", "eyebrow", ai ? result.cache_hit ? "SAVED AI BUYING BRIEF" : "AI BUYING BRIEF" : "LOCAL OWNER EVIDENCE"), node("h2", "", result.products.length === 2 ? "Compare the tradeoffs" : "Your buying brief"), node("p", "muted", `Saved ${timeLabel(job.updated_at)}. Rebuild to check the latest stored evidence.`));
    if ((result.dataset || job.options.dataset) === "demo") intro.append(node("p", "research-notice", "Fictional demo sample. These reviews illustrate the workflow and are not customer evidence."));
    const tags = node("div", "brief-tags");
    for (const priority of result.priorities.length ? result.priorities : ["overall"]) tags.append(node("span", "badge", buyingPriorityNames[priority]));
    tags.append(node("span", "badge", "CURRENT PRICE NOT CHECKED")); intro.append(tags); container.append(intro);
    if (result.notice) {
      const validationFailure = ["report_format", "evidence_reference", "evidence_quote", "invalid_json", "invalid_response"].includes(result.notice.code);
      const notice = node("div", "research-notice"); notice.append(node("strong", "", validationFailure ? "The AI interpretation did not pass checks" : "AI unavailable; owner evidence is still here"), node("p", "", validationFailure ? "The response could not be accepted safely. Original owner reports remain available below; no unchecked buying recommendation is shown." : result.notice.message));
      if (result.notice.retry_after != null) notice.append(node("p", "", `Try AI after ${new Date(new Date(job.updated_at).getTime() + Number(result.notice.retry_after) * 1000).toLocaleTimeString()}. No automatic retry was made.`));
      container.append(notice);
    }
    if (!ai) container.append(node("p", "brief-local-explanation", "These are locally selected source passages, not an AI buying recommendation. Ratings and keyword matches alone cannot establish a product's strengths or weaknesses."));
    const grid = node("div", "brief-product-grid");
    for (const [productIndex, product] of result.products.entries()) {
      const panel = node("article", "panel brief-product"), heading = node("header", "brief-product-heading");
      if (result.products.length === 2) heading.append(node("span", "badge", productIndex === 0 ? "FIRST PRODUCT" : "SECOND PRODUCT"));
      heading.append(node("h3", "", product.name || productName(product.asin)), node("span", "product-id", `ASIN ${product.asin}`));
      const currentCount = state.overview?.products.find((item) => item.asin === product.asin)?.review_count;
      heading.append(node("p", "brief-rating", `Saved sample average: ${average(product.average_rating)} / 5 · ${plural(product.stored_reviews, "stored review")}`), node("p", "field-help", `${number(product.selected_reviews)} reviews selected for context. ${product.last_review_added_at ? `Reviews last added ${dateLabel(product.last_review_added_at)}.` : "Collection date unavailable."}`));
      panel.append(heading);
      if (currentCount != null && currentCount !== product.stored_reviews) panel.append(node("p", "research-notice", "The saved review count has changed. Rebuild this brief to include the current collection."));
      if (!product.stored_reviews || !product.selected_reviews) {
        panel.append(node("h4", "", product.stored_reviews ? "No matching owner text yet" : "No saved reviews yet"), node("p", "field-help", "This brief needs relevant saved review text. No AI request was made for an incomplete comparison."));
        if (state.dataset === "workspace") panel.append(button("Collect reviews for this product", "button button-white", () => collectForBuying(product.asin), "layers"));
        panel.append(button("Import a review CSV", "text-button", openImport, "upload"));
      } else if (ai) {
        for (const [field, title] of [["benefits", "What owners appreciate"], ["concerns", "What deserves attention"]]) {
          const section = node("section", `brief-section brief-${field}`); section.append(node("h4", "", title));
          if (!product[field]?.length) section.append(node("p", "field-help", "No supported finding was returned for this section. This does not establish that none exists."));
          for (const finding of product[field] || []) section.append(briefFinding(finding, result.dataset)); panel.append(section);
        }
        const unknowns = node("section", "brief-section brief-unknowns"); unknowns.append(node("h4", "", "What remains uncertain"));
        const list = node("ul");
        for (const note of [...product.uncertainties, "Current price, exact specifications and long-term failure rates were not established."]) list.append(node("li", "", note));
        unknowns.append(list); panel.append(unknowns);
      } else {
        const details = node("details", "brief-source-details"); details.open = true;
        details.append(node("summary", "", "Read selected owner reports"), briefSources(product.source_examples || [], result.dataset)); panel.append(details);
      }
      if (product.stored_reviews) panel.append(button("Explore all saved reviews", "text-button", () => exploreProduct(product.asin), "arrow"));
      grid.append(panel);
    }
    if (result.products.length === 2 && ai) {
      container.append(buyingComparison(result));
      const details = node("details", "brief-product-details"); details.append(node("summary", "", "Explore each product and its full findings"), grid); container.append(details);
    } else container.append(grid);
    if (result.products.length === 2 && !ai) {
      const comparison = node("section", "panel brief-comparison"); comparison.append(node("h3", "", "How the evidence compares"));
      for (const [index, product] of result.products.entries()) comparison.append(node("p", "field-help", `${index === 0 ? "First" : "Second"} product: ${product.name || productName(product.asin)}`));
      for (const finding of result.tradeoffs) comparison.append(briefFinding(finding, result.dataset));
      if (!result.tradeoffs.length) comparison.append(node("p", "field-help", "No source-supported comparison was accepted. Compare the owner reports above; the collected evidence does not establish an overall winner."));
      container.append(comparison);
    }
    const limits = node("section", "panel brief-limits"); limits.append(node("h3", "", "Keep these limits in view"));
    const list = node("ul"); for (const note of result.limitations) list.append(node("li", "", note)); limits.append(list);
    const process = node("details", "brief-source-details"); process.append(node("summary", "", "How this brief was made"), node("p", "field-help", "Python computes the stored ratings. Local retrieval selects up to 12 relevant reviews per product, balancing available rating groups. Optional AI interprets those excerpts. Every accepted finding links to supplied source text; that check does not guarantee the interpretation."), usageSummary(result.usage, result.model));
    if (ai && result.cache_hit) process.append(node("p", "field-help", "Reused a revalidated saved interpretation without a new model request."));
    limits.append(process); container.append(limits);
    if (state.briefAwaitingResult && state.view === "buying") {
      state.briefAwaitingResult = false;
      container.scrollIntoView({behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
    }
  }
  $("buying-consent").addEventListener("change", updateBuyingControls);
  return {buyingPriorityNames, buyingPriorities, setBuyingCompare, fillBuyingForm, syncBuyingProducts, updateBuyingControls, buyingChanged, startBuying, submitBuying, briefSources, briefFinding, collectForBuying, buyingComparison, renderBuying};
};
