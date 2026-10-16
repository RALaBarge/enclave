// The view language: JSON arrays, ["tag", {attrs}, ...children], strings are text. Shared by the engine (renders) and the
// tests (validate). Text only ever reaches the DOM through text nodes, so agent output cannot inject markup.
export const TAGS = new Set(["div", "span", "b", "i", "pre", "code", "h2", "h3", "p", "button", "input", "textarea", "select",
  "option", "label", "table", "thead", "tbody", "tr", "th", "td", "ul", "li", "details", "summary"]);
// k = key (a changed key means a fresh element), c = class, d = data sent back with events, on = {click|input|change|enter: "message-name"} (a bare string means click)
export const ATTRS = new Set(["k", "c", "d", "on", "name", "placeholder", "value", "type", "disabled", "title", "rows", "size",
  "selected", "checked", "open"]);
export const INPUT_TYPES = new Set(["text", "number", "checkbox", "password"]);
export const EVENTS = new Set(["click", "input", "change", "enter"]);

export function validate(n, path = "view") {
  if (n === null || n === false || n === undefined || typeof n === "string" || typeof n === "number") return [];
  if (!Array.isArray(n) || typeof n[0] !== "string") return [path + ": not a node"];
  const errs = [];
  if (!TAGS.has(n[0])) errs.push(path + ": tag " + n[0] + " not allowed");
  let i = 1;
  if (n[1] && typeof n[1] === "object" && !Array.isArray(n[1])) {
    i = 2;
    for (const [k, v] of Object.entries(n[1])) {
      if (!ATTRS.has(k)) errs.push(path + ": attribute " + k + " not allowed");
      if (k === "type" && !INPUT_TYPES.has(v)) errs.push(path + ": input type " + v + " not allowed");
      if (k === "on" && typeof v === "object") for (const e of Object.keys(v)) if (!EVENTS.has(e)) errs.push(path + ": event " + e);
    }
  }
  for (let j = i; j < n.length; j++) errs.push(...validate(n[j], path + ">" + n[0] + "[" + (j - i) + "]"));
  return errs;
}

// Normalise on -> {event: name}
export function events(attrs) {
  const on = attrs.on;
  return !on ? {} : typeof on === "string" ? { click: on } : on;
}
