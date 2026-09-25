// Monguana's code editor: CodeMirror 6, bundled into one classic script that
// sets window.MgEditor. Built by build.sh into appcode/vendor/codemirror/.
//
// Why a classic script and a global: the page loads it on demand by appending
// a <script> and waiting for its load event (the pattern IguanaXterm uses for
// GridStack). Pyodide then calls MgEditor.create() through the FFI. Nothing
// here uses eval, workers or remote resources, so pytincture's CSP
// (script-src 'self' …) is satisfied as-is.
//
// The API is deliberately small and value-in / value-out: Python never
// touches CodeMirror's state objects.

import { EditorView, keymap, placeholder as placeholderExt, drawSelection, highlightActiveLine,
  lineNumbers, highlightActiveLineGutter } from "@codemirror/view";
import { EditorState, Prec, Compartment } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap, indentWithTab, insertNewlineAndIndent }
  from "@codemirror/commands";
import { javascript } from "@codemirror/lang-javascript";
import { autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
  completionStatus, acceptCompletion } from "@codemirror/autocomplete";
import { syntaxHighlighting, HighlightStyle, bracketMatching, indentOnInput }
  from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

const VERSION = "1";

// ── Look ─────────────────────────────────────────────────────────────────────
// Matches Monguana's slate palette (monguana.py _CSS) rather than a stock theme.

const theme = EditorView.theme({
  "&": {
    color: "#d1fae5",
    backgroundColor: "#0f172a",
    border: "1px solid #334155",
    borderRadius: "6px",
    fontSize: "12.5px",
  },
  "&.cm-focused": {
    outline: "none",
    borderColor: "#10b981",
    boxShadow: "0 0 0 3px rgba(16,185,129,.18)",
  },
  ".cm-scroller": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    lineHeight: "1.45",
  },
  ".cm-content": { padding: "6px 0", caretColor: "#34d399" },
  ".cm-line": { padding: "0 8px" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "#34d399" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "#1e3a5f !important",
  },
  ".cm-activeLine": { backgroundColor: "rgba(148,163,184,.06)" },
  ".cm-gutters": { backgroundColor: "#0b1220", color: "#475569", border: "none" },
  ".cm-activeLineGutter": { backgroundColor: "#111827" },
  ".cm-placeholder": { color: "#64748b" },
  ".cm-matchingBracket": { backgroundColor: "rgba(16,185,129,.22)", outline: "none" },
  ".cm-tooltip": {
    backgroundColor: "#111827", border: "1px solid #334155", color: "#e2e8f0",
    borderRadius: "6px", overflow: "hidden",
  },
  ".cm-tooltip-autocomplete > ul": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: "12px", maxHeight: "16em",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    backgroundColor: "#065f46", color: "#ecfdf5",
  },
  ".cm-completionDetail": { color: "#94a3b8", fontStyle: "normal", marginLeft: "1em" },
  ".cm-completionMatchedText": { textDecoration: "none", color: "#6ee7b7" },
}, { dark: true });

const highlight = HighlightStyle.define([
  { tag: [t.propertyName, t.definition(t.propertyName)], color: "#7dd3fc" },
  { tag: t.string, color: "#fcd34d" },
  { tag: [t.number, t.bool, t.null], color: "#f9a8d4" },
  { tag: t.regexp, color: "#fdba74" },
  { tag: [t.function(t.variableName), t.className], color: "#6ee7b7" },
  { tag: t.variableName, color: "#e2e8f0" },
  { tag: [t.comment, t.lineComment, t.blockComment], color: "#64748b", fontStyle: "italic" },
  { tag: [t.brace, t.squareBracket, t.paren, t.punctuation, t.separator], color: "#94a3b8" },
  { tag: t.keyword, color: "#c4b5fd" },
]);

// ── Completions ──────────────────────────────────────────────────────────────

const OPERATORS = [
  ["$eq", "equal"], ["$ne", "not equal"], ["$gt", "greater than"],
  ["$gte", "greater or equal"], ["$lt", "less than"], ["$lte", "less or equal"],
  ["$in", "any of [..]"], ["$nin", "none of [..]"], ["$exists", "field present"],
  ["$type", "BSON type"], ["$regex", "pattern"], ["$options", "regex flags"],
  ["$and", "all of [..]"], ["$or", "any of [..]"], ["$nor", "none of [..]"],
  ["$not", "negate"], ["$elemMatch", "array element matches"], ["$size", "array length"],
  ["$all", "array has all"], ["$expr", "aggregation expression"], ["$text", "text search"],
  ["$mod", "[divisor, remainder]"],
  // update operators
  ["$set", "update: set fields"], ["$unset", "update: remove fields"],
  ["$inc", "update: increment"], ["$mul", "update: multiply"], ["$rename", "update: rename"],
  ["$push", "update: append"], ["$pull", "update: remove matching"],
  ["$addToSet", "update: add if absent"], ["$pop", "update: remove first/last"],
  ["$min", "update: keep smaller"], ["$max", "update: keep larger"],
  ["$currentDate", "update: now"], ["$setOnInsert", "update: on upsert"],
  // pipeline stages
  ["$match", "stage"], ["$group", "stage"], ["$project", "stage"], ["$sort", "stage"],
  ["$limit", "stage"], ["$skip", "stage"], ["$unwind", "stage"], ["$lookup", "stage"],
  ["$addFields", "stage"], ["$count", "stage"], ["$facet", "stage"],
  ["$sortByCount", "stage"], ["$replaceRoot", "stage"], ["$sample", "stage"],
  // accumulators
  ["$sum", "accumulator"], ["$avg", "accumulator"], ["$first", "accumulator"],
  ["$last", "accumulator"], ["$push", "accumulator"], ["$addToSet", "accumulator"],
].map(([label, detail]) => ({ label, detail, type: "keyword" }));

