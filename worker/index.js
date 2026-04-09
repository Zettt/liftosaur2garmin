/**
 * Cloudflare Worker: Garmin OAuth ticket exchange proxy.
 *
 * Garmin blocks token exchange from AWS/Azure IPs (Vercel, GitHub Actions).
 * This Worker runs on Cloudflare's edge network, which uses different IP ranges.
 *
 * POST /exchange { ticket }
 * → Fetches consumer creds from S3
 * → Exchanges ticket for OAuth1 token
 * → Exchanges OAuth1 for OAuth2 token
 * → Returns { oauth1, oauth2 }
 */

const CONSUMER_URL = "https://thegarth.s3.amazonaws.com/oauth_consumer.json";
const API_BASE = "https://connectapi.garmin.com";
const USER_AGENT = "com.garmin.android.apps.connectmobile";

// CORS headers for any origin (called from user's Vercel app)
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }
    if (request.method !== "POST") {
      return Response.json({ error: "POST only" }, { status: 405, headers: corsHeaders });
    }

    try {
      const { ticket } = await request.json();
      if (!ticket) {
        return Response.json({ error: "No ticket" }, { status: 400, headers: corsHeaders });
      }

      // Step 1: Fetch consumer credentials
      const consumerResp = await fetch(CONSUMER_URL);
      const consumer = await consumerResp.json();
      const consumerKey = consumer.consumer_key;
      const consumerSecret = consumer.consumer_secret;

      // Step 2: Exchange ticket for OAuth1 token
      const callbackUrl = "https://sso.garmin.com/sso/embed";
      const preauthUrl =
        `${API_BASE}/oauth-service/oauth/preauthorized` +
        `?ticket=${encodeURIComponent(ticket)}` +
        `&login-url=${encodeURIComponent(callbackUrl)}` +
        `&accepts-mfa-tokens=true`;

      const oauthHeader = buildOAuth1Header("GET", preauthUrl, consumerKey, consumerSecret);
      const preauthResp = await fetch(preauthUrl, {
        headers: { Authorization: oauthHeader, "User-Agent": USER_AGENT },
      });

      if (!preauthResp.ok) {
        const text = await preauthResp.text();
        return Response.json(
          { error: `OAuth1 exchange failed (${preauthResp.status}): ${text.slice(0, 200)}` },
          { status: 502, headers: corsHeaders }
        );
      }

      const preauthText = await preauthResp.text();
      const preauthParams = new URLSearchParams(preauthText);
      const oauth1Token = preauthParams.get("oauth_token") || "";
      const oauth1Secret = preauthParams.get("oauth_token_secret") || "";

      if (!oauth1Token) {
        return Response.json(
          { error: "No OAuth1 token — ticket may have expired" },
          { status: 400, headers: corsHeaders }
        );
      }

      // Step 3: Exchange OAuth1 for OAuth2
      const exchangeUrl = `${API_BASE}/oauth-service/oauth/exchange/user/2.0`;
      const exchangeHeader = buildOAuth1Header(
        "POST", exchangeUrl, consumerKey, consumerSecret, oauth1Token, oauth1Secret
      );
      const exchangeResp = await fetch(exchangeUrl, {
        method: "POST",
        headers: {
          Authorization: exchangeHeader,
          "User-Agent": USER_AGENT,
          "Content-Type": "application/x-www-form-urlencoded",
        },
      });

      if (!exchangeResp.ok) {
        const text = await exchangeResp.text();
        return Response.json(
          { error: `OAuth2 exchange failed (${exchangeResp.status}): ${text.slice(0, 200)}` },
          { status: 502, headers: corsHeaders }
        );
      }

      const oauth2 = await exchangeResp.json();

      // Compute absolute timestamps that garth/garminconnect expects
      const now = Math.floor(Date.now() / 1000);
      if (oauth2.expires_in && !oauth2.expires_at) {
        oauth2.expires_at = now + oauth2.expires_in;
      }
      if (oauth2.refresh_token_expires_in && !oauth2.refresh_token_expires_at) {
        oauth2.refresh_token_expires_at = now + oauth2.refresh_token_expires_in;
      }

      return Response.json(
        {
          oauth1: {
            oauth_token: oauth1Token,
            oauth_token_secret: oauth1Secret,
            domain: "garmin.com",
          },
          oauth2,
        },
        { headers: corsHeaders }
      );
    } catch (e) {
      return Response.json(
        { error: e.message || "Internal error" },
        { status: 500, headers: corsHeaders }
      );
    }
  },
};

