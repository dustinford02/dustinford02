function stripComment(line) {
  let quote = null;

  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if ((character === '"' || character === "'") && line[index - 1] !== "\\") {
      quote = quote === character ? null : quote ?? character;
    }
    if (character === "#" && quote === null) {
      return line.slice(0, index);
    }
  }

  return line;
}

function parseScalar(value, lineNumber) {
  if (value === "true") return true;
  if (value === "false") return false;
  if (value === "null") return null;

  if (value.startsWith('"')) {
    try {
      return JSON.parse(value);
    } catch {
      throw new Error(`line ${lineNumber}: invalid double-quoted value`);
    }
  }

  if (value.startsWith("'")) {
    if (!value.endsWith("'")) {
      throw new Error(`line ${lineNumber}: unterminated single-quoted value`);
    }
    return value.slice(1, -1).replaceAll("''", "'");
  }

  if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) {
    return Number(value);
  }

  return value;
}

export function parseMappingYaml(source) {
  const root = {};
  const stack = [{ indent: -1, value: root }];

  source.split(/\r?\n/).forEach((rawLine, index) => {
    const lineNumber = index + 1;
    const withoutComment = stripComment(rawLine).trimEnd();
    if (withoutComment.trim() === "") return;
    if (withoutComment.includes("\t")) {
      throw new Error(`line ${lineNumber}: tabs are not supported`);
    }

    const indent = withoutComment.length - withoutComment.trimStart().length;
    const content = withoutComment.trimStart();
    const match = /^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$/.exec(content);
    if (!match) {
      throw new Error(`line ${lineNumber}: expected a mapping entry`);
    }

    while (stack.at(-1).indent >= indent) stack.pop();
    const parent = stack.at(-1);
    if (!parent) {
      throw new Error(`line ${lineNumber}: invalid indentation`);
    }
    const expectedIndent = parent.indent === -1 ? 0 : parent.indent + 2;
    if (indent !== expectedIndent) {
      throw new Error(
        `line ${lineNumber}: expected ${expectedIndent} spaces of indentation`,
      );
    }

    const [, key, rawValue] = match;
    if (Object.hasOwn(parent.value, key)) {
      throw new Error(`line ${lineNumber}: duplicate key "${key}"`);
    }

    if (rawValue === undefined || rawValue === "") {
      const child = {};
      parent.value[key] = child;
      stack.push({ indent, value: child });
      return;
    }

    parent.value[key] = parseScalar(rawValue.trim(), lineNumber);
  });

  return root;
}