const HELPERS = [
  ["ObjectId", 'ObjectId("")', 10],
  ["ISODate", 'ISODate("")', 9],
  ["NumberLong", "NumberLong()", 11],
  ["NumberInt", "NumberInt()", 10],
  ["NumberDecimal", 'NumberDecimal("")', 15],
  ["UUID", 'UUID("")', 6],
].map(([label, text, caret]) => ({
  label, type: "function", detail: "BSON",
  apply: (view, _completion, from, to) => {
    view.dispatch({
      changes: { from, to, insert: text },
      selection: { anchor: from + caret },
    });
  },
}));

// Field paths are per editor: the page sets them from the sampled schema.
function completionSource(getFields) {
  return (context) => {
    const word = context.matchBefore(/[\w$.]+/);
    if (!word && !context.explicit) return null;
    const from = word ? word.from : context.pos;
    const text = word ? word.text : "";
    let options;
    if (text.startsWith("$")) {
      options = OPERATORS;
    } else {
      const fields = getFields().map((field) => ({
        label: field.path, detail: field.types || "", type: "property", boost: 2,
      }));
      options = fields.concat(HELPERS, text ? [] : OPERATORS);
    }
    return { from, options, validFor: /^[\w$.]*$/ };
  };
}

// ── Editors ──────────────────────────────────────────────────────────────────

function create(host, options = {}) {
  let fields = [];
  const multiline = Boolean(options.multiline);
  const call = (name, ...args) => {
    const handler = options[name];
    if (typeof handler === "function") handler(...args);
  };

  const run = () => { call("onRun"); return true; };
  const keys = [
    { key: "Mod-Enter", run },
    { key: "Mod-s", run: () => { call("onSave"); return true; }, preventDefault: true },
  ];
  if (!multiline) {
    // One-line boxes run on Enter, as the plain text box did. An open
    // completion list gets Enter first: completionKeymap is Prec.highest.
    // While a list is open, Enter accepts or does nothing: never a newline.
    // acceptCompletion refuses within ~75 ms of the list opening (CodeMirror's
    // guard against accidental accepts), and falling through then reached the
    // default keymap's newline -- a fast Enter put a line break in the filter.
    keys.push({
      key: "Enter",
      run: (view) => {
        if (completionStatus(view.state) === "active") {
          acceptCompletion(view);
          return true;
        }
        return run();
      },
    });
    keys.push({ key: "Shift-Enter", run: insertNewlineAndIndent });
  }

  const readOnly = new Compartment();
  const extensions = [
    history(),
    drawSelection(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    javascript(),
    syntaxHighlighting(highlight),
    theme,
    autocompletion({ override: [completionSource(() => fields)], activateOnTyping: true }),
    // Ours first: in a one-line box our Enter must decide before
    // completionKeymap's, which falls through when it declines.
    Prec.highest(keymap.of(keys)),
    Prec.high(keymap.of(completionKeymap)),
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap,
      ...(multiline ? [indentWithTab] : [])]),
    EditorView.lineWrapping,
    EditorView.updateListener.of((update) => {
      if (update.docChanged) call("onChange", update.state.doc.toString());
      if (update.focusChanged && !update.view.hasFocus) call("onBlur");
    }),
    readOnly.of(EditorState.readOnly.of(Boolean(options.readOnly))),
    EditorView.contentAttributes.of({
      "aria-label": options.label || "Query editor",
      spellcheck: "false",
      autocorrect: "off",
      autocapitalize: "off",
    }),
  ];
  if (options.placeholder) extensions.push(placeholderExt(options.placeholder));
  if (options.lineNumbers) {
    extensions.push(lineNumbers(), highlightActiveLineGutter(), highlightActiveLine());
  }
  const sizing = {};
  if (options.minHeight) sizing["&"] = { minHeight: options.minHeight };
  if (options.maxHeight) sizing[".cm-scroller"] = { maxHeight: options.maxHeight, overflow: "auto" };
  if (options.height) {
    sizing["&"] = { height: options.height };
    sizing[".cm-scroller"] = { overflow: "auto" };
  }
  extensions.push(EditorView.theme(sizing));

  const view = new EditorView({
    parent: host,
    state: EditorState.create({ doc: options.value || "", extensions }),
  });

  return {
    getValue: () => view.state.doc.toString(),
    setValue: (text, caret) => {
      const insert = String(text ?? "");
      const anchor = caret == null ? insert.length : Math.max(0, Math.min(caret, insert.length));
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert },
        selection: { anchor },
      });
    },
    focus: () => view.focus(),
    setFields: (list) => { fields = Array.from(list || []); },
    setReadOnly: (value) => view.dispatch({
      effects: readOnly.reconfigure(EditorState.readOnly.of(Boolean(value))),
    }),
    destroy: () => view.destroy(),
    dom: view.dom,
  };
}

window.MgEditor = { VERSION, create };
