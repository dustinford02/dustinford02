#!/usr/bin/env node

import { resolve } from "node:path";

import { checkHarness } from "./fence.mjs";

function usage() {
  console.error("Usage: harness-fence check <file>");
}

const [, , command, file] = process.argv;

if (command !== "check" || !file) {
  usage();
  process.exitCode = 2;
} else {
  try {
    const filePath = resolve(file);
    const failures = await checkHarness(filePath);

    if (failures.length === 0) {
      console.log(`PASS ${file}`);
    } else {
      console.error(`BLOCK ${file}`);
      for (const failure of failures) {
        console.error(`- ${failure.id}: ${failure.reason}`);
      }
      process.exitCode = 1;
    }
  } catch (error) {
    console.error(`ERROR ${file}: ${error.message}`);
    process.exitCode = 1;
  }
}
