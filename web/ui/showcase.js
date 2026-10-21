"use strict";

window.ReviewdeskUI.showcase = (manifest, actions) => {
  const {node, button} = window.ReviewdeskUI;
  const panel = node("section", "panel showcase-guide");
  panel.setAttribute("aria-label", "Start exploring the demo");
  panel.append(node("span", "badge", "OFFLINE SHOWCASE"), node("h2", "", "Take a look around."), node("p", "", manifest.notice));
  const controls = node("div", "showcase-actions");
  controls.append(button("1. Inspect a fan", "button button-white", actions.inspect), button("2. Compare the fans", "button button-white", actions.compare), button("3. Ask about battery life", "button button-white", actions.ask));
  panel.append(controls);
  const example = node("details", "execution-details");
  example.append(node("summary", "", "Read an example interpretation"), node("p", "field-help", manifest.example_label));
  for (const item of manifest.examples) {
    const card = node("article", "showcase-example");
    const open = button("Open this fictional source", "text-button", () => actions.source(item.source, open));
    card.append(node("p", "", item.interpretation), node("blockquote", "", item.source.quote), open);
    example.append(card);
  }
  panel.append(example);
  document.getElementById("showcase-guide").replaceChildren(panel);
  return {ready: (enabled) => { for (const control of controls.querySelectorAll("button")) control.disabled = !enabled; }};
};
