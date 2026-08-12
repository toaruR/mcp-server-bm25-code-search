#!/usr/bin/env node

const { spawn, execSync } = require("child_process");
const path = require("path");
const fs = require("fs");

function findPythonCommand() {
  const candidates = ["python3", "python", "uv"];
  for (const cmd of candidates) {
    try {
      execSync(`${cmd} --version`, { stdio: "ignore" });
      return cmd;
    } catch (e) {
      // ignore
    }
  }
  return null;
}

function main() {
  const args = process.argv.slice(2);
  const serverPath = path.resolve(__dirname, "..", "bm25_search", "mcp_server.py");

  const pythonCmd = findPythonCommand();

  let child;
  if (pythonCmd && fs.existsSync(serverPath)) {
    if (pythonCmd === "uv") {
      child = spawn("uv", ["run", "python", serverPath, ...args], {
        stdio: "inherit",
        env: process.env,
      });
    } else {
      child = spawn(pythonCmd, [serverPath, ...args], {
        stdio: "inherit",
        env: process.env,
      });
    }
  } else {
    // Fallback to uvx if python script path is not directly found or python binary not found
    try {
      child = spawn("uvx", ["mcp-server-bm25-code-search", ...args], {
        stdio: "inherit",
        env: process.env,
      });
    } catch (err) {
      console.error("[mcp-server-bm25-code-search] Error: Neither Python nor uvx could be found in PATH.");
      console.error("Please install Python 3.9+ or uv (https://astral.sh/uv).");
      process.exit(1);
    }
  }

  child.on("error", (err) => {
    console.error(`[mcp-server-bm25-code-search] Failed to start server process: ${err.message}`);
    process.exit(1);
  });

  child.on("exit", (code, signal) => {
    if (signal) {
      process.exit(1);
    } else {
      process.exit(code ?? 0);
    }
  });
}

main();
