// Shared harness for ad-hoc live NetSuite checks.
// Reference clone from a working production integration (mint repo).
//
// Conventions:
// - serial execution (SuiteQL rate pressure stays flat),
// - run ALL checks even after failures — one run reports the full failure set,
// - exit code 1 when anything failed.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { buildNsAuthHeader, netsuiteHost } from "./netsuiteAuth";

const SUITEQL_PATH = "/services/rest/query/v1/suiteql";

export interface LiveCheck {
  name: string;
  run: () => Promise<void>;
}

export interface SuiteqlRows {
  items: Array<Record<string, unknown>>;
}

/**
 * Populate process.env from an env file ($NS_ENV_FILE, default ".env.local",
 * resolved from cwd). Minimal parser — no dotenv dep.
 * Real environment variables always win.
 */
export function loadEnvLocal(): void {
  const envPath = resolve(process.cwd(), process.env.NS_ENV_FILE ?? ".env.local");
  let contents: string;
  try {
    contents = readFileSync(envPath, "utf8");
  } catch {
    return;
  }
  for (const line of contents.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}

/** Run one SuiteQL query against the real account. */
export async function suiteql(query: string): Promise<SuiteqlRows> {
  const response = await fetch(`${netsuiteHost()}${SUITEQL_PATH}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: buildNsAuthHeader("POST", SUITEQL_PATH),
      prefer: "transient",
    },
    body: JSON.stringify({ q: query }),
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 300);
    throw new Error(`SuiteQL ${response.status}: ${detail}`);
  }
  return (await response.json()) as SuiteqlRows;
}

/** Execute checks serially; report everything; never abort early. */
export async function runAll(checks: LiveCheck[]): Promise<void> {
  let passed = 0;
  const failed: string[] = [];
  for (const check of checks) {
    try {
      await check.run();
      passed += 1;
      console.log(`PASS ${check.name}`);
    } catch (error) {
      failed.push(check.name);
      const message = error instanceof Error ? error.message : String(error);
      console.log(`FAIL ${check.name}: ${message}`);
    }
  }
  console.log(`${passed} passed, ${failed.length} failed`);
  if (failed.length > 0) {
    process.exitCode = 1;
  }
}
