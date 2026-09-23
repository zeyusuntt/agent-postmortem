#!/usr/bin/env node
// PreToolUse hook for the Workflow tool. Backs rule R-MODEL-ALLOCATION (see
// rules.py): every agent() call in a workflow script must set a concrete
// top-level opts.model or opts.agentType, or carry an intentional
// "// model-ok: <reason>" comment on the same or an adjacent line. A
// violation denies the tool call with a message naming the lines to fix.
//
// Fails open only when the event is confirmed not to be a Workflow call, or
// the call names a pre-registered workflow with no script text to check
// (there is nothing here to inspect either way). Any other failure to
// inspect the call (the hook payload is not valid JSON, scriptPath cannot
// be read, or an internal error while scanning the script) denies with a
// diagnostic instead of silently letting an unchecked script through.
//
// Install: copy this file anywhere readable (for example
// ~/.claude/hooks/model_allocation_guard.mjs), then add to settings.json:
// {"hooks": {"PreToolUse": [{"matcher": "Workflow", "hooks": [
//   {"type": "command", "command": "node /path/to/model_allocation_guard.mjs"}
// ]}]}}
// `postmortem apply R-MODEL-ALLOCATION --target FILE` prints this exact
// snippet with the path filled in; it does not edit settings.json for you.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString("utf8");
}

// Blank out strings and comments with spaces, preserving length and
// newlines, so paren matching and line numbers stay accurate.
function stripStringsAndComments(src) {
  let out = "";
  let i = 0;
  let mode = "code"; // code | line | block | squote | dquote | template
  const n = src.length;
  while (i < n) {
    const ch = src[i];
    const next = src[i + 1];
    if (mode === "code") {
      if (ch === "/" && next === "/") { mode = "line"; out += "  "; i += 2; continue; }
      if (ch === "/" && next === "*") { mode = "block"; out += "  "; i += 2; continue; }
      if (ch === "'") { mode = "squote"; out += " "; i++; continue; }
      if (ch === '"') { mode = "dquote"; out += " "; i++; continue; }
      if (ch === "`") { mode = "template"; out += " "; i++; continue; }
      out += ch; i++; continue;
    }
    if (mode === "line") {
      if (ch === "\n") { mode = "code"; out += "\n"; } else { out += " "; }
      i++; continue;
    }
    if (mode === "block") {
      if (ch === "*" && next === "/") { mode = "code"; out += "  "; i += 2; continue; }
      out += ch === "\n" ? "\n" : " "; i++; continue;
    }
    // string modes
    if (ch === "\\") { out += "  "; i += 2; continue; }
    if (
      (mode === "squote" && ch === "'") ||
      (mode === "dquote" && ch === '"') ||
      (mode === "template" && ch === "`")
    ) {
      mode = "code"; out += " "; i++; continue;
    }
    out += ch === "\n" ? "\n" : " "; i++; continue;
  }
  return out;
}

function lineOf(text, idx) {
  let line = 1;
  for (let i = 0; i < idx && i < text.length; i++) if (text[i] === "\n") line++;
  return line;
}

// Find real agent(...) call sites: identifier boundary before "agent", not a
// property access (skips foo.agent(...)), must be a call (skips bare
// references like pipeline.map(agent)). Carries both the cleaned argument
// text (safe for structural parsing: no stray brackets or commas hiding
// inside a string) and the same slice of the original, unblanked source
// (needed to tell a real value like model: "opus" apart from a blanked-out
// one), aligned index for index since blanking preserves length exactly.
function findAgentCalls(cleaned, src) {
  const calls = [];
  const re = /(?<![\w$.])agent\s*\(/g;
  let m;
  while ((m = re.exec(cleaned))) {
    const open = cleaned.indexOf("(", m.index);
    let depth = 0;
    let j = open;
    for (; j < cleaned.length; j++) {
      if (cleaned[j] === "(") depth++;
      else if (cleaned[j] === ")") { depth--; if (depth === 0) break; }
    }
    if (depth !== 0) continue; // unbalanced call — skip this site, fail open
    calls.push({
      start: m.index,
      end: j,
      args: cleaned.slice(open + 1, j),
      rawArgs: src.slice(open + 1, j),
    });
  }
  return calls;
}

// Split text at top-level commas only (ignoring commas nested inside (),
// [], or {}). Operates on cleaned text so a bracket or comma that only
// exists inside a string literal never confuses depth counting.
function splitTopLevel(text) {
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "(" || ch === "[" || ch === "{") depth++;
    else if (ch === ")" || ch === "]" || ch === "}") depth--;
    else if (ch === "," && depth === 0) {
      parts.push({ start, end: i });
      start = i + 1;
    }
  }
  parts.push({ start, end: text.length });
  return parts;
}

