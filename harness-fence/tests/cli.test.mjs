import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const cli = fileURLToPath(new URL("../src/cli.mjs", import.meta.url));
const good = fileURLToPath(
  new URL("../fence/examples/good.harness.yaml", import.meta.url),
);
const bad = fileURLToPath(
  new URL("../fence/examples/bad.harness.yaml", import.meta.url),
);

function check(file) {
  return spawnSync(process.execPath, [cli, "check", file], {
    encoding: "utf8",
  });
}

test("good example passes", () => {
  const result = check(good);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /^PASS /);
});

test("bad example reports all required must-blocks", () => {
  const result = check(bad);

  assert.equal(result.status, 1);
  assert.match(result.stderr, /H3: Install-on-host must remain disabled/);
  assert.match(result.stderr, /H5: Max, Pro, CLI, and setup-token/);
  assert.match(result.stderr, /H11: Run authorization requires operator approval/);
});
