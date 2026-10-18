"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const svgNS = "http://www.w3.org/2000/svg";
  const validViews = new Set(["overview", "reviews", "products", "collection", "analysis", "research"]);
  function preference(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }
  const state = {
    dataset: preference("reviewdesk-dataset") === "workspace" ? "workspace" : "demo",
    view: "overview", overview: null, reviews: null,
    filters: { q: "", asin: "", rating: "all", verified: "all", sort: "newest", page: 1 },
    overviewController: null, reviewController: null, file: null,
    importing: false, exporting: false, toastTimer: null, searchTimer: null,
    overviewVersion: 0, reviewVersion: 0,
    overviewError: null, automation: null, automationVersion: 0, automationController: null,
    automationPromise: null, automationPending: false,
    pollTimer: null, mutation: false, jobStatuses: new Map(), jobRenderKey: "", analysisJobKey: "",
    analysis: null, analysisVersion: 0, analysisController: null, analysisLoading: false,
    provider: null, providerVersion: 0, providerSaving: false, providerPolicyDirty: false,
    providerChecking: false, providerQuota: null, providerQuotaError: false,
    plan: null, planVersion: 0, planController: null, planLoading: false, planError: null,
    researchRenderKey: "", researchJobKey: "",
  };
  const viewCopy = {
    overview: ["Review overview", "Explore feedback, compare products, and keep the evidence close."],
    reviews: ["Every review, in focus.", "Find the feedback you need. Open any review to see the full story."],
    products: ["Your products", "Compare the feedback, then explore the reviews behind each product."],
    collection: ["Collect. Save. Understand.", "Bring product reviews into your workspace, with progress you can pick up again."],
    analysis: ["From feedback to findings", "Explore patterns, see the limits, and check the reviews behind every theme."],
    research: ["Ask your reviews", "Start with a question. Follow the evidence in your saved reviews."],
  };
  const number = (value) => new Intl.NumberFormat().format(Number(value) || 0);
  const average = (value, precision = 1) => value === null || value === undefined || value === "" ? "N/A" : Number(value).toFixed(precision);
  const percent = (value) => `${Math.round(Number(value) || 0)}%`;
  const localQuestions = new Set([
    "how many reviews", "how many reviews are there", "how many reviews are stored",
    "how many reviews are in this selection", "how many reviews are in this dataset",
    "how many reviews are in this workspace", "how many reviews do we have",
    "how many reviews do i have", "total reviews", "review count", "what is the review count",
    "what is the total review count", "count reviews", "count the reviews",
    "what is the average rating", "average rating", "what is the mean rating", "mean rating",
    "show the average rating", "show average rating", "rating distribution", "ratings distribution",
    "what is the rating distribution", "show rating distribution", "show the rating distribution",
    "rating breakdown", "show rating breakdown", "show the rating breakdown",
  ]);
  const isLocalQuestion = (question) => localQuestions.has(String(question).toLowerCase().replace(/\s+/g, " ").trim().replace(/[.?!]+$/, "").trim());
  const isVerified = (value) => value === true || value === 1 || String(value).toLowerCase() === "true";
  const plural = (count, noun) => `${number(count)} ${noun}${Number(count) === 1 ? "" : "s"}`;
  function node(tag, className, textValue) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (textValue !== undefined) result.textContent = String(textValue);
    return result;
  }
  function icon(name, className = "") {
    const result = document.createElementNS(svgNS, "svg");
    result.setAttribute("class", `icon ${className}`.trim());
    result.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(svgNS, "use");
    use.setAttribute("href", `#i-${name}`);
    result.append(use);
    return result;
  }
  function button(label, className, action, iconName) {
    const result = node("button", className);
    result.type = "button";
    if (iconName) result.append(icon(iconName));
    result.append(node("span", "", label));
    result.addEventListener("click", action);
    return result;
  }
  function dateLabel(value) {
    if (!value) return "Date unavailable";
    const text = String(value);
    if (!/^\d{4}-\d{2}-\d{2}/.test(text)) return text;
    const parsed = new Date(text.slice(0, 10) + "T12:00:00");
    return Number.isNaN(parsed.getTime()) ? text : parsed.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }
  function savePreference(key, value) {
    try { localStorage.setItem(key, value); } catch { /* The workspace also works when browser storage is unavailable. */ }
  }
  function queryString(withFilters = false) {
    const params = new URLSearchParams({ dataset: state.dataset });
    if (withFilters) {
      for (const [key, value] of Object.entries(state.filters)) {
        if (value !== "") params.set(key, String(value));
      }
      params.set("page_size", "10");
    }
    return params;
  }
  async function request(url, options = {}) {
    let response;
    const controller = new AbortController(), parentSignal = options.signal;
    let timedOut = false;
    const abort = () => controller.abort();
    if (parentSignal?.aborted) controller.abort();
    else parentSignal?.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 20000);
    try {
      response = await fetch(url, { cache: "no-store", ...options, signal: controller.signal });
    } catch (error) {
      if (timedOut) throw new Error("The local server took too long to respond. Check its terminal, then try again.");
      if (error.name === "AbortError") throw error;
      throw new Error("The local server could not be reached. Make sure it is running, then try again.");
    } finally {
      clearTimeout(timeout);
      parentSignal?.removeEventListener("abort", abort);
    }
    if (!response.ok) {
      let message = `The request failed (${response.status}). Please try again.`;
      try {
        const data = await response.json();
        if (data.error) message = data.error;
        const details = describeProviderDiagnostics(data.diagnostics);
        if (details) message += ` ${details}`;
        if (data.retry_after != null && Number.isFinite(Number(data.retry_after))) message += ` Wait at least ${number(Math.ceil(Number(data.retry_after)))} seconds before trying again. Retries are manual.`;
      } catch { /* Keep the readable fallback. */ }
      throw new Error(message);
    }
    return response;
  }
  function connection(connected) {
    $("connection-label").classList.toggle("disconnected", !connected);
    $("connection-label").lastElementChild.textContent = connected ? "Local data" : "Connection issue";
    $("connection-label").firstElementChild.style.background = connected ? "" : "var(--danger)";
  }
  function empty(container, title, description, iconName = "message", action) {
    container.replaceChildren();
    const mark = node("span", "empty-icon"); mark.append(icon(iconName));
    container.append(mark, node("h3", "", title), node("p", "", description));
    if (action) container.append(button(action.label, "button button-white", action.run, action.icon));
    container.hidden = false;
  }
  function setMenu(open) {
    $("sidebar").classList.toggle("open", open);
    $("sidebar-overlay").hidden = !open;
    $("menu-toggle").setAttribute("aria-expanded", String(open));
    $("menu-toggle").setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    if (open) $("dataset-select").focus();
  }
  function navigate(view, focus = false) {
    if (!validViews.has(view)) view = "overview";
    const changed = state.view !== view;
    state.view = view;
    document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${view}` || (!state.overview && !["collection", "analysis", "research"].includes(view)); });
    $("page-error").hidden = !state.overviewError || ["collection", "analysis", "research"].includes(view);
    $("loading-panel").hidden = Boolean(state.overview) || ["collection", "analysis", "research"].includes(view) || Boolean(state.overviewError);
    if (["collection", "analysis", "research"].includes(view)) $("main-content").setAttribute("aria-busy", "false");
    document.querySelectorAll("[data-view]").forEach((link) => {
      const active = link.dataset.view === view;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    const pageLabel = view === "research" ? "Ask reviews" : view.charAt(0).toUpperCase() + view.slice(1);
    $("breadcrumb-page").textContent = pageLabel;
    $("page-title").textContent = viewCopy[view][0];
    $("page-subtitle").textContent = viewCopy[view][1];
    document.title = `${pageLabel} | Reviewdesk`;
    setMenu(false);
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view === "reviews" && state.overview && (changed || !state.reviews)) loadReviews();
    if (view === "analysis" && changed) loadAnalysis();
    if (view === "collection" && changed) loadAutomation();
    if (view === "research" && changed) { renderResearch(); loadAutomation(); }
    if (changed) window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    if (focus) $("main-content").focus({ preventScroll: true });
  }
  function resetFilters() {
    clearTimeout(state.searchTimer);
    state.filters = { q: "", asin: "", rating: "all", verified: "all", sort: "newest", page: 1 };
    state.reviews = null;
    syncFilters();
  }
  function renderDatasetContext() {
    const demo = state.dataset === "demo", banner = $("provenance-banner");
    $("breadcrumb-workspace").textContent = demo ? "Demo workspace" : "My workspace";
    banner.classList.toggle("workspace-banner", !demo);
    banner.querySelector("p").replaceChildren(node("strong", "", demo ? "Demo workspace" : "Your local workspace"), node("span", "", demo ? "You're exploring fictional reviews. Import or collect reviews to work with your own data." : "Reviews and saved reports are stored on this device. External analysis only runs when you choose it."));
    banner.querySelector(".badge").textContent = demo ? "SAMPLE DATA" : "LOCAL DATA";
  }
  function syncFilters() {
    $("review-search").value = state.filters.q;
    $("product-filter").value = state.filters.asin;
    $("rating-filter").value = state.filters.rating;
    $("verified-filter").value = state.filters.verified;
    $("review-sort").value = state.filters.sort;
    $("clear-filters").hidden = !state.filters.q && !state.filters.asin && state.filters.rating === "all" && state.filters.verified === "all";
  }
  async function loadOverview(silent = false) {
    if (typeof silent !== "boolean") silent = false;
    renderDatasetContext();
    state.overviewController?.abort();
    state.reviewController?.abort();
    state.overviewController = new AbortController();
    const version = ++state.overviewVersion;
    ++state.reviewVersion;
    if (!silent) { state.overview = null; state.reviews = null; }
    state.overviewError = null;
    $("loading-panel").hidden = silent || ["collection", "analysis", "research"].includes(state.view);
    $("page-error").hidden = true;
    $("export-button").disabled = true;
    if (!silent) document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${state.view}` || !["collection", "analysis", "research"].includes(state.view); });
    $("main-content").setAttribute("aria-busy", String(!["collection", "analysis", "research"].includes(state.view)));
    try {
      const response = await request(`/api/overview?${queryString()}`, { signal: state.overviewController.signal });
      const data = await response.json();
      if (version !== state.overviewVersion) return;
      state.overview = data;
      state.overviewError = null;
      connection(true);
      renderOverview();
      navigate(state.view);
    } catch (error) {
      if (error.name === "AbortError" || version !== state.overviewVersion) return;
      connection(false);
      state.overviewError = error.message;
      $("page-error-message").textContent = error.message || "Please check that the local server is still running.";
      $("page-error").hidden = ["collection", "analysis", "research"].includes(state.view);
      if (!state.overview) {
        $("collection-list").replaceChildren();
        empty($("collection-empty"), "Saved product progress is unavailable", "Collection controls still work. Check the local database or server terminal, then refresh this section.", "info");
      }
    } finally {
      if (version === state.overviewVersion) {
        $("loading-panel").hidden = true;
        $("main-content").setAttribute("aria-busy", "false");
        $("export-button").disabled = !state.overview;
      }
    }
  }
  function renderOverview() {
    const data = state.overview, summary = data.summary;
    $("breadcrumb-workspace").textContent = data.dataset.name;
    $("nav-review-count").textContent = number(summary.total_reviews);
    $("nav-product-count").textContent = number(summary.product_count);
    const banner = $("provenance-banner");
    banner.classList.toggle("workspace-banner", !data.dataset.is_demo);
    const description = banner.querySelector("p");
    description.replaceChildren(node("strong", "", data.dataset.is_demo ? "Demo workspace" : "Your local workspace"), node("span", "", data.dataset.is_demo ? "You're exploring fictional reviews. Import or collect reviews to work with your own data." : "Reviews and saved reports are stored on this device. External analysis only runs when you choose it."));
    banner.querySelector(".badge").textContent = data.dataset.is_demo ? "SAMPLE DATA" : "LOCAL DATA";
    $("updated-label").textContent = data.last_updated ? `Data added ${dateLabel(data.last_updated)}` : "Stored on this device";
    renderStats(summary);
    renderDistribution(data);
    renderProductSnapshot(data.products);
    renderRecent(data.recent_reviews);
    renderProducts(data.products);
    renderCollection(data.checkpoints);
    const filter = $("product-filter");
    filter.replaceChildren(node("option", "", "All products")); filter.firstElementChild.value = "";
    for (const product of data.products) {
      const option = node("option", "", product.name || product.asin); option.value = product.asin; filter.append(option);
    }
    if (!data.products.some((p) => p.asin === state.filters.asin)) state.filters.asin = "";
    syncFilters();
    syncAnalysisProducts();
  }
  function renderStats(summary) {
    const specs = [
      ["Total reviews", number(summary.total_reviews), "", "Reviews in this workspace", "message"],
      ["Products", number(summary.product_count), "", "Unique product ASINs", "box"],
      ["Average rating", average(summary.average_rating), summary.average_rating === null ? "" : "/ 5", "Across reviews with a rating", "star"],
      ["Verified purchases", percent(summary.verified_share), "", `${plural(summary.verified_count, "review")} marked verified`, "shield"],
    ];
    $("stats-grid").replaceChildren(...specs.map(([label, value, suffix, caption, iconName]) => {
      const card = node("article", "stat-card");
      const heading = node("div", "stat-heading"); heading.append(node("span", "", label), icon(iconName));
      const metric = node("div", "stat-number", value); if (suffix) metric.append(node("small", "", suffix));
      card.append(heading, metric, node("p", "stat-caption", caption));
      return card;
    }));
  }
  function renderDistribution(data) {
    $("distribution-average").textContent = average(data.summary.average_rating);
    const totalRated = data.ratings.reduce((sum, row) => sum + Number(row.count), 0);
    $("distribution-total").textContent = totalRated === data.summary.total_reviews ? plural(data.summary.total_reviews, "review") : `${number(totalRated)} rated of ${plural(data.summary.total_reviews, "review")}`;
    $("distribution-stars").replaceChildren(...Array.from({ length: 5 }, (_, index) => icon("star", index < Math.round(Number(data.summary.average_rating) || 0) ? "" : "star-empty")));
    $("rating-bars").replaceChildren(...data.ratings.map((row) => {
      const bar = node("button", "rating-bar"); bar.type = "button";
      bar.setAttribute("aria-label", `${row.rating} stars, ${plural(row.count, "review")}. Show reviews.`);
      const label = node("span", "bar-label", row.rating); label.append(icon("star"));
      const track = node("span", "bar-track"), fill = node("span", "bar-fill");
      fill.style.width = `${totalRated ? Number(row.count) / totalRated * 100 : 0}%`;
      track.append(fill); bar.append(label, track, node("span", "bar-value", number(row.count)));
      bar.addEventListener("click", () => { resetFilters(); state.filters.rating = String(row.rating); syncFilters(); navigate("reviews", true); });
      return bar;
    }));
  }
  function productArt(index = 0) {
    const art = node("span", `product-art tone-${index % 3}`); art.append(icon("box")); return art;
  }
  function productName(asin) { return state.overview?.products.find((product) => product.asin === asin)?.name || asin || "Unknown product"; }
  function exploreProduct(asin) { resetFilters(); state.filters.asin = asin; syncFilters(); navigate("reviews", true); }
  function renderProductSnapshot(products) {
    const container = $("product-snapshot");
    container.replaceChildren(...products.slice(0, 3).map((product, index) => {
      const row = node("button", "snapshot-row"); row.type = "button"; row.setAttribute("aria-label", `Explore ${product.name || product.asin} reviews`);
      const main = node("span", "snapshot-main"); main.append(node("span", "product-name", product.name || product.asin), node("span", "product-id", product.asin));
      const score = node("span", "snapshot-score"), rating = node("span", "snapshot-rating"); rating.append(icon("star"), node("span", "", average(product.average_rating)));
      score.append(rating, node("span", "snapshot-reviews", plural(product.review_count, "review")));
      row.append(productArt(index), main, score, icon("chevron", "snapshot-arrow"));
      row.addEventListener("click", () => exploreProduct(product.asin)); return row;
    }));
    if (!products.length) {
      const placeholder = node("div", "empty-state compact");
      empty(placeholder, "A fresh start", "Import reviews to see your products here.", "box", { label: "Import reviews", run: openImport, icon: "upload" }); container.append(placeholder);
    }
  }
  function ratingPill(value) {
    const pill = node("span", "rating-pill");
    if (value !== "" && value !== null && value !== undefined) pill.append(icon("star"), node("span", "", average(value)));
    else pill.append(node("span", "muted", "N/A"));
    return pill;
  }
  function verifiedLabel() { const label = node("span", "verified-label", "Verified"); label.prepend(icon("shield")); return label; }
  function reviewRow(review, full = false) {
    const row = node("tr");
    const reviewCell = node("td");
    const title = button(review.review_title || "Untitled review", "review-title-button", () => showReview(review));
    reviewCell.append(title, node("span", "review-preview", review.review_body || "No review text provided."));
    if (full) {
      const byline = node("span", "reviewer-line"); byline.append(node("span", "", review.reviewer_name || "Anonymous reviewer"));
      if (isVerified(review.verified_purchase)) byline.append(verifiedLabel()); reviewCell.append(byline);
    }
    const productCell = node("td"); productCell.append(node("span", "table-product", productName(review.asin)));
    const ratingCell = node("td"); ratingCell.append(ratingPill(review.rating));
    const dateCell = node("td"); dateCell.append(node("span", "table-date", dateLabel(review.review_date)));
    const actionCell = node("td"), action = button("", "icon-button review-open", () => showReview(review), "chevron");
    action.setAttribute("aria-label", `Open review: ${review.review_title || review.review_id}`); actionCell.append(action);
    row.append(reviewCell, productCell, ratingCell, dateCell, actionCell); return row;
  }
  function renderRecent(reviews) {
    $("recent-review-body").replaceChildren(...reviews.slice(0, 4).map((review) => reviewRow(review)));
    $("recent-empty").hidden = reviews.length > 0;
    $("recent-review-body").closest(".table-scroll").hidden = !reviews.length;
    if (!reviews.length) empty($("recent-empty"), "Your first review belongs here", "Add a CSV to start exploring your product feedback.", "message", { label: "Import reviews", run: openImport, icon: "upload" });
  }
  function renderProducts(products) {
    $("products-count").textContent = plural(products.length, "product");
    $("products-grid").replaceChildren(...products.map((product, index) => {
      const card = node("article", "product-card"), top = node("div", "product-card-top");
      top.append(productArt(index), node("span", "badge", state.overview.dataset.is_demo ? "DEMO PRODUCT" : "PRODUCT"));
      const metrics = node("div", "product-card-metrics");
      [[number(product.review_count), "Total reviews"], [average(product.average_rating), "Average rating / 5"]].forEach(([value, label]) => {
        const metric = node("div"); metric.append(node("strong", "", value), node("span", "", label)); metrics.append(metric);
      });
      const foot = node("div", "product-card-foot");
      foot.append(node("span", "muted", `${number(product.verified_count)} verified`), button("Explore reviews", "text-button", () => exploreProduct(product.asin), "arrow"));
      card.append(top, node("h2", "", product.name || product.asin), node("span", "product-id", product.asin), metrics, foot); return card;
    }));
    $("products-empty").hidden = products.length > 0;
    if (!products.length) empty($("products-empty"), "Make room for your products", "Products appear here as soon as you import their reviews.", "box", { label: "Import reviews", run: openImport, icon: "upload" });
  }
  function renderCollection(checkpoints) {
    $("collection-list").replaceChildren(...checkpoints.map((checkpoint) => {
      const card = node("article", "panel checkpoint-card"), header = node("div", "checkpoint-header");
      const status = String(checkpoint.status || "unknown");
      const badge = node("span", "badge", status.replaceAll("_", " ").toUpperCase());
      if (["complete", "zero_reviews", "failed", "blocked", "partial", "paused", "in_progress"].includes(status)) badge.classList.add(`status-${status}`);
      header.append(icon("layers"), node("h3", "", productName(checkpoint.asin)), badge);
      const details = node("dl", "checkpoint-grid");
      const values = [["Product ASIN", checkpoint.asin], ["Pages collected", checkpoint.pages_completed ?? 0], ["Last saved", dateLabel(checkpoint.updated_at)], ["Next page", checkpoint.next_url || "None saved"]];
      for (const [label, value] of values) { const item = node("div"); item.append(node("dt", "", label), node("dd", "", value ?? "Unavailable")); details.append(item); }
      card.append(header, details);
      const message = checkpoint.detail;
      if (message) card.append(node("p", "checkpoint-message", message));
      return card;
    }));
    $("collection-empty").hidden = checkpoints.length > 0;
    $("checkpoint-description").textContent = `Collection positions in ${state.overview.dataset.name}`;
    if (!checkpoints.length) empty($("collection-empty"), "No saved product positions", state.overview.dataset.is_demo ? "Demo reviews are fictional. Live collection saves product progress in My workspace." : "Each collected page saves a position here. CSV imports do not create collection checkpoints.", "layers");
  }
  async function loadReviews() {
    state.reviewController?.abort();
    state.reviewController = new AbortController();
    const version = ++state.reviewVersion;
    $("reviews-error").hidden = true; $("reviews-loading").hidden = false;
    $("reviews-table-wrap").hidden = true; $("reviews-empty").hidden = true; $("pagination").hidden = true;
    $("results-count").textContent = "Loading reviews...";
    $("view-reviews").setAttribute("aria-busy", "true"); syncFilters();
    try {
      const response = await request(`/api/reviews?${queryString(true)}`, { signal: state.reviewController.signal });
      const data = await response.json();
      if (version !== state.reviewVersion) return;
      state.reviews = data; state.filters.page = data.page;
      $("results-count").textContent = `${plural(data.total, "review")} found`;
      $("reviews-body").replaceChildren(...data.items.map((review) => reviewRow(review, true)));
      $("reviews-table-wrap").hidden = !data.items.length;
      if (!data.items.length) {
        const filtered = state.filters.q || state.filters.asin || state.filters.rating !== "all" || state.filters.verified !== "all";
        empty($("reviews-empty"), filtered ? "No reviews match just yet" : "Your workspace is ready", filtered ? "Try another search or clear your filters to see all reviews." : "Import a review CSV to start exploring feedback.", "search", filtered ? { label: "Clear filters", run: () => { resetFilters(); loadReviews(); } } : { label: "Import reviews", run: openImport, icon: "upload" });
      }
      if (data.total > 0) {
        $("pagination").hidden = false;
        const first = (data.page - 1) * data.page_size + 1, last = Math.min(data.page * data.page_size, data.total);
        $("page-range").textContent = `Showing ${number(first)} to ${number(last)} of ${number(data.total)} reviews`;
        $("page-number").textContent = `${data.page} / ${Math.max(1, data.pages)}`;
        $("previous-page").disabled = data.page <= 1;
        $("next-page").disabled = data.page >= data.pages;
      }
      connection(true);
    } catch (error) {
      if (error.name === "AbortError" || version !== state.reviewVersion) return;
      $("reviews-error-message").textContent = error.message || "We couldn't load these reviews.";
      $("reviews-error").hidden = false; $("results-count").textContent = "Reviews unavailable"; connection(false);
    } finally {
      if (version === state.reviewVersion) { $("reviews-loading").hidden = true; $("view-reviews").setAttribute("aria-busy", "false"); }
    }
  }
  function showReview(review, dataset = state.dataset) {
    const container = $("review-detail"); container.replaceChildren();
    const product = node("div", "detail-product"); product.append(icon("box"), node("span", "", productName(review.asin)));
    const title = node("h2", "", review.review_title || "Untitled review"); title.id = "detail-title";
    const rating = node("div", "detail-rating"); rating.append(ratingPill(review.rating)); if (isVerified(review.verified_purchase)) rating.append(verifiedLabel());
    const byline = node("div", "detail-byline"); byline.append(node("span", "", review.reviewer_name || "Anonymous reviewer"), node("span", "", "·"), node("span", "", dateLabel(review.review_date)));
    const body = node("p", "review-body", review.review_body || "No review text provided.");
    const details = node("dl", "detail-meta");
    const id = node("div"), idValue = node("dd", "id-copy"), copy = button("", "icon-button", () => copyReviewId(review.review_id), "copy"); copy.setAttribute("aria-label", "Copy review ID");
    idValue.append(node("code", "", review.review_id), copy); id.append(node("dt", "", "Review ID"), idValue); details.append(id);
    for (const [label, value] of [["Product ASIN", review.asin], ["Source", review.source || "Imported review"], ["Added to workspace", dateLabel(review.created_at)], ["Purchase verification", isVerified(review.verified_purchase) ? "Marked verified in source data" : review.verified_purchase === "" || review.verified_purchase == null ? "Not provided" : "Not marked verified"]]) {
      const field = node("div"); field.append(node("dt", "", label), node("dd", "", value)); details.append(field);
    }
    container.append(product, title, rating, byline, body, details);
    if (dataset === "demo") container.append(node("p", "detail-demo-note", "This is a fictional review created for the demo. It is not customer evidence."));
    else {
      try {
        const url = new URL(review.product_url);
        if (url.protocol === "https:" && ["www.amazon.in", "amazon.in"].includes(url.hostname) && !url.username && !url.password) {
          const link = node("a", "detail-link", "View product on Amazon"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; link.append(icon("external")); container.append(link);
        }
      } catch { /* Omit invalid source links. */ }
    }
    $("review-dialog").showModal();
  }
  async function copyReviewId(id) {
    try { await navigator.clipboard.writeText(String(id)); toast("Review ID copied."); }
    catch { toast("Couldn't copy automatically. You can select the review ID above."); }
  }
  function openImport() {
    if (state.importing) return;
    $("import-result").hidden = true;
    $("import-dialog").showModal();
  }
  function chooseFile(file) {
    if (state.importing) return;
    state.file = file || null;
    $("import-result").hidden = true;
    $("file-label").textContent = file ? file.name : "Choose a CSV or drop it here";
    $("file-description").textContent = file ? `${number(Math.ceil(file.size / 1024))} KB · Click to choose another file` : "UTF-8 CSV, up to 5 MB";
  }
  function importMessage(message, error = false) {
    $("import-result").textContent = message;
    $("import-result").classList.toggle("error", error);
    $("import-result").hidden = false;
  }
  async function importFile(event) {
    event.preventDefault();
    if (state.importing) return;
    const file = state.file;
    if (!file) { importMessage("Choose a CSV file to import.", true); return; }
    if (!file.name.toLowerCase().endsWith(".csv")) { importMessage("Please choose a file ending in .csv.", true); return; }
    if (file.size > 5 * 1024 * 1024) { importMessage("This file is larger than 5 MB. Split it into smaller CSV files and try again.", true); return; }
    if (!file.size) { importMessage("This file is empty. Choose a CSV containing review rows.", true); return; }
    state.importing = true;
    $("import-submit").disabled = true; $("csv-file").disabled = true; $("import-cancel").disabled = true;
    $("import-submit").lastElementChild.textContent = "Importing...";
    importMessage("Validating your CSV and checking for duplicate reviews...");
    try {
      const response = await request("/api/import?dataset=workspace", { method: "POST", headers: { "Content-Type": "text/csv", "X-Reviewdesk-Request": "1" }, body: file });
      const data = await response.json();
      const message = `${plural(data.inserted, "review")} imported. ${plural(data.duplicates, "duplicate")} skipped. ${plural(data.total_reviews, "review")} now in My workspace.`;
      importMessage(message);
      state.dataset = "workspace"; $("dataset-select").value = "workspace"; savePreference("reviewdesk-dataset", "workspace");
      $("analysis-consent").checked = false; $("research-consent").checked = false; $("analysis-product").value = "";
      updateWorkflowControls();
      resetFilters();
      state.file = null; $("csv-file").value = "";
      $("file-label").textContent = "Choose another CSV or drop it here"; $("file-description").textContent = "UTF-8 CSV, up to 5 MB";
      await loadOverview();
      loadAnalysis(); loadAnalysisPlan(); renderResearch();
      toast(message);
    } catch (error) { importMessage(error.message || "We couldn't import this CSV. Please try again.", true); }
    finally {
      state.importing = false;
      $("import-submit").disabled = false; $("csv-file").disabled = false; $("import-cancel").disabled = false;
      $("import-submit").lastElementChild.textContent = "Import reviews";
    }
  }
  async function exportCsv() {
    if (state.exporting || !state.overview) return;
    state.exporting = true; $("export-button").disabled = true;
    const label = $("export-button").querySelector("span"); label.textContent = "Exporting...";
    const dataset = state.dataset;
    try {
      const params = queryString(state.view === "reviews"); params.delete("page"); params.delete("page_size");
      const response = await request(`/api/export?${params}`), blob = await response.blob();
      const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = `reviewdesk-${dataset}-reviews.csv`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
      toast("Your spreadsheet-safe CSV is ready.");
    } catch (error) { toast(error.message || "We couldn't export your reviews. Please try again."); }
    finally { state.exporting = false; $("export-button").disabled = !state.overview; label.textContent = "Export CSV"; }
  }
  const activeStatuses = new Set(["queued", "running", "cancelling"]);
  const retryStatuses = new Set(["paused", "blocked", "failed", "cancelled", "interrupted"]);
  const jobLabels = { collection: "Review collection", analysis: "Review analysis", auth: "Manual sign-in", research: "Review research" };
  function messageAt(id, message) {
    $(id).textContent = message || "";
    $(id).hidden = !message;
  }
  function timeLabel(value) {
    if (!value) return "Just now";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }
  function syncAnalysisProducts() {
    const products = state.overview?.products || [];
    const key = JSON.stringify(products.map((product) => [product.asin, product.name]));
    for (const id of ["analysis-product", "research-product"]) {
      const select = $(id), selected = select.value;
      if (select.dataset.products === key) continue;
      select.dataset.products = key;
      const all = node("option", "", "All products"); all.value = "";
      select.replaceChildren(all);
      for (const product of products) {
        const option = node("option", "", product.name || product.asin);
        option.value = product.asin; select.append(option);
      }
      select.value = products.some((product) => product.asin === selected) ? selected : "";
    }
  }
  function updateWorkflowControls() {
    const data = state.automation, active = data?.active_job;
    const busy = Boolean(active && activeStatuses.has(active.status));
    const browserReady = Boolean(data?.capabilities?.playwright_installed);
    const sessionReady = data?.auth?.status === "saved";
    $("auth-start").disabled = !data || state.mutation || busy || !browserReady;
    $("auth-start").lastElementChild.textContent = sessionReady ? "Refresh sign-in" : "Open sign-in";
    const canSave = active?.kind === "auth" && active.status === "running" && active.phase === "awaiting_login";
    $("auth-save").hidden = !canSave;
    $("auth-save").disabled = !canSave || state.mutation;
    $("collection-submit").disabled = !data || state.mutation || busy || !browserReady || !sessionReady;
    $("collection-submit").lastElementChild.textContent = state.mutation ? "Please wait..." : "Start collection";
    const configured = Boolean(data?.capabilities?.openai_configured), router = routerStatus();
    const routerReady = router.configured;
    $("openai-option").disabled = !configured;
    $("openai-option").textContent = configured ? `OpenAI${data.capabilities.model ? ` (${data.capabilities.model})` : ""}` : "OpenAI, not configured";
    if (!configured && $("analysis-method").value === "openai") $("analysis-method").value = "local";
    const method = $("analysis-method").value, external = method !== "local", useRouter = method === "openrouter";
    $("analysis-consent-wrap").hidden = !external;
    $("analysis-depth-wrap").hidden = !useRouter;
    $("analysis-provider-note").hidden = !useRouter || routerReady;
    $("analysis-provider-note").firstElementChild.textContent = router.disabled ? "Free AI requests are disabled after a provider cost signal. Check AI settings and the provider account." : "Add your OpenRouter key to use free model analysis.";
    $("analysis-method-help").textContent = useRouter ? "Free model analysis with a bounded call budget and saved chunk results. Broader coverage can take several runs." : external ? "OpenAI interprets the selected review text. Check source evidence and review the provider's output." : "Local analysis groups common terms and uses review ratings. It does not call a language model.";
    $("analysis-consent-label").textContent = useRouter ? "Send the selected review text to OpenRouter" : "Send the selected review text to OpenAI";
    $("analysis-consent-detail").textContent = useRouter ? "Free models only, with no paid fallback. Review text leaves this device when you start the run." : "Uses your configured API key and may incur charges. Review text is shared with the provider.";
    const ready = useRouter ? routerReady : configured;
    $("analysis-submit").disabled = !data || state.mutation || busy || (external && (!ready || !$("analysis-consent").checked)) || (useRouter && (state.planLoading || !state.plan || Boolean(state.planError)));
    $("analysis-submit").lastElementChild.textContent = state.mutation ? "Please wait..." : "Run analysis";
    $("browser-setup").hidden = !data || browserReady;
    $("jobs-live-label").hidden = !busy;
    $("research-submit").disabled = !data || state.mutation || busy;
    $("research-submit").lastElementChild.textContent = state.mutation ? "Please wait..." : "Ask reviews";
    const local = isLocalQuestion($("research-question").value);
    $("research-consent").disabled = !routerReady || local;
    $("research-consent-wrap").hidden = local;
    $("research-help").textContent = local ? "This exact statistics question runs locally. No model call or API key is needed." : "Matching passages are found locally first. Without sharing, you get local evidence only. Optional focused AI uses at most one call; agent investigation uses up to four. If AI is unavailable, local sources stay available.";
    $("research-provider-note").hidden = routerReady;
    $("research-provider-note").firstElementChild.textContent = router.disabled ? "AI is disabled after a cost signal. Local statistics and evidence search still work." : "Local evidence search works without a key. Add OpenRouter for optional AI interpretation.";
    const providerBusy = busy && (active.kind === "research" || active.options?.analysis_method === "openrouter");
    $("provider-save").disabled = state.providerSaving || state.providerChecking || providerBusy;
    $("provider-key").disabled = state.providerSaving || state.providerChecking || providerBusy || state.provider?.key_source === "environment";
    $("provider-zdr").disabled = state.providerSaving || state.providerChecking || providerBusy;
    $("provider-response-mode").disabled = state.providerSaving || state.providerChecking || providerBusy;
    $("provider-remember").disabled = state.providerSaving || state.providerChecking || providerBusy || !state.provider?.remember_available || state.provider?.key_source === "environment";
    $("provider-clear").disabled = state.providerSaving || state.providerChecking || providerBusy || !["session", "saved"].includes(state.provider?.key_source);
    $("provider-check").disabled = state.providerSaving || state.providerChecking || providerBusy || !["session", "saved", "environment"].includes(state.provider?.key_source);
    $("allowance-refresh").disabled = $("provider-check").disabled;
    $("provider-check").textContent = state.providerChecking ? "Checking account..." : "Check OpenRouter quota";
  }
  function routerStatus() {
    const capabilities = state.automation?.capabilities;
    const disabled = Boolean(state.provider?.disabled || capabilities?.openrouter_disabled);
    return { disabled, configured: !disabled && Boolean(capabilities?.openrouter_configured ?? state.provider?.configured) };
  }
  function jobAction(label, job, action, iconName) {
    const control = button(label, "button button-white button-small", action, iconName);
    control.dataset.jobId = job.id; control.dataset.jobAction = label;
    control.disabled = state.mutation;
    return control;
  }
  function jobCard(job, compact = false) {
    const card = node("article", `panel job-card${compact ? " compact-job" : ""}`);
    const header = node("div", "job-header"), identity = node("div", "job-identity");
    const mark = node("span", "job-mark"); mark.append(icon(job.kind === "analysis" ? "spark" : job.kind === "research" ? "search" : job.kind === "auth" ? "shield" : "layers"));
    const copy = node("div"); copy.append(node("h3", "", jobLabels[job.kind] || "Background job"), node("p", "job-date", timeLabel(job.created_at)));
    identity.append(mark, copy);
    const badge = node("span", `badge job-status status-${job.status}`, String(job.status || "unknown").replaceAll("_", " ").toUpperCase());
    header.append(identity, badge); card.append(header);
    const isActive = activeStatuses.has(job.status), progress = job.progress || {};
    if (isActive) {
      const phase = node("div", "job-phase");
      if (job.status !== "cancelling") phase.append(node("span", "spinner"));
      phase.append(node("strong", "", job.status === "cancelling" ? "Stopping after the current operation" : String(job.phase || "Waiting to start").replaceAll("_", " ")));
      if (progress.current_asin) phase.append(node("code", "", progress.current_asin));
      card.append(phase);
    }
    if (job.kind === "collection") {
      const metrics = node("dl", "job-metrics");
      const values = [["Products finished", `${number(progress.products_done)} / ${number(progress.products_total || job.options?.urls?.length)}`], ["Pages saved", number(progress.pages_processed)], ["New reviews", number(progress.reviews_inserted)]];
      for (const [label, value] of values) { const item = node("div"); item.append(node("dd", "", value), node("dt", "", label)); metrics.append(item); }
      card.append(metrics);
    }
    if (job.kind === "analysis" || job.kind === "research" || job.phase === "analysing") {
      const values = [];
      if (progress.chunks_done != null && progress.chunks_total != null) values.push(["Text chunks processed", `${number(progress.chunks_done)} / ${number(progress.chunks_total)}`]);
      if (progress.calls != null) values.push(["Model calls", `${number(progress.calls)}${progress.max_calls != null ? ` / ${number(progress.max_calls)}` : ""}`]);
      if (progress.chunks_cached != null) values.push(["Cached chunks", number(progress.chunks_cached)]);
      if (progress.tools_used != null) values.push(["Data lookups", number(progress.tools_used)]);
      if (values.length) {
        const metrics = node("dl", "job-metrics");
        for (const [label, value] of values) { const item = node("div"); item.append(node("dd", "", value), node("dt", "", label)); metrics.append(item); }
        card.append(metrics);
      }
    }
    if (job.kind === "research" && job.options?.question) card.append(node("p", "job-question", job.options.question));
    if (job.detail) card.append(node("p", "job-detail", job.detail));
    if (job.error && job.error !== job.detail) card.append(node("p", "job-error", job.error));
    if (job.result?.retry_after != null && Number.isFinite(Number(job.result.retry_after))) card.append(node("p", "job-detail", `Provider retry guidance: wait at least ${number(Math.ceil(Number(job.result.retry_after)))} seconds before trying again. No automatic retry or paid fallback will run.`));
    const diagnostics = describeProviderDiagnostics(job.result?.diagnostics || (job.result?.error_code === "rate_limit" ? {rate_limit_source: "unknown"} : null));
    if (diagnostics) card.append(node("p", "job-detail", diagnostics));
    const actions = node("div", "job-actions");
    if (isActive && job.kind === "auth" && job.status === "running" && job.phase === "awaiting_login") {
      const note = node("p", "field-help", "Complete sign-in in the opened browser, then save the session here."); card.append(note);
      actions.append(jobAction("Save signed-in session", job, () => mutateJob(job, "finish"), "check"));
    }
    if (isActive) {
      const stop = jobAction(job.status === "cancelling" ? "Stopping..." : "Stop job", job, () => mutateJob(job, "cancel"), "close");
      stop.disabled = state.mutation || job.status === "cancelling"; actions.append(stop);
    } else if (retryStatuses.has(job.status) && job.kind === "collection") {
      actions.append(jobAction("Resume collection", job, () => fillCollection(job), "refresh"));
    }
    if (!isActive && job.result?.analysis_id) actions.append(jobAction("View analysis", job, () => {
      const dataset = job.options?.dataset || "workspace";
      if (["demo", "workspace"].includes(dataset) && dataset !== state.dataset) chooseDataset(dataset);
      $("analysis-product").value = job.options?.asin || "";
      navigate("analysis", true); loadAnalysis();
    }, "arrow"));
    if (!isActive && job.kind === "research") actions.append(jobAction(job.status === "succeeded" ? "View answer" : "Review question", job, () => {
      if (job.options?.dataset && job.options.dataset !== state.dataset) chooseDataset(job.options.dataset);
      $("research-product").value = job.options?.asin || "";
      $("research-question").value = job.options?.question || "";
      $("research-consent").checked = false;
      navigate("research", true); renderResearch();
    }, "arrow"));
    if (actions.children.length) card.append(actions);
    return card;
  }
  function preserveJobFocus(container, render) {
    const focused = container.contains(document.activeElement) ? document.activeElement : null;
    const id = focused?.dataset.jobId, action = focused?.dataset.jobAction;
    render();
    if (id && action) [...container.querySelectorAll("button[data-job-id]")].find((control) => control.dataset.jobId === id && control.dataset.jobAction === action && !control.disabled)?.focus({ preventScroll: true });
  }
  function renderAutomation() {
    const data = state.automation;
    if (!data) return;
    const auth = data.auth || {}, status = auth.status || "missing";
    $("auth-status").textContent = status === "saved" ? "SESSION SAVED" : status.toUpperCase();
    $("auth-status").className = `badge auth-${status}`;
    $("auth-detail").textContent = auth.detail || (status === "saved" ? "A session is saved on this device. Collection checks whether Amazon still accepts it." : "Open sign-in to save an Amazon session on this device.");
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    const active = data.active_job;
    if (active?.kind === "auth") {
      const ready = active.status === "running" && active.phase === "awaiting_login";
      $("auth-status").textContent = ready ? "WAITING FOR YOU" : active.status === "cancelling" ? "STOPPING" : "OPENING SIGN-IN";
      $("auth-detail").textContent = ready ? "1. Sign in in the Amazon window. 2. Return here and save your session." : active.status === "cancelling" ? active.detail || "Stopping sign-in. The previous session will be retained." : "Opening the Amazon sign-in window. Wait until it appears before signing in.";
    } else if (status !== "saved") {
      const latestAuth = jobs.find((job) => job.kind === "auth");
      if (latestAuth && ["failed", "paused", "blocked", "cancelled", "interrupted"].includes(latestAuth.status) && latestAuth.detail) $("auth-detail").textContent = latestAuth.detail;
    }
    const displayed = active ? [active, ...jobs.filter((job) => job.id !== active.id)].slice(0, 8) : jobs.slice(0, 8);
    const key = JSON.stringify([displayed, state.mutation]);
    if (state.jobRenderKey !== key) {
      state.jobRenderKey = key;
      preserveJobFocus($("job-list"), () => $("job-list").replaceChildren(...displayed.map((job) => jobCard(job))));
    }
    $("jobs-empty").hidden = displayed.length > 0;
    if (!displayed.length) empty($("jobs-empty"), "Ready when you are", "Your sign-in, collection, and analysis jobs will appear here.", "layers");
    const analysisJob = active?.kind === "analysis" || active?.phase === "analysing" ? active : null;
    $("analysis-job").hidden = !analysisJob;
    const analysisKey = JSON.stringify([analysisJob, state.mutation]);
    if (state.analysisJobKey !== analysisKey) {
      state.analysisJobKey = analysisKey;
      preserveJobFocus($("analysis-job"), () => $("analysis-job").replaceChildren(...(analysisJob ? [jobCard(analysisJob, true)] : [])));
    }
    updateWorkflowControls();
    renderResearch();
    renderAllowance();
  }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (document.hidden) return;
    const busy = state.automation?.active_job && activeStatuses.has(state.automation.active_job.status);
    state.pollTimer = setTimeout(loadAutomation, busy ? 2000 : 10000);
  }
  async function loadAutomation() {
    clearTimeout(state.pollTimer);
    if (state.automationPromise) {
      state.automationPending = true;
      return state.automationPromise;
    }
    state.automationPending = false;
    const operation = readAutomation();
    state.automationPromise = operation;
    try { await operation; }
    finally {
      state.automationPromise = null;
      const again = state.automationPending;
      state.automationPending = false;
      if (again && !document.hidden) await loadAutomation();
      else schedulePoll();
    }
  }
  async function readAutomation() {
    state.automationController?.abort();
    state.automationController = new AbortController();
    const version = ++state.automationVersion;
    try {
      const response = await request("/api/automation", { signal: state.automationController.signal });
      const data = await response.json();
      if (version !== state.automationVersion) return;
      const jobs = [...(data.jobs || []), ...(data.active_job ? [data.active_job] : [])];
      let refreshData = false, refreshAnalysis = false, refreshProvider = false;
      for (const job of jobs) {
        const previous = state.jobStatuses.get(job.id);
        if (previous && activeStatuses.has(previous.status) && !activeStatuses.has(job.status)) {
          toast(`${jobLabels[job.kind] || "Job"}: ${String(job.status).replaceAll("_", " ")}.`);
          refreshData ||= job.kind === "collection";
          refreshAnalysis ||= job.kind === "analysis" || Boolean(job.result?.analysis_id);
          refreshProvider ||= job.kind === "research" || job.options?.analysis_method === "openrouter";
        }
        if (job.kind === "collection" && previous && previous.inserted !== job.progress?.reviews_inserted) refreshData = true;
        state.jobStatuses.set(job.id, { status: job.status, inserted: job.progress?.reviews_inserted });
      }
      const activityChanged = (state.automation?.jobs || []).map((job) => `${job.id}:${job.status}`).join("|") !== (data.jobs || []).map((job) => `${job.id}:${job.status}`).join("|");
      state.automation = data;
      $("automation-error-message").textContent = ""; $("automation-error").hidden = true;
      renderAutomation();
      if (refreshData && state.dataset === "workspace") {
        $("analysis-consent").checked = false; $("research-consent").checked = false; updateWorkflowControls();
        await loadOverview(true);
        loadAnalysisPlan();
        if (state.view === "reviews") loadReviews();
      }
      if (refreshAnalysis) { loadAnalysis(); loadAnalysisPlan(); }
      else if (activityChanged && state.view === "analysis" && state.analysis?.report) renderAnalysis();
      if (refreshProvider) { await loadProvider(); checkProviderQuota(); }
    } catch (error) {
      if (error.name === "AbortError" || version !== state.automationVersion) return;
      $("automation-error-message").textContent = error.message;
      $("automation-error-message").hidden = false;
      $("automation-error").hidden = false;
      $("jobs-live-label").hidden = true;
    }
  }
  async function postJson(url, payload) {
    const response = await request(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Reviewdesk-Request": "1" }, body: JSON.stringify(payload) });
    return response.json();
  }
  function providerMessage(message, error = false) {
    messageAt("provider-result", message);
    $("provider-result").classList.toggle("error", error);
  }
  function describeProviderDiagnostics(details) {
    if (!details || typeof details !== "object") return "";
    const parts = [];
    const routing = { privacy_policy: "Reported routing reason: data policy.", required_parameters: "Reported routing reason: required parameters.", region_policy: "Reported routing reason: region policy.", model_unavailable: "Reported routing reason: model unavailable.", no_matching_endpoint: "Reported routing reason: no matching endpoint; the excluding requirement was not identified." };
    if (Object.hasOwn(routing, details.routing_reason)) parts.push(routing[details.routing_reason]);
    if (details.rate_limit_source === "provider") parts.push("The response identifies the model provider as the source.");
    else if (details.rate_limit_source === "openrouter") parts.push("OpenRouter supplied platform rate-limit headers.");
    else if (details.rate_limit_source === "unknown") parts.push("The response did not identify the rate-limit source.");
    if (Number.isInteger(details.http_status)) parts.push(`HTTP status: ${details.http_status}.`);
    if (Number.isInteger(details.error_status) && details.error_status !== details.http_status) parts.push(`Error inside the response: ${details.error_status}.`);
    if (Number.isInteger(details.limit)) parts.push(`Reported limit: ${number(details.limit)}.`);
    if (Number.isInteger(details.remaining)) parts.push(`Reported remaining: ${number(details.remaining)}.`);
    if (Number.isInteger(details.reset_at)) parts.push(`Reported reset: ${new Date(details.reset_at * 1000).toLocaleString()}.`);
    return parts.join(" ");
  }
  async function checkProviderQuota() {
    if (state.providerChecking || state.providerSaving || !["session", "saved", "environment"].includes(state.provider?.key_source)) return;
    state.providerChecking = true; updateWorkflowControls();
    const output = $("provider-quota"); output.hidden = false;
    output.replaceChildren(node("p", "field-help", "Reading OpenRouter account counters. No review text is sent."));
    try {
      const result = await postJson("/api/provider/check", {}), quota = result.quota || {};
      state.providerQuota = result; state.providerQuotaError = false; renderAllowance();
      output.replaceChildren(node("strong", "", "OpenRouter account allowance"));
      if (quota.available) {
        const counts = node("dl", "provider-quota-counts");
        for (const [label, value] of [["Used", quota.used], ["Daily limit", quota.limit], ["Remaining", quota.remaining]]) {
          const item = node("div"); item.append(node("dt", "", label), node("dd", "", number(value))); counts.append(item);
        }
        output.append(counts, node("p", "field-help", `Daily reset: ${new Date(result.resets_at * 1000).toLocaleString()}.`));
      } else output.append(node("p", "field-help", "Connection verified. OpenRouter did not supply its free-request counter. This does not mean the allowance is zero."));
      if (Number.isInteger(result.app_daily_remaining)) output.append(node("p", "field-help", `Reviewdesk separately has ${number(result.app_daily_remaining)} of ${number(result.app_daily_limit)} local attempts left. Failed and uncertain attempts count here.`));
      output.append(node("p", "field-help", "Daily allowance does not measure per-minute limits or the model provider's available capacity."),
                    node("p", "field-help", `Checked ${new Date(result.checked_at * 1000).toLocaleString()}. No AI response was generated.`));
    } catch (error) { output.replaceChildren(node("p", "form-message error", error.message)); state.providerQuotaError = true; renderAllowance(); }
    finally { state.providerChecking = false; updateWorkflowControls(); }
  }
  function renderAllowance() {
    const result = state.providerQuota, quota = result?.quota;
    const freshDay = result && Date.now() < Number(result.resets_at) * 1000;
    $("allowance-summary").textContent = quota?.available && freshDay ? `${number(quota.remaining)} / ${number(quota.limit)} AI requests left` : "AI allowance not checked";
    const local = Number.isInteger(result?.app_daily_remaining) ? ` Reviewdesk budget: ${number(result.app_daily_remaining)} attempts left.` : "";
    $("allowance-detail").textContent = result && freshDay ? `Checked ${new Date(result.checked_at * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}.${local} Refresh for the latest count.` : state.provider?.configured ? "Refresh to check OpenRouter. This uses no model requests." : "Connect a key to check remaining requests.";
    if (state.providerQuotaError) $("allowance-detail").textContent = "The last check failed. Any count above is from an earlier check. Refresh to try again; no model call was made.";
    if (state.provider?.retry_at && Date.now() < state.provider.retry_at * 1000) $("allowance-detail").textContent += ` Free AI paused until ${new Date(state.provider.retry_at * 1000).toLocaleTimeString()}; local evidence still works.`;
  }
  function renderProvider() {
    const provider = state.provider;
    if (!provider) return;
    $("provider-status-label").textContent = provider.disabled ? "REQUESTS DISABLED" : provider.configured ? "CONFIGURED" : "NOT CONNECTED";
    $("provider-sidebar-status").textContent = provider.disabled ? "Check account" : provider.configured ? "Free models" : "Not connected";
    $("provider-source-label").textContent = provider.key_source === "environment" ? "The key is managed in the server environment. Change it there and restart the server to replace it." : provider.key_source === "saved" ? "The key is saved in Windows Credential Manager for this Windows account and reconnects after restart. Its value is never returned to this page." : provider.key_source === "session" ? "A key is held in local server memory. Choose Remember on this Windows account to keep it after restart." : provider.storage_error ? "Windows could not read the saved key. Reconnect it here or use a session-only key." : "Add a key to connect. Nothing is saved to browser storage.";
    if (provider.disabled) $("provider-source-label").textContent = "A provider reported a nonzero cost. Further AI requests are disabled. Check the provider account before reconnecting or restarting the server.";
    if (!state.providerPolicyDirty) {
      $("provider-zdr").checked = Boolean(provider.require_zdr);
      $("provider-response-mode").value = provider.response_mode === "prompt" ? "prompt" : "schema";
      $("provider-remember").checked = Boolean(provider.remember_key);
    }
    $("provider-remember-wrap").hidden = !provider.remember_available || provider.key_source === "environment";
    $("provider-budget-label").textContent = `App limit: up to ${number(provider.daily_limit)} model calls per day. Free model availability and provider limits may vary. Saving settings makes no model request.`;
    $("provider-save").lastElementChild.textContent = provider.configured ? "Save settings" : "Use this key";
    updateWorkflowControls();
    renderAllowance();
  }
  async function loadProvider() {
    const version = ++state.providerVersion;
    try {
      const response = await request("/api/provider"), data = await response.json();
      if (version !== state.providerVersion) return;
      state.provider = { configured: Boolean(data.configured), key_source: data.key_source, model: data.model, free_only: data.free_only, privacy: data.privacy, daily_limit: data.daily_limit, require_zdr: Boolean(data.require_zdr), response_mode: data.response_mode, disabled: Boolean(data.disabled), remember_key: Boolean(data.remember_key), remember_available: Boolean(data.remember_available), storage_error: Boolean(data.storage_error), retry_at: data.retry_at };
      state.providerQuota = data.quota_snapshot ? {...data.quota_snapshot, app_daily_remaining: data.app_daily_remaining} : null;
      renderProvider();
    } catch (error) {
      if (version !== state.providerVersion) return;
      $("provider-status-label").textContent = "UNAVAILABLE";
      $("provider-sidebar-status").textContent = "Check setup";
      providerMessage(error.message, true);
    }
  }
  function openSettings() {
    $("provider-key").value = ""; state.providerPolicyDirty = false; providerMessage(""); setMenu(false);
    $("settings-dialog").showModal(); loadProvider();
  }
  async function saveProvider(event, clear = false) {
    event?.preventDefault();
    if (state.providerSaving || state.providerChecking) return;
    const key = $("provider-key").value.trim();
    if (!clear && !key && !state.provider?.configured) { providerMessage("Paste an OpenRouter key to connect free model analysis.", true); return; }
    state.providerSaving = true; updateWorkflowControls(); providerMessage("Updating local server settings...");
    const payload = { require_zdr: $("provider-zdr").checked, response_mode: $("provider-response-mode").value };
    if (clear) payload.clear = true;
    else {
      if (key) payload.api_key = key;
      if (state.provider?.remember_available && state.provider?.key_source !== "environment") payload.remember_key = $("provider-remember").checked;
    }
    $("provider-key").value = "";
    try {
      await postJson("/api/provider", payload);
      state.providerQuotaError = false;
      $("provider-quota").hidden = true; $("provider-quota").replaceChildren();
      state.providerPolicyDirty = false;
      await loadProvider();
      $("analysis-consent").checked = false; $("research-consent").checked = false;
      if (!clear && state.provider?.configured) $("analysis-method").value = "openrouter";
      providerMessage(clear ? "The key was forgotten, including its saved Windows copy. No model request was made." : state.provider?.remember_key ? "Key and preferences saved in Windows Credential Manager. They will reconnect after restart." : "Settings updated. No model request was made.");
      loadAnalysisPlan(); await loadAutomation();
    } catch (error) {
      const message = key ? String(error.message).split(key).join("[redacted]") : error.message;
      providerMessage(message, true);
    } finally {
      $("provider-key").value = ""; state.providerSaving = false; updateWorkflowControls();
    }
  }
  async function loadAnalysisPlan() {
    state.planController?.abort(); state.planController = new AbortController();
    const version = ++state.planVersion;
    state.plan = null; state.planError = null;
    const router = $("analysis-method").value === "openrouter";
    $("analysis-plan").hidden = !router;
    if (!router) { state.planLoading = false; updateWorkflowControls(); return; }
    state.planLoading = true; $("analysis-plan").replaceChildren(node("p", "field-help", "Planning coverage and cached work locally...")); updateWorkflowControls();
    try {
      const params = new URLSearchParams({ dataset: state.dataset, asin: $("analysis-product").value, depth: $("analysis-depth").value });
      const response = await request(`/api/analysis/plan?${params}`, { signal: state.planController.signal }), plan = await response.json();
      if (version !== state.planVersion) return;
      state.plan = plan; renderAnalysisPlan();
    } catch (error) {
      if (error.name === "AbortError" || version !== state.planVersion) return;
      state.planError = error.message;
      const retry = button("Try again", "text-button", loadAnalysisPlan, "refresh");
      $("analysis-plan").replaceChildren(node("p", "form-message error", error.message), retry);
    } finally {
      if (version === state.planVersion) { state.planLoading = false; updateWorkflowControls(); }
    }
  }
  function renderAnalysisPlan() {
    const plan = state.plan;
    if (!plan) return;
    const heading = node("div", "plan-heading"); heading.append(node("strong", "", plan.depth === "deep" ? "Broader coverage, within a hard budget" : "A quick sample, within a hard budget"), node("span", "badge", "FREE MODELS ONLY"));
    const grid = node("div", "plan-grid");
    const values = [["Reviews selected", `${number(plan.selected_reviews)} / ${number(plan.total_reviews)}`], ["Calls in this run", `Up to ${number(plan.calls_this_run)}`], ["Cached chunks", `${number(plan.chunks_cached)} / ${number(plan.chunks_total)}`], ["App daily budget left", plan.daily_remaining == null ? "Not available" : number(plan.daily_remaining)]];
    for (const [label, value] of values) { const item = node("div"); item.append(node("span", "", label), node("strong", "", value)); grid.append(item); }
    const copy = node("p", "field-help", "This is a local plan. Availability may limit a run. Broader analysis saves completed chunks so later runs can reuse them.");
    $("analysis-plan").replaceChildren(heading, grid, copy);
    if (Array.isArray(plan.notes) && plan.notes.length) {
      const details = node("details", "plan-notes"), summary = node("summary", "", "Coverage details"), list = node("ul");
      for (const note of plan.notes) list.append(node("li", "", note)); details.append(summary, list); $("analysis-plan").append(details);
    }
  }
  async function createJob(options, errorId) {
    if (state.mutation) return null;
    state.mutation = true; messageAt(errorId, ""); renderAutomation();
    try {
      const data = await postJson("/api/jobs", options);
      const job = data.job || data;
      state.jobStatuses.set(job.id, { status: job.status, inserted: job.progress?.reviews_inserted });
      toast(`${jobLabels[job.kind] || "Job"} started.`);
      return job;
    } catch (error) { messageAt(errorId, error.message); return null; }
    finally { state.mutation = false; updateWorkflowControls(); await loadAutomation(); }
  }
  async function mutateJob(job, action) {
    if (state.mutation) return;
    state.mutation = true; renderAutomation();
    try {
      await postJson(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, {});
      toast(action === "cancel" ? "Stop requested. Committed progress stays saved." : "Checking and saving the browser session...");
    } catch (error) { toast(error.message); }
    finally { state.mutation = false; await loadAutomation(); }
  }
  function chooseDataset(dataset) {
    if (state.dataset === dataset) return;
    clearTimeout(state.searchTimer);
    state.dataset = dataset; $("dataset-select").value = dataset;
    savePreference("reviewdesk-dataset", dataset); resetFilters();
    for (const id of ["analysis-product", "research-product"]) {
      $(id).replaceChildren(node("option", "", "All products")); $(id).firstElementChild.value = "";
      delete $(id).dataset.products;
    }
    state.analysis = null; $("analysis-report").hidden = true;
    $("analysis-consent").checked = false; $("research-consent").checked = false;
    loadOverview(); loadAnalysis(); loadAnalysisPlan(); renderResearch();
  }
  async function startCollection(event) {
    event.preventDefault();
    messageAt("collection-form-error", "");
    let urls = $("collection-urls").value.split(/\r?\n/).map((url) => url.trim()).filter(Boolean);
    const pages = Number($("collection-pages").value);
    if (!urls.length) { messageAt("collection-form-error", "Add at least one Amazon India product URL."); return; }
    const products = new Map();
    for (const value of urls) {
      try {
        const url = new URL(value);
        const match = url.pathname.match(/^\/(?:[^/]+\/)?(?:dp|product-reviews)\/([A-Za-z0-9]{10})(?:\/|$)/i);
        if (!/^https:\/\/(?:www\.)?amazon\.in\//i.test(value) || /\s|[\u0000-\u001f\u007f]/.test(value) || url.protocol !== "https:" || !["amazon.in", "www.amazon.in"].includes(url.hostname) || url.username || url.password || url.port || !match) throw new Error();
        if (!products.has(match[1].toUpperCase())) products.set(match[1].toUpperCase(), value);
      } catch { messageAt("collection-form-error", "Use full HTTPS Amazon India product or review URLs with a 10-character ASIN."); return; }
    }
    urls = [...products.values()];
    if (urls.length > 10) { messageAt("collection-form-error", "Use no more than 10 products in one run."); return; }
    if (!Number.isInteger(pages) || pages < 1 || pages > 20 || urls.length * pages > 50) { messageAt("collection-form-error", "Choose 1 to 20 pages per product, with no more than 50 pages across this run."); return; }
    const job = await createJob({ kind: "collection", urls, max_pages: pages, resume: $("collection-resume").checked, transport: $("collection-transport").value, analyse: $("collection-analyse").checked, analysis_method: "local", allow_external: false }, "collection-form-error");
    if (job) { $("analysis-consent").checked = false; $("research-consent").checked = false; updateWorkflowControls(); chooseDataset("workspace"); }
  }
  function fillCollection(job) {
    const options = job.options || {};
    $("collection-urls").value = (options.urls || []).join("\n");
    $("collection-pages").value = options.max_pages || 3;
    $("collection-resume").checked = true;
    $("collection-analyse").checked = options.analyse !== false;
    $("collection-transport").value = options.transport === "hybrid" ? "hybrid" : "browser";
    updateTransportHelp(); navigate("collection", true);
    $("collection-urls").focus();
    toast("Previous options restored. Review them, then start a run to resume saved progress.");
  }
  function updateTransportHelp() {
    $("transport-help").textContent = $("collection-transport").value === "hybrid" ? "Tries lighter session requests, then falls back to the browser when needed. Login and CAPTCHA still stop collection." : "Uses your saved session in Chromium.";
  }
  async function loadAnalysis() {
    state.analysisController?.abort(); state.analysisController = new AbortController();
    const version = ++state.analysisVersion;
    const params = new URLSearchParams({ dataset: state.dataset, asin: $("analysis-product").value });
    state.analysisLoading = true; state.analysis = null;
    $("analysis-report").hidden = true; $("analysis-empty").hidden = true;
    $("analysis-error").hidden = true; $("analysis-loading").hidden = false;
    try {
      const response = await request(`/api/analysis?${params}`, { signal: state.analysisController.signal });
      const data = await response.json();
      if (version !== state.analysisVersion) return;
      state.analysis = data; renderAnalysis();
    } catch (error) {
      if (error.name === "AbortError" || version !== state.analysisVersion) return;
      $("analysis-error-message").textContent = error.message; $("analysis-error").hidden = false;
    } finally {
      if (version === state.analysisVersion) { state.analysisLoading = false; $("analysis-loading").hidden = true; }
    }
  }
  async function startAnalysis(event) {
    event.preventDefault();
    const method = $("analysis-method").value, consent = $("analysis-consent").checked;
    if (method !== "local" && !consent) { messageAt("analysis-form-error", "Choose a local scan or explicitly allow the selected review text to be sent to the model provider."); return; }
    const options = { kind: "analysis", dataset: state.dataset, asin: $("analysis-product").value, analysis_method: method, allow_external: method !== "local" && consent };
    if (method === "openrouter") options.depth = $("analysis-depth").value;
    const job = await createJob(options, "analysis-form-error");
    if (job) { $("analysis-consent").checked = false; $("research-consent").checked = false; }
    updateWorkflowControls();
  }
  function usageSummary(usage, model = "", label = "") {
    const section = node("div", "model-usage");
    if (label) section.append(node("strong", "usage-label", label));
    const facts = [];
    if (usage && Number.isFinite(Number(usage.calls))) facts.push(`${plural(usage.calls, "model call")}`);
    if (usage && Number.isFinite(Number(usage.cached_chunks))) facts.push(`${plural(usage.cached_chunks, "cached chunk")}`);
    if (usage?.usage_available !== false && usage?.input_tokens != null) facts.push(`${number(usage.input_tokens)} input tokens`);
    if (usage?.usage_available !== false && usage?.output_tokens != null) facts.push(`${number(usage.output_tokens)} output tokens`);
    if (facts.length) section.append(node("p", "", facts.join(" · ")));
    const models = Array.isArray(usage?.models) ? [...new Set(usage.models)] : [];
    if (models.length) section.append(node("p", "usage-models", `Model${models.length === 1 ? "" : "s"} returned: ${models.join(", ")}`));
    else if (model) section.append(node("p", "usage-models", Number(usage?.cached_chunks) > 0 ? `Model used for the saved answer: ${model}` : `Configured model route: ${model}`));
    if (Number(usage?.calls) > 0) {
      const knownCost = typeof usage.cost === "number" && Number.isFinite(usage.cost) && usage.cost >= 0;
      section.append(node("p", "usage-models", knownCost ? `Provider-reported cost: $${usage.cost.toFixed(2)} USD` : "Provider cost was not reported."));
    }
    return section;
  }
  async function startResearch(event) {
    event.preventDefault();
    const question = $("research-question").value.trim();
    if (!question) { messageAt("research-form-error", "Add a question about your stored reviews."); return; }
    if (question.length > 1500) { messageAt("research-form-error", "Keep the question within 1,500 characters."); return; }
    const local = isLocalQuestion(question), router = routerStatus();
    const job = await createJob({ kind: "research", question, dataset: state.dataset, asin: $("research-product").value, research_mode: $("research-mode").value, allow_external: !local && $("research-consent").checked && router.configured }, "research-form-error");
    if (job) $("research-consent").checked = false;
    updateWorkflowControls(); renderResearch();
  }
  function researchCard(job) {
    const result = job.result, card = node("article", "panel research-answer");
    const question = node("div", "research-question-line"); question.append(icon("message"), node("h3", "", job.options?.question || result?.question || "Review question")); card.append(question);
    const provenance = node("div", "research-answer-meta");
    provenance.append(node("span", "badge", result?.method === "local" ? "LOCAL STATISTICS" : result?.method === "local_evidence" ? "LOCAL EVIDENCE" : job.status === "succeeded" ? result.cache_hit ? "SAVED AI ANSWER" : "OPENROUTER FREE" : String(job.status).toUpperCase()), node("span", "", timeLabel(job.updated_at)));
    card.append(provenance);
    if (job.status !== "succeeded" || !result?.answer) {
      card.append(node("p", "job-error", job.detail || job.error || "This question could not be completed."));
      if (result?.retry_after != null && Number.isFinite(Number(result.retry_after))) card.append(node("p", "field-help", `Provider guidance: wait at least ${number(Math.ceil(Number(result.retry_after)))} seconds before another attempt. Retries are manual.`));
      card.append(button("Review this question", "text-button", () => {
        $("research-question").value = job.options?.question || ""; $("research-consent").checked = false;
        $("research-question").focus(); toast("Question restored. Check it and sharing consent before trying again.");
      }, "refresh"));
      card.append(button("Find local evidence", "text-button", async () => {
        await createJob({kind: "research", dataset: job.options.dataset, asin: job.options.asin || "", question: job.options.question, allow_external: false, research_mode: "focused"}, "research-form-error");
      }, "search"));
      return card;
    }
    if (result.method === "openrouter") card.append(node("p", "field-help", "AI interpretation of selected excerpts. Check the linked sources before relying on its conclusion."));
    card.append(node("p", "research-answer-text", result.answer));
    if (result.notice?.message) {
      const notice = node("div", "research-notice");
      notice.append(node("strong", "", "AI unavailable; local evidence shown"), node("p", "", result.notice.message));
      if (result.notice.retry_after != null) {
        const retryAt = new Date(new Date(job.updated_at).getTime() + Math.ceil(Number(result.notice.retry_after)) * 1000);
        notice.append(node("p", "", `AI can be tried after ${retryAt.toLocaleTimeString()}. No automatic retry was made.`));
      }
      card.append(notice);
    }
    const usage = usageSummary(result.usage, result.method === "local" ? "" : result.model);
    if (usage.children.length) card.append(usage);
    const scope = result.scope || {};
    const scopeFacts = [];
    if (scope.total_reviews != null) scopeFacts.push(`${plural(scope.total_reviews, "stored review")}`);
    if (scope.matched_reviews != null) scopeFacts.push(`${number(scope.matched_reviews)} local matches`);
    if (scope.retrieved_reviews != null) scopeFacts.push(`${number(scope.retrieved_reviews)} selected for context`);
    if (scope.analysed_reviews != null) scopeFacts.push(`${number(scope.analysed_reviews)} examined`);
    if (scopeFacts.length) card.append(node("p", "research-coverage", scopeFacts.join(" · ")));
    const sources = Array.isArray(result.evidence) ? result.evidence : [];
    if (sources.length) {
      const evidence = node("div", "research-evidence"), title = node("h4", "", "Source reviews"); evidence.append(title);
      for (const source of sources.slice(0, 8)) {
        const entry = node("div", "evidence-card");
        const quotes = Array.isArray(source.quotes) && source.quotes.length ? source.quotes.slice(0, 8) : source.quote ? [source.quote] : [];
        if (quotes.length > 1) entry.append(node("p", "muted", `${number(quotes.length)} passages from one review`));
        for (const quote of quotes) entry.append(node("blockquote", "", quote));
        const open = button("Read source review", "text-button", () => openEvidence(source, job.options.dataset, open), "arrow");
        const footer = node("div", "evidence-source"); footer.append(node("code", "", source.asin), open); entry.append(footer); evidence.append(entry);
      }
      card.append(evidence);
    }
    const steps = Array.isArray(result.steps) ? result.steps : [], tools = steps.filter((step) => step.kind === "tool");
    if (tools.length) {
      const details = node("details", "research-steps"), summary = node("summary", "", `Research steps · ${plural(tools.length, "data lookup")}`), list = node("ol");
      const labels = { search_reviews: "Searched stored reviews", get_review: "Read a source review", review_stats: "Checked review statistics", compare_products: "Compared product ratings" };
      for (const step of steps.slice(0, 32)) {
        const item = node("li"), heading = node("strong", "", labels[step.name] || String(step.name || step.kind || "Research step").replaceAll("_", " "));
        item.append(heading); if (step.detail) item.append(node("p", "", step.detail)); list.append(item);
      }
      details.append(summary, list); card.append(details);
    }
    if (Array.isArray(result.limitations) && result.limitations.length) {
      const list = node("ul", "research-limitations"); for (const note of [...new Set(result.limitations)]) list.append(node("li", "", note)); card.append(list);
    }
    return card;
  }
  function renderResearch() {
    const active = state.automation?.active_job;
    const researchJob = active?.kind === "research" ? active : null;
    const activeKey = JSON.stringify([researchJob, state.mutation]);
    $("research-job").hidden = !researchJob;
    if (state.researchJobKey !== activeKey) {
      state.researchJobKey = activeKey;
      preserveJobFocus($("research-job"), () => $("research-job").replaceChildren(...(researchJob ? [jobCard(researchJob, true)] : [])));
    }
    const seenQuestions = new Set();
    const jobs = (state.automation?.jobs || []).filter((job) => job.kind === "research" && !activeStatuses.has(job.status) && job.options?.dataset === state.dataset && (job.options?.asin || "") === $("research-product").value).filter((job) => {
      const question = String(job.options?.question || "").trim().toLowerCase().replace(/\s+/g, " ");
      if (seenQuestions.has(question)) return false;
      seenQuestions.add(question); return true;
    }).slice(0, 5);
    const key = JSON.stringify([state.dataset, $("research-product").value, jobs]);
    if (state.researchRenderKey !== key) {
      state.researchRenderKey = key;
      $("research-results").replaceChildren(...jobs.map(researchCard));
    }
    $("research-empty").hidden = jobs.length > 0;
    if (!jobs.length) empty($("research-empty"), "Start with something you want to know", "Ask about a count, a rating, or a pattern in your saved reviews. Answers stay linked to the available evidence.", "search");
  }
  async function openEvidence(evidence, dataset, control) {
    if (control.disabled) return;
    control.disabled = true;
    try {
      const params = new URLSearchParams({ dataset, asin: evidence.asin, review_id: evidence.review_id });
      const response = await request(`/api/review?${params}`), data = await response.json();
      showReview(data.review, dataset);
    } catch (error) { toast(error.message || "This source review is no longer available."); }
    finally { control.disabled = false; }
  }
  function renderAnalysis() {
    const data = state.analysis, report = data?.report;
    $("analysis-report").replaceChildren(); $("analysis-report").hidden = !report;
    $("analysis-empty").hidden = Boolean(report);
    if (!report) {
      empty($("analysis-empty"), "The evidence comes first", "Run an analysis on the selected reviews to see themes, rating signals, and source evidence. Local analysis is free and stays on this device.", "spark");
      return;
    }
    const container = $("analysis-report"), scope = report.scope || {}, ratings = report.rating_summary || {};
    const latestAttempt = (state.automation?.jobs || []).find((job) => job.kind === "analysis" && job.options?.dataset === scope.dataset && (job.options?.asin || "") === (scope.asin || ""));
    if (latestAttempt?.status === "failed" && latestAttempt.options?.analysis_method === "openrouter" && Date.parse(latestAttempt.created_at) > Date.parse(report.generated_at)) {
      const warning = node("div", "report-stale"), copy = node("div");
      const failure = latestAttempt.result?.error_code === "unavailable" && !latestAttempt.result?.diagnostics?.routing_reason ? "No compatible free route was available; the response did not identify why." : latestAttempt.detail || "The AI request did not finish.";
      copy.append(node("strong", "", "The latest AI attempt failed"), node("p", "", failure), node("p", "", `No new AI report was saved. The report below is the saved ${report.method_label || report.method} from ${timeLabel(report.generated_at)}.`));
      const diagnostics = describeProviderDiagnostics(latestAttempt.result?.diagnostics);
      if (diagnostics) copy.append(node("p", "", diagnostics));
      warning.append(icon("info"), copy); container.append(warning);
    }
    if (data.stale) {
      const warning = node("div", "report-stale"); warning.append(icon("info"), node("p", "", "Reviews have changed since this report was generated. Run analysis again to update the findings.")); container.append(warning);
    }
    if (scope.partial) {
      const warning = node("div", "report-stale"); warning.append(icon("info"), node("p", "", "This report has partial text coverage. Completed chunks are cached. A later broader run can continue the work within its budget.")); container.append(warning);
    }
    if (report.pause_reason?.message) {
      const pause = node("div", "report-stale"), copy = node("div"); copy.append(node("p", "", report.pause_reason.message));
      if (report.pause_reason.retry_after != null && Number.isFinite(Number(report.pause_reason.retry_after))) copy.append(node("p", "", `Provider guidance: wait at least ${number(Math.ceil(Number(report.pause_reason.retry_after)))} seconds before another run. Retries are manual, with no paid fallback.`));
      const diagnostics = describeProviderDiagnostics(report.pause_reason.diagnostics);
      if (diagnostics) copy.append(node("p", "", diagnostics));
      pause.append(icon("clock"), copy); container.append(pause);
    }
    const intro = node("section", "panel report-intro"), topline = node("div", "report-topline");
    topline.append(node("span", "eyebrow", "SAVED ANALYSIS"), node("span", "badge", report.method_label || report.method || "Analysis"));
    if (report.method === "openrouter") topline.append(node("span", "badge", report.plan?.depth === "deep" ? "BROADER COVERAGE" : "QUICK SAMPLE"));
    if (scope.full_coverage === true) topline.append(node("span", "badge", "STORED TEXT COVERED"));
    intro.append(topline, node("h2", "", scope.asin ? `Feedback for ${scope.asin}` : "Your feedback, in perspective"), node("p", "report-summary", report.summary || "No summary was generated."));
    const metadata = node("div", "report-metadata");
    metadata.append(node("span", "", `${plural(scope.analysed_reviews, "review")} analysed of ${number(scope.total_reviews)}`), node("span", "", `Generated ${timeLabel(report.generated_at)}`), node("span", "", scope.dataset === "demo" ? "Fictional demo reviews" : "My workspace"));
    intro.append(metadata);
    const cachedJob = (state.automation?.jobs || []).find((job) => job.kind === "analysis" && job.options?.dataset === scope.dataset && (job.options?.asin || "") === (scope.asin || "") && job.options?.analysis_method === report.method && (report.method !== "openrouter" || job.options?.depth === report.plan?.depth) && job.result?.analysis_id);
    if (cachedJob?.result?.cached === true) intro.append(node("p", "cache-reused-note", "The latest run reused this saved report with no model call."));
    if (scope.processed_segments != null && scope.total_segments != null) intro.append(node("p", "report-scope-note", `${number(scope.processed_segments)} of ${number(scope.total_segments)} text segments processed. The stored collection can still be a limited sample of available reviews.`));
    const usage = usageSummary(report.usage, report.method === "local" ? "" : report.model, report.usage ? "Report generation usage" : "");
    if (usage.children.length) intro.append(usage);
    if (Number(scope.excluded_reviews) > 0 || scope.body_truncated) intro.append(node("p", "report-scope-note", `${number(scope.excluded_reviews)} reviews excluded.${scope.body_truncated ? " Some review text was shortened for analysis." : ""} Findings describe the analysed sample.`));
    container.append(intro);
    container.append(reportProcess(report));
    const ratingSection = node("section", "report-rating-section"), ratingTitle = node("div", "section-title");
    const ratingCopy = node("div"); ratingCopy.append(node("h2", "", "Rating signals"), node("p", "", "Based on star ratings, rather than inferred sentiment")); ratingTitle.append(ratingCopy); ratingSection.append(ratingTitle);
    const ratingGrid = node("div", "analysis-rating-grid");
    const metrics = [["Average rating", average(ratings.average, 2), `${plural(ratings.rated, "rated review")}`], ["Positive ratings", number(ratings.positive), "4 to 5 stars"], ["Neutral ratings", number(ratings.neutral), "Above 2, below 4 stars"], ["Critical ratings", number(ratings.critical), "1 to 2 stars"]];
    for (const [label, value, caption] of metrics) { const item = node("div", "panel analysis-stat"); item.append(node("span", "stat-heading", label), node("strong", "", value), node("span", "field-help", caption)); ratingGrid.append(item); }
    ratingSection.append(ratingGrid); container.append(ratingSection);
    const themesSection = node("section", "workflow-section"), title = node("div", "section-title"), titleCopy = node("div");
    titleCopy.append(node("h2", "", "Themes in the reviews"), node("p", "", "Open the source reviews to check each finding")); title.append(titleCopy); themesSection.append(title);
    const themes = Array.isArray(report.themes) ? report.themes : [];
    if (!themes.length) { const noThemes = node("div", "panel empty-state compact"); empty(noThemes, "No supported themes yet", "There may be too little review text in this sample to identify a useful pattern.", "message"); themesSection.append(noThemes); }
    else {
      const grid = node("div", "theme-grid");
      for (const [themeIndex, theme] of themes.entries()) {
        const card = node("article", "panel theme-card"), heading = node("div", "theme-heading");
        const countLabel = theme.count_basis === "cited_reviews" ? "cited review" : "matching review";
        heading.append(node("h3", "", theme.label || "Review theme"), node("span", "badge", plural(theme.review_count, countLabel))); card.append(heading);
        if (theme.explanation) card.append(node("p", "theme-explanation", theme.explanation));
        if (theme.rating_mix && typeof theme.rating_mix === "object") {
          const mix = theme.rating_mix, line = node("div", "theme-rating-mix");
          line.append(node("span", "mix-label", theme.count_basis === "cited_reviews" ? "Cited review ratings" : "Matching review ratings"));
          const bands = node("p", "", `${number(mix.positive)} at 4 to 5 stars · ${number(mix.neutral)} above 2, below 4 · ${number(mix.critical)} at 1 to 2 stars · ${number(mix.unrated)} unrated`);
          line.append(bands); card.append(line);
        }
        const evidenceList = node("div", "theme-evidence");
        const sources = (Array.isArray(theme.evidence) ? theme.evidence : []).slice(0, 8);
        const moreSources = node("div", "theme-more-evidence");
        moreSources.id = `theme-more-sources-${themeIndex}`; moreSources.hidden = true;
        for (const [sourceIndex, evidence] of sources.entries()) {
          const evidenceCard = node("div", "evidence-card");
          const quotes = Array.isArray(evidence.quotes) && evidence.quotes.length ? evidence.quotes.slice(0, 8) : [evidence.quote || "Review evidence"];
          if (quotes.length > 1) evidenceCard.append(node("p", "muted", `${number(quotes.length)} passages from one review`));
          for (const quote of quotes) evidenceCard.append(node("blockquote", "", quote));
          const action = button("Read source review", "text-button", () => openEvidence(evidence, scope.dataset || state.dataset, action), "arrow");
          const source = node("div", "evidence-source"); source.append(node("code", "", evidence.asin || ""), action); evidenceCard.append(source);
          (sourceIndex < 3 ? evidenceList : moreSources).append(evidenceCard);
        }
        if (sources.length > 3) {
          evidenceList.append(moreSources);
          const toggle = button(`Show ${plural(sources.length - 3, "more source")}`, "text-button source-toggle", () => {
            moreSources.hidden = !moreSources.hidden;
            toggle.setAttribute("aria-expanded", String(!moreSources.hidden));
            toggle.lastElementChild.textContent = moreSources.hidden ? `Show ${plural(sources.length - 3, "more source")}` : "Show fewer sources";
          }, "down");
          toggle.setAttribute("aria-expanded", "false"); toggle.setAttribute("aria-controls", moreSources.id);
          evidenceList.append(toggle);
        }
        card.append(evidenceList); grid.append(card);
      }
      themesSection.append(grid);
    }
    container.append(themesSection);
    const limitations = node("section", "panel report-limitations"), heading = node("div", "limitations-heading"); heading.append(icon("info"), node("h2", "", "Read this report with its limits")); limitations.append(heading);
    const list = node("ul");
    const notes = [...new Set(Array.isArray(report.limitations) ? report.limitations : [])];
    if (scope.dataset === "demo" && !notes.some((note) => /fictional|demo data/i.test(String(note)))) notes.unshift("These reviews are fictional demo data. Findings are examples, not customer evidence.");
    if (Number(ratings.unrated) > 0) notes.push(`${plural(ratings.unrated, "review")} had no rating and did not contribute to rating signals.`);
    if (!notes.length) notes.push("Findings describe only the selected stored reviews. They do not establish complete coverage or product quality.");
    for (const note of notes) list.append(node("li", "", note)); limitations.append(list); container.append(limitations);
  }
  function reportProcess(report) {
    const details = node("details", "panel report-process"), list = node("ol"), scope = report.scope || {};
    details.append(node("summary", "", "How this report was produced"));
    const steps = [];
    steps.push(`${number(scope.total_reviews)} stored reviews in this scope. This is the collected sample, not a claim about every review on Amazon.`);
    steps.push("Python calculates the average and rating groups from the stored ratings. These numbers are not estimated by a language model.");
    if (report.method === "local") {
      steps.push("The local scan matches English topic keywords. It makes no AI request. Matching a word does not establish praise, a complaint, or the sentiment of a topic.");
      steps.push("Each topic keeps exact excerpts from matching reviews. Open a source to read its full context. Topics can overlap, so their counts should not be added together.");
    } else {
      const router = report.method === "openrouter";
      const selection = router && report.plan?.depth === "deep" ? "Broader coverage splits long reviews into segments and packs requests within a fixed byte budget." : "The model receives a bounded selection of review text. Shortened or excluded reviews are disclosed in the coverage notes.";
      steps.push(`${selection} ${number(scope.analysed_reviews)} reviews were analysed; ${number(scope.excluded_reviews)} were excluded. Reviewer names and Amazon session data are not supplied by the app; review text itself may contain personal details.`);
      steps.push(router ? "The model selects labelled passages for its themes. Python inserts the original quotes and checks their source references before saving a chunk. Multiple passages from one review count as one source. Cited counts are examples, not the number of all reviews expressing that view." : "The model proposes themes and quotes. Python checks that every quote occurs in the cited submitted text. Cited counts are examples, not topic prevalence.");
      steps.push("A source match proves where a quote came from. It does not prove the interpretation. Read the linked reviews before relying on a conclusion.");
      if (router) {
        const provenance = report.provenance;
        if (provenance?.response_mode) steps.push(provenance.response_mode === "prompt" ? "Response format: compatible JSON. The format was requested in the prompt and validated locally, without requiring provider-enforced JSON support." : "Response format: provider-enforced JSON, followed by the same local validation.");
        steps.push(provenance ? `Recorded request policy: free-only routing, training use denied, zero data retention ${provenance.require_zdr === true ? "required" : "not required"}.` : "This older report did not record its privacy settings. Current settings do not establish which settings produced a past report.");
        steps.push("Validated chunks are cached locally. An unchanged run can reuse them. The plan shows calls still needed; report generation usage describes the saved report's original run. Failed requests are not retried automatically.");
      }
    }
    for (const step of steps) list.append(node("li", "", step));
    details.append(list);
    return details;
  }
  function focusSearch() {
    navigate("reviews", true);
    $("review-search").focus();
  }
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const dark = theme === "dark";
    $("theme-toggle").setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} appearance`);
    $("theme-toggle").replaceChildren(icon(dark ? "sun" : "moon"));
  }

  document.querySelectorAll("[data-view]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); navigate(link.dataset.view, true); }));
  document.querySelectorAll("[data-go-view]").forEach((control) => control.addEventListener("click", () => navigate(control.dataset.goView, true)));
  document.querySelectorAll("[data-open-import]").forEach((control) => control.addEventListener("click", openImport));
  document.querySelectorAll("[data-open-settings]").forEach((control) => control.addEventListener("click", openSettings));
  document.querySelectorAll("[data-close-dialog]").forEach((control) => control.addEventListener("click", () => { if (!(state.importing && control.dataset.closeDialog === "import-dialog") && !(state.providerSaving && control.dataset.closeDialog === "settings-dialog")) $(control.dataset.closeDialog).close(); }));
  $("import-dialog").addEventListener("cancel", (event) => { if (state.importing) event.preventDefault(); });
  $("settings-dialog").addEventListener("cancel", (event) => { if (state.providerSaving) event.preventDefault(); });
  $("settings-dialog").addEventListener("close", () => { $("provider-key").value = ""; });
  for (const dialog of [$("review-dialog"), $("import-dialog"), $("settings-dialog")]) dialog.addEventListener("click", (event) => {
    if (event.target !== dialog || (state.importing && dialog.id === "import-dialog") || (state.providerSaving && dialog.id === "settings-dialog")) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
  $("dataset-select").value = state.dataset;
  $("dataset-select").addEventListener("change", () => chooseDataset($("dataset-select").value));
  $("menu-toggle").addEventListener("click", () => setMenu(!$("sidebar").classList.contains("open")));
  $("sidebar-overlay").addEventListener("click", () => { setMenu(false); $("menu-toggle").focus(); });
  window.matchMedia("(min-width: 761px)").addEventListener("change", (event) => { if (event.matches) setMenu(false); });
  $("retry-button").addEventListener("click", loadOverview); $("reviews-retry").addEventListener("click", loadReviews);
  $("review-search").addEventListener("input", () => {
    state.filters.q = $("review-search").value; state.filters.page = 1; syncFilters();
    state.reviewController?.abort(); ++state.reviewVersion;
    $("reviews-table-wrap").hidden = true; $("reviews-empty").hidden = true; $("pagination").hidden = true;
    $("reviews-loading").hidden = false; $("results-count").textContent = "Searching reviews...";
    clearTimeout(state.searchTimer); state.searchTimer = setTimeout(loadReviews, 250);
  });
  for (const [id, key] of [["product-filter", "asin"], ["rating-filter", "rating"], ["verified-filter", "verified"], ["review-sort", "sort"]]) {
    $(id).addEventListener("change", () => { clearTimeout(state.searchTimer); state.filters[key] = $(id).value; state.filters.page = 1; loadReviews(); });
  }
  $("clear-filters").addEventListener("click", () => { resetFilters(); loadReviews(); });
  $("previous-page").addEventListener("click", () => { if (state.filters.page > 1) { --state.filters.page; loadReviews(); } });
  $("next-page").addEventListener("click", () => { if (state.reviews && state.filters.page < state.reviews.pages) { ++state.filters.page; loadReviews(); } });
  $("quick-search").addEventListener("click", focusSearch);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("sidebar").classList.contains("open")) { setMenu(false); $("menu-toggle").focus(); }
    if (event.key === "Tab" && $("sidebar").classList.contains("open")) {
      const controls = [...$("sidebar").querySelectorAll('a[href], button:not(:disabled), select')];
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    const dialogOpen = Boolean(document.querySelector("dialog[open]"));
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "") || document.activeElement?.isContentEditable;
    if (!dialogOpen && ((event.key.toLowerCase() === "k" && (event.ctrlKey || event.metaKey)) || (event.key === "/" && !typing))) { event.preventDefault(); focusSearch(); }
  });
  $("csv-file").addEventListener("change", () => chooseFile($("csv-file").files[0]));
  ["dragenter", "dragover"].forEach((type) => $("dropzone").addEventListener(type, (event) => { event.preventDefault(); if (!state.importing) $("dropzone").classList.add("drag-over"); }));
  ["dragleave", "drop"].forEach((type) => $("dropzone").addEventListener(type, (event) => { event.preventDefault(); $("dropzone").classList.remove("drag-over"); }));
  $("dropzone").addEventListener("drop", (event) => {
    if (state.importing) return;
    const files = event.dataTransfer.files;
    if (files.length !== 1) { importMessage("Choose one CSV file at a time.", true); return; }
    chooseFile(files[0]);
    const transfer = new DataTransfer(); transfer.items.add(files[0]); $("csv-file").files = transfer.files;
  });
  $("import-form").addEventListener("submit", importFile);
  $("collection-form").addEventListener("submit", startCollection);
  $("collection-transport").addEventListener("change", updateTransportHelp);
  $("auth-start").addEventListener("click", async () => {
    const job = await createJob({ kind: "auth" }, "auth-form-error");
    if (job) navigate("collection", true);
  });
  $("auth-save").addEventListener("click", () => {
    const active = state.automation?.active_job;
    if (!state.mutation && active?.kind === "auth" && active.status === "running" && active.phase === "awaiting_login") mutateJob(active, "finish");
  });
  $("automation-retry").addEventListener("click", loadAutomation);
  $("checkpoint-refresh").addEventListener("click", () => loadOverview(true));
  $("analysis-form").addEventListener("submit", startAnalysis);
  $("analysis-product").addEventListener("change", () => { $("analysis-consent").checked = false; $("research-consent").checked = false; updateWorkflowControls(); loadAnalysis(); loadAnalysisPlan(); });
  $("analysis-method").addEventListener("change", () => { $("analysis-consent").checked = false; $("research-consent").checked = false; messageAt("analysis-form-error", ""); updateWorkflowControls(); loadAnalysisPlan(); });
  $("analysis-depth").addEventListener("change", () => { $("analysis-consent").checked = false; updateWorkflowControls(); loadAnalysisPlan(); });
  $("analysis-consent").addEventListener("change", updateWorkflowControls);
  $("analysis-retry").addEventListener("click", loadAnalysis);
  $("provider-form").addEventListener("submit", saveProvider);
  $("provider-clear").addEventListener("click", (event) => saveProvider(event, true));
  $("provider-check").addEventListener("click", checkProviderQuota);
  $("allowance-refresh").addEventListener("click", checkProviderQuota);
  $("provider-remember").addEventListener("change", () => { state.providerPolicyDirty = true; });
  $("provider-zdr").addEventListener("change", () => { state.providerPolicyDirty = true; });
  $("provider-response-mode").addEventListener("change", () => { state.providerPolicyDirty = true; });
  $("research-form").addEventListener("submit", startResearch);
  $("research-product").addEventListener("change", () => { $("research-consent").checked = false; messageAt("research-form-error", ""); updateWorkflowControls(); renderResearch(); });
  $("research-question").addEventListener("input", () => { $("research-consent").checked = false; messageAt("research-form-error", ""); updateWorkflowControls(); });
  document.querySelectorAll("[data-question]").forEach((control) => control.addEventListener("click", () => { $("research-question").value = control.dataset.question; $("research-consent").checked = false; updateWorkflowControls(); $("research-question").focus(); }));
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(state.pollTimer); else loadAutomation(); });
  window.addEventListener("pagehide", () => { clearTimeout(state.pollTimer); state.automationController?.abort(); });
  $("export-button").title = "Download a spreadsheet-safe CSV"; $("export-button").addEventListener("click", exportCsv);
  $("theme-toggle").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; applyTheme(theme); savePreference("reviewdesk-theme", theme); });
  $("review-sort").options[0].textContent = "Recently added"; $("review-sort").options[1].textContent = "Added earliest";
  $("recent-review-body").closest(".panel").querySelector(".panel-heading p").textContent = "The most recently added feedback in your workspace";
  $("rating-bars").closest(".panel").querySelector(".panel-footnote span").textContent = "Ratings grouped to the nearest star. Select a bar to explore.";
  if (/Mac|iPhone|iPad/.test(navigator.platform)) $("search-shortcut").textContent = "⌘ K";
  const theme = preference("reviewdesk-theme") === "dark" ? "dark" : "light";
  applyTheme(theme);
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), true));
  navigate(location.hash.slice(1));
  loadOverview();
  updateWorkflowControls();
  loadAutomation();
  loadProvider();
})();
