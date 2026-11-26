#!/usr/bin/env node
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const installer = path.join(__dirname, "..", "scripts", "install_plugin.py");
const options = process.argv.slice(2);
let result;
if (process.platform === "win32") {
  // The Windows app and its knowledge service share the user's WSL distribution.
  const converted = spawnSync("wsl.exe", ["--exec", "wslpath", "-u", installer], { encoding: "utf8" });
  if (converted.error || converted.status !== 0) {
    console.error("Makara requires a working WSL distribution with Python 3.11+ installed.");
    process.exit(1);
  }
  result = spawnSync("wsl.exe", ["--exec", "python3", converted.stdout.trim(), "--windows-app", ...options], { stdio: "inherit" });
} else if (process.platform === "linux") {
  result = spawnSync("python3", [installer, ...options], { stdio: "inherit" });
} else {
  console.error("Makara's local installer currently supports Linux and Windows with WSL.");
  process.exit(1);
}
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
