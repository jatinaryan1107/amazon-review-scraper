"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const svgNS = "http://www.w3.org/2000/svg";
  const validViews = new Set(["overview", "reviews", "products", "collection"]);
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
  };
  const viewCopy = {
    overview: ["Review overview", "Explore feedback, compare products, and keep the evidence close."],
    reviews: ["Every review, in focus.", "Find the feedback you need. Open any review to see the full story."],
    products: ["Your products", "Compare the feedback, then explore the reviews behind each product."],
    collection: ["Collection history", "Keep track of saved collection progress and where things stand."],
  };
  const number = (value) => new Intl.NumberFormat().format(Number(value) || 0);
  const average = (value) => value === null || value === undefined || value === "" ? "N/A" : Number(value).toFixed(1);
  const percent = (value) => `${Math.round(Number(value) || 0)}%`;
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
    try {
      response = await fetch(url, { cache: "no-store", ...options });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("The local server could not be reached. Make sure it is running, then try again.");
    }
    if (!response.ok) {
      let message = `The request failed (${response.status}). Please try again.`;
      try { const data = await response.json(); if (data.error) message = data.error; } catch { /* Keep the readable fallback. */ }
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
    document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${view}` || !state.overview; });
    document.querySelectorAll("[data-view]").forEach((link) => {
      const active = link.dataset.view === view;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    const pageLabel = view.charAt(0).toUpperCase() + view.slice(1);
    $("breadcrumb-page").textContent = pageLabel;
    $("page-title").textContent = viewCopy[view][0];
    $("page-subtitle").textContent = viewCopy[view][1];
    document.title = `${pageLabel} | Reviewdesk`;
    setMenu(false);
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view === "reviews" && state.overview && (changed || !state.reviews)) loadReviews();
    if (changed) window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    if (focus) $("main-content").focus({ preventScroll: true });
  }
  function resetFilters() {
    clearTimeout(state.searchTimer);
    state.filters = { q: "", asin: "", rating: "all", verified: "all", sort: "newest", page: 1 };
    state.reviews = null;
    syncFilters();
  }
  function syncFilters() {
    $("review-search").value = state.filters.q;
    $("product-filter").value = state.filters.asin;
    $("rating-filter").value = state.filters.rating;
    $("verified-filter").value = state.filters.verified;
    $("review-sort").value = state.filters.sort;
    $("clear-filters").hidden = !state.filters.q && !state.filters.asin && state.filters.rating === "all" && state.filters.verified === "all";
  }
  async function loadOverview() {
    state.overviewController?.abort();
    state.reviewController?.abort();
    state.overviewController = new AbortController();
    const version = ++state.overviewVersion;
    ++state.reviewVersion;
    state.overview = null; state.reviews = null;
    $("loading-panel").hidden = false;
    $("page-error").hidden = true;
    $("export-button").disabled = true;
    document.querySelectorAll(".view").forEach((section) => { section.hidden = true; });
    $("main-content").setAttribute("aria-busy", "true");
    try {
      const response = await request(`/api/overview?${queryString()}`, { signal: state.overviewController.signal });
      const data = await response.json();
      if (version !== state.overviewVersion) return;
      state.overview = data;
      connection(true);
      renderOverview();
      navigate(state.view);
    } catch (error) {
      if (error.name === "AbortError" || version !== state.overviewVersion) return;
      connection(false);
      $("page-error-message").textContent = error.message || "Please check that the local server is still running.";
      $("page-error").hidden = false;
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
    description.replaceChildren(node("strong", "", data.dataset.is_demo ? "Demo workspace" : "Your local workspace"), node("span", "", data.dataset.is_demo ? "You're exploring fictional reviews. Import a CSV to work with your own data." : "Your imported reviews are stored on this device. No AI analysis has been performed."));
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
    if (!checkpoints.length) empty($("collection-empty"), "No collection jobs yet", state.overview.dataset.is_demo ? "This demo uses fictional reviews. Saved scraper checkpoints will appear in My workspace after you run a collection separately." : "Saved progress will appear here after a collection is run separately. You can import a CSV now to explore existing reviews.", "layers");
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
  function showReview(review) {
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
    if (state.overview.dataset.is_demo) container.append(node("p", "detail-demo-note", "This is a fictional review created for the demo. It is not customer evidence."));
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
      resetFilters();
      state.file = null; $("csv-file").value = "";
      $("file-label").textContent = "Choose another CSV or drop it here"; $("file-description").textContent = "UTF-8 CSV, up to 5 MB";
      await loadOverview();
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
  document.querySelectorAll("[data-close-dialog]").forEach((control) => control.addEventListener("click", () => { if (!(state.importing && control.dataset.closeDialog === "import-dialog")) $(control.dataset.closeDialog).close(); }));
  $("import-dialog").addEventListener("cancel", (event) => { if (state.importing) event.preventDefault(); });
  for (const dialog of [$("review-dialog"), $("import-dialog")]) dialog.addEventListener("click", (event) => {
    if (event.target !== dialog || (state.importing && dialog.id === "import-dialog")) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
  $("dataset-select").value = state.dataset;
  $("dataset-select").addEventListener("change", () => {
    clearTimeout(state.searchTimer); state.dataset = $("dataset-select").value; savePreference("reviewdesk-dataset", state.dataset); resetFilters(); loadOverview();
  });
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
})();
