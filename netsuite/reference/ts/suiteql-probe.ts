// Probe a single SuiteQL with the OAuth signer in ./netsuiteAuth. Prints the
// raw NetSuite response (status + full body incl. `o:errorDetails`).
// Loads env from $NS_ENV_FILE (default ".env.sandbox.local", resolved from cwd)
// and HARD-ABORTS unless NS_ACCOUNT_ID ends in _SB<digits> (sandbox only).
// Usage:
//   pnpm exec tsx suiteql-probe.ts "SELECT id FROM subsidiary WHERE id = 3"
//   NS_ENV_FILE=.env.local pnpm exec tsx suiteql-probe.ts "SELECT ..."
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

function loadEnvFile(path: string): void {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    return;
  }
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq <= 0) continue;
    const k = t.slice(0, eq).trim();
    let v = t.slice(eq + 1).trim();
    if (
      v.length >= 2 &&
      ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'")))
    ) {
      v = v.slice(1, -1);
    }
    if (process.env[k] === undefined) process.env[k] = v;
  }
}
loadEnvFile(resolve(process.cwd(), process.env.NS_ENV_FILE ?? ".env.sandbox.local"));
if (!/_SB\d+$/i.test(process.env.NS_ACCOUNT_ID ?? "")) {
  console.error(`Not sandbox NS_ACCOUNT_ID=${process.env.NS_ACCOUNT_ID}; abort.`);
  process.exit(1);
}

import { buildNsAuthHeader, netsuiteHost } from "./netsuiteAuth";

const SUITEQL = "/services/rest/query/v1/suiteql";
const q = process.argv.slice(2).join(" ").trim();
if (!q) {
  console.error('usage: pnpm exec tsx suiteql-probe.ts "<SQL>"');
  process.exit(1);
}

async function main(): Promise<void> {
  const res = await fetch(netsuiteHost() + SUITEQL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: buildNsAuthHeader("POST", SUITEQL),
      prefer: "transient",
    },
    body: JSON.stringify({ q }),
  });
  const text = await res.text();
  console.log("status", res.status);
  console.log(JSON.stringify(JSON.parse(text), null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
