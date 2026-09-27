import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { parseMappingYaml } from "./yaml.mjs";

const defaultRulesUrl = new URL("../fence/rules.yaml", import.meta.url);

function getPath(value, path) {
  return path.split(".").reduce((current, key) => current?.[key], value);
}

function display(value) {
  return value === undefined ? "missing" : JSON.stringify(value);
}

function findCredentialLikeValue(value, path = []) {
  if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      const finding = findCredentialLikeValue(child, [...path, key]);
      if (finding) return finding;
    }
    return null;
  }

  if (typeof value !== "string" || /^__[A-Z0-9_]+__$/.test(value)) return null;
  const key = path.at(-1) ?? "";
  const sensitiveKey = /(?:secret|token|credential|api[-_]?key)/iu.test(key);
  const secretSignature = /^(?:sk-[a-z0-9_-]{8,}|[a-z0-9+/]{32,}={0,2})$/iu.test(value);
  return sensitiveKey || secretSignature ? path.join(".") : null;
}

function evaluateRule(rule, config, source) {
  switch (rule.kind) {
    case "equals": {
      const actual = getPath(config, rule.path);
      return Object.is(actual, rule.value)
        ? null
        : `${rule.reason} Expected ${rule.path}=${display(rule.value)}; got ${display(actual)}.`;
    }
    case "conditionalEquals": {
      if (!Object.is(getPath(config, rule.whenPath), rule.whenValue)) return null;
      const actual = getPath(config, rule.path);
      return Object.is(actual, rule.value)
        ? null
        : `${rule.reason} Expected ${rule.path}=${display(rule.value)}; got ${display(actual)}.`;
    }
    case "forbiddenPattern":
      return new RegExp(rule.pattern, "iu").test(source) ? rule.reason : null;
    case "noSecrets": {
      const embedded = getPath(config, rule.embeddedPath);
      const credentialPath = findCredentialLikeValue(config);
      if (embedded === false && credentialPath === null) return null;
      const detail = credentialPath
        ? ` Credential-like value found at ${credentialPath}.`
        : ` Expected ${rule.embeddedPath}=false; got ${display(embedded)}.`;
      return `${rule.reason}${detail}`;
    }
    case "runauthDualGate": {
      if (getPath(config, rule.blockPath) === undefined) return null;
      const valid =
        getPath(config, rule.presentPath) === true &&
        getPath(config, rule.approvalPath) === true &&
        getPath(config, rule.placeholderPath) === rule.placeholder;
      return valid ? null : rule.reason;
    }
    default:
      throw new Error(`rules file contains unsupported kind "${rule.kind}"`);
  }
}

export async function checkHarness(filePath, rulesUrl = defaultRulesUrl) {
  const [source, rulesSource] = await Promise.all([
    readFile(filePath, "utf8"),
    readFile(fileURLToPath(rulesUrl), "utf8"),
  ]);
  const config = parseMappingYaml(source);
  const rules = parseMappingYaml(rulesSource);
  const failures = [];

  for (const [id, rule] of Object.entries(rules)) {
    const reason = evaluateRule(rule, config, source);
    if (reason) failures.push({ id, reason });
  }

  return failures;
}