// ── OAuth1 HMAC-SHA1 signing ─────────────────────────────────────────────

function buildOAuth1Header(method, fullUrl, consumerKey, consumerSecret, token = "", tokenSecret = "") {
  const [baseUrl, queryString] = fullUrl.split("?");
  const nonce = crypto.randomUUID().replace(/-/g, "");
  const timestamp = Math.floor(Date.now() / 1000).toString();

  const oauthParams = {
    oauth_consumer_key: consumerKey,
    oauth_nonce: nonce,
    oauth_signature_method: "HMAC-SHA1",
    oauth_timestamp: timestamp,
    oauth_version: "1.0",
  };
  if (token) oauthParams.oauth_token = token;

  // Collect all params (oauth + query string)
  const allParams = { ...oauthParams };
  if (queryString) {
    for (const part of queryString.split("&")) {
      const [k, v] = part.split("=");
      allParams[decodeURIComponent(k)] = decodeURIComponent(v || "");
    }
  }

  // Build signature base string
  const sortedParams = Object.keys(allParams)
    .sort()
    .map((k) => `${enc(k)}=${enc(allParams[k])}`)
    .join("&");
  const baseString = `${method.toUpperCase()}&${enc(baseUrl)}&${enc(sortedParams)}`;
  const signingKey = `${enc(consumerSecret)}&${enc(tokenSecret)}`;

  const signature = hmacSha1(signingKey, baseString);
  oauthParams.oauth_signature = signature;

  const header = Object.entries(oauthParams)
    .map(([k, v]) => `${enc(k)}="${enc(v)}"`)
    .join(", ");
  return `OAuth ${header}`;
}

function enc(s) {
  return encodeURIComponent(s).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
}

function hmacSha1(key, data) {
  // Use Web Crypto API synchronously via crypto.subtle workaround
  // Cloudflare Workers support top-level await and crypto.subtle
  const encoder = new TextEncoder();
  const keyData = encoder.encode(key);
  const msgData = encoder.encode(data);

  // We need sync HMAC but Workers only have async crypto.subtle
  // Use a synchronous fallback
  return hmacSha1Sync(keyData, msgData);
}

function hmacSha1Sync(keyBytes, msgBytes) {
  // SHA1 HMAC implementation for Cloudflare Workers
  // Key padding
  const blockSize = 64;
  let key = keyBytes;
  if (key.length > blockSize) {
    key = sha1(key);
  }
  const paddedKey = new Uint8Array(blockSize);
  paddedKey.set(key);

  const oKeyPad = new Uint8Array(blockSize);
  const iKeyPad = new Uint8Array(blockSize);
  for (let i = 0; i < blockSize; i++) {
    oKeyPad[i] = paddedKey[i] ^ 0x5c;
    iKeyPad[i] = paddedKey[i] ^ 0x36;
  }

  const inner = sha1(concat(iKeyPad, msgBytes));
  const hmac = sha1(concat(oKeyPad, inner));
  return btoa(String.fromCharCode(...hmac));
}

function concat(a, b) {
  const c = new Uint8Array(a.length + b.length);
  c.set(a);
  c.set(b, a.length);
  return c;
}

// Minimal SHA1 implementation
function sha1(msg) {
  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
  const msgLen = msg.length;
  const bitLen = msgLen * 8;

  // Pre-processing: padding
  const padded = new Uint8Array(Math.ceil((msgLen + 9) / 64) * 64);
  padded.set(msg);
  padded[msgLen] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 4, bitLen, false);

  // Process each 512-bit block
  for (let offset = 0; offset < padded.length; offset += 64) {
    const w = new Uint32Array(80);
    for (let i = 0; i < 16; i++) {
      w[i] = view.getUint32(offset + i * 4, false);
    }
    for (let i = 16; i < 80; i++) {
      w[i] = rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    }

    let a = h0, b = h1, c = h2, d = h3, e = h4;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5a827999; }
      else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1; }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }
      else { f = b ^ c ^ d; k = 0xca62c1d6; }

      const temp = (rotl(a, 5) + f + e + k + w[i]) >>> 0;
      e = d; d = c; c = rotl(b, 30); b = a; a = temp;
    }

    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
  }

  const result = new Uint8Array(20);
  const rv = new DataView(result.buffer);
  rv.setUint32(0, h0, false);
  rv.setUint32(4, h1, false);
  rv.setUint32(8, h2, false);
  rv.setUint32(12, h3, false);
  rv.setUint32(16, h4, false);
  return result;
}

function rotl(n, s) {
  return ((n << s) | (n >>> (32 - s))) >>> 0;
}