// Does one call argument set model or agentType to a concrete value, right
// on its own top level? Only an object-literal argument is inspected, and
// only that argument's own top-level properties: a value nested inside
// another object (for example { onError: { model: "x" } }) does not count,
// and neither does model: undefined or an empty value.
function argSetsModelOrAgentType(cleanedArg, rawArg) {
  if (!cleanedArg.trim().startsWith("{")) return false;
  const open = cleanedArg.indexOf("{");
  const close = cleanedArg.lastIndexOf("}");
  if (open === -1 || close === -1 || close <= open) return false;

  const body = cleanedArg.slice(open + 1, close);
  const rawBody = rawArg.slice(open + 1, close);
  for (const { start, end } of splitTopLevel(body)) {
    const prop = body.slice(start, end);
    // Match only through the colon, not the value: a string value that was
    // blanked to spaces in `prop` would otherwise let a greedy \s* here
    // swallow the whole thing, leaving nothing for rawValue to see.
    const keyMatch = prop.match(/^\s*([\w$]+)\s*:/);
    if (!keyMatch) continue;
    const key = keyMatch[1];
    if (key !== "model" && key !== "agentType") continue;
    const rawValue = rawBody.slice(start + keyMatch[0].length, end).trim();
    if (rawValue.length > 0 && rawValue !== "undefined") return true;
  }
  return false;
}

function denyUninspectable(reason) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason:
          `model_allocation_guard: could not inspect this Workflow call (${reason}); ` +
          "denying rather than silently allowing an unchecked script. Fix the " +
          "underlying issue and re-invoke Workflow.",
      },
    })
  );
  process.exit(0);
}

let input;
try {
  input = JSON.parse((await readStdin()) || "{}");
} catch (err) {
  denyUninspectable(`the hook payload is not valid JSON: ${err.message}`);
}

if (input.tool_name && input.tool_name !== "Workflow") process.exit(0); // confirmed non-Workflow

const ti = input.tool_input || {};
let src = typeof ti.script === "string" && ti.script.length ? ti.script : null;
if (!src && typeof ti.scriptPath === "string" && ti.scriptPath.length) {
  try {
    src = readFileSync(resolve(input.cwd || process.cwd(), ti.scriptPath), "utf8");
  } catch (err) {
    denyUninspectable(`could not read scriptPath "${ti.scriptPath}": ${err.message}`);
  }
}
if (!src) process.exit(0); // named workflow or nothing to check

try {
  const cleaned = stripStringsAndComments(src);
  const calls = findAgentCalls(cleaned, src);
  if (calls.length === 0) process.exit(0);

  const srcLines = src.split("\n");
  const violations = [];
  for (const c of calls) {
    const setsModelOrAgentType = splitTopLevel(c.args).some(({ start, end }) =>
      argSetsModelOrAgentType(c.args.slice(start, end), c.rawArgs.slice(start, end))
    );
    if (setsModelOrAgentType) continue;
    const startLine = lineOf(cleaned, c.start);
    const endLine = lineOf(cleaned, c.end);
    let exempt = false;
    for (const ln of [startLine - 1, startLine, endLine, endLine + 1]) {
      const text = srcLines[ln - 1];
      if (text && /model-ok\s*:/i.test(text)) { exempt = true; break; }
    }
    if (!exempt) violations.push(startLine);
  }

  if (violations.length === 0) process.exit(0);

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason:
          `model_allocation_guard: ${violations.length} agent() call(s) at line(s) ` +
          `${violations.join(", ")} set neither opts.model nor opts.agentType. Set one ` +
          `explicitly, or add a "// model-ok: <reason>" comment on the same or an ` +
          `adjacent line to intentionally inherit the session model. Fix the script ` +
          `and re-invoke Workflow.`,
      },
    })
  );
  process.exit(0);
} catch (err) {
  denyUninspectable(`internal error while scanning the script: ${err.message}`);
}
