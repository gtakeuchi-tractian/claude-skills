// NetSuite OAuth 1.0 (Token-Based Authentication) signer.
// SERVER-SIDE ONLY — reads secrets from env and must never run in the client.
// Reference clone from a working production integration (mint repo).
// Env access centralized in `./env`.
import crypto from "node:crypto";
import { getNsAccountId, getNsCredentials, suiteTalkHost } from "./env";

function generateNonce(length = 32): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = crypto.randomBytes(length);
  let res = "";
  for (let i = 0; i < length; i++) {
    res += chars.charAt(bytes[i] % chars.length);
  }
  return res;
}

function percentEncode(str: string | number): string {
  return encodeURIComponent(String(str)).replace(
    /[!*()']/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function generateSignature(
  baseString: string,
  consumerSecret: string,
  tokenSecret: string,
): string {
  const key = `${percentEncode(consumerSecret)}&${percentEncode(tokenSecret)}`;
  return crypto.createHmac("sha256", key).update(baseString).digest("base64");
}

/** The NetSuite SuiteTalk host derived from NS_ACCOUNT_ID (throws if unset). */
export function netsuiteHost(): string {
  const accountId = getNsAccountId();
  if (!accountId) {
    throw new Error("Missing NS_ACCOUNT_ID env variable.");
  }
  return suiteTalkHost(accountId);
}

/**
 * Build the `Authorization: OAuth ...` header for a SuiteTalk REST request.
 * `urlPath` must be the path component the request targets (e.g. the SuiteQL path).
 * `queryParams` (RFC 5849 §3.4.1.3) joins the signature's parameter set when the
 * request carries a query string (e.g. `?expandSubResources=true`); the base URL
 * itself always excludes the query. Omitting it keeps the signature bit-identical
 * to before — NetSuite 401s when the request has a query not present in the base string.
 */
export function buildNsAuthHeader(
  method: string,
  urlPath: string,
  queryParams?: Record<string, string | number>,
): string {
  const { accountId, consumerKey, consumerSecret, tokenId, tokenSecret, realm } =
    getNsCredentials();

  const oauthParams: Record<string, string | number> = {
    oauth_consumer_key: consumerKey,
    oauth_token: tokenId,
    oauth_nonce: generateNonce(),
    oauth_signature_method: "HMAC-SHA256",
    oauth_timestamp: Math.floor(Date.now() / 1000),
    oauth_version: "1.0",
  };

  const signatureParams = { ...oauthParams, ...(queryParams ?? {}) };
  const baseUrl = `${suiteTalkHost(accountId)}${urlPath}`;
  const encodedBaseUrl = percentEncode(baseUrl);

  const paramString = Object.keys(signatureParams)
    .sort()
    .map((k) => `${percentEncode(k)}=${percentEncode(signatureParams[k])}`)
    .join("&");

  const baseString = [method.toUpperCase(), encodedBaseUrl, percentEncode(paramString)].join("&");

  const signature = generateSignature(baseString, consumerSecret, tokenSecret);

  return (
    `OAuth realm="${realm}",` +
    `oauth_consumer_key="${percentEncode(String(oauthParams.oauth_consumer_key))}",` +
    `oauth_token="${percentEncode(String(oauthParams.oauth_token))}",` +
    `oauth_signature_method="${oauthParams.oauth_signature_method}",` +
    `oauth_timestamp="${oauthParams.oauth_timestamp}",` +
    `oauth_nonce="${percentEncode(String(oauthParams.oauth_nonce))}",` +
    `oauth_version="${oauthParams.oauth_version}",` +
    `oauth_signature="${percentEncode(signature)}"`
  );
}
