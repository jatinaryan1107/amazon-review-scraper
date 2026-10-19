"use strict";

window.ReviewdeskUI = (() => {
  function node(tag, className, textValue) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (textValue !== undefined) result.textContent = String(textValue);
    return result;
  }
  function icon(name, className = "") {
    const result = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    result.setAttribute("class", `icon ${className}`.trim());
    result.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
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
  return {node, icon, button};
})();
