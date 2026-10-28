// Server-only secret accessors for NetSuite/CAMU integrations.
// Holds OAuth/CAMU secrets only — never import from client code.
// Reference clone from a working production integration (mint repo).

/** Raw NS_ACCOUNT_ID read. Undefined when unset — caller decides how to react. */
export function getNsAccountId(): string | undefined {
  return process.env.NS_ACCOUNT_ID;
}

/** Build the SuiteTalk REST host from an account id (pure — no env read). */
export function suiteTalkHost(accountId: string): string {
  return `https://${accountId.toLowerCase().replace(/_/g, "-")}.suitetalk.api.netsuite.com`;
}

interface NsCredentials {
  accountId: string;
  consumerKey: string;
  consumerSecret: string;
  tokenId: string;
  tokenSecret: string;
  realm: string;
}

/**
 * Read + validate the five NetSuite OAuth credentials. Throws an aggregate
 * error naming every missing var so a signing caller can't silently proceed
 * with partial creds.
 */
export function getNsCredentials(): NsCredentials {
  const {
    NS_ACCOUNT_ID,
    NS_CONSUMER_KEY,
    NS_CONSUMER_SECRET,
    NS_TOKEN_ID,
    NS_TOKEN_SECRET,
    NS_REALM,
  } = process.env;

  const missing = Object.entries({
    NS_ACCOUNT_ID,
    NS_CONSUMER_KEY,
    NS_CONSUMER_SECRET,
    NS_TOKEN_ID,
    NS_TOKEN_SECRET,
  })
    .filter(([, v]) => !v)
    .map(([k]) => k);

  if (missing.length > 0) {
    throw new Error(`Missing NetSuite credentials: ${missing.join(", ")}.`);
  }

  assertSandboxOnlyInDev();

  return {
    accountId: NS_ACCOUNT_ID as string,
    consumerKey: NS_CONSUMER_KEY as string,
    consumerSecret: NS_CONSUMER_SECRET as string,
    tokenId: NS_TOKEN_ID as string,
    tokenSecret: NS_TOKEN_SECRET as string,
    // Realm defaults to the account id (NetSuite convention) when not set.
    realm: NS_REALM || (NS_ACCOUNT_ID as string),
  };
}

const SANDBOX_ACCOUNT_PATTERN = /_SB\d+$/i;

/**
 * Local-dev guardrail: refuse to touch a production-looking NS_ACCOUNT_ID —
 * one without NetSuite's sandbox `_SB<digits>` suffix — so local dev can only
 * ever sign against the sandbox. Fires only when NODE_ENV=development.
 * Deliberate production-local work: NS_ALLOW_PROD_LOCAL=true.
 */
export function assertSandboxOnlyInDev(): void {
  if (process.env.NODE_ENV !== "development") return;
  if (process.env.NS_ALLOW_PROD_LOCAL === "true") return;
  const accountId = process.env.NS_ACCOUNT_ID;
  if (!accountId || SANDBOX_ACCOUNT_PATTERN.test(accountId)) return;
  throw new Error(
    `NS_ACCOUNT_ID "${accountId}" has no sandbox suffix (_SB1, _SB2, …) — looks like ` +
      `production. Local dev connects only to the sandbox: point the NS_* ` +
      `vars at the sandbox account's credentials, or set ` +
      `NS_ALLOW_PROD_LOCAL=true to bypass deliberately.`,
  );
}

interface CamuConfig {
  apiKey: string | undefined;
  apiUrl: string | undefined;
}

/** CAMU proxy config. Both optional — callers decide how to handle absence. */
export function getCamuConfig(): CamuConfig {
  return { apiKey: process.env.CAMU_API_KEY, apiUrl: process.env.CAMU_API_URL };
}
