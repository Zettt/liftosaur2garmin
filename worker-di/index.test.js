import test from "node:test";
import assert from "node:assert/strict";

import worker from "./index.js";

class MemoryKv {
  constructor() {
    this.items = new Map();
  }

  async put(key, value) {
    this.items.set(key, value);
  }

  async get(key) {
    return this.items.get(key) ?? null;
  }

  async delete(key) {
    this.items.delete(key);
  }
}

function makeEnv() {
  return { MFA_SESSIONS: new MemoryKv() };
}

async function readJson(response) {
  return await response.json();
}

test("login returns success on portal happy path", async () => {
  const env = makeEnv();
  const originalFetch = global.fetch;
  global.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.includes("/portal/sso/en-US/sign-in")) {
      return new Response("", {
        status: 200,
        headers: { "set-cookie": "SESSION=portal; Path=/;" },
      });
    }
    if (url.includes("/portal/api/login")) {
      return Response.json({
        responseStatus: { type: "SUCCESSFUL" },
        serviceTicketId: "ST-portal",
      });
    }
    if (url.includes("diauth.garmin.com")) {
      return Response.json({
        access_token: "header.eyJjbGllbnRfaWQiOiJDSUQifQ.sig",
        refresh_token: "refresh",
      });
    }
    throw new Error(`Unexpected fetch: ${url} ${init.method || "GET"}`);
  };

  try {
    const response = await worker.fetch(
      new Request("https://worker.example/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "user@example.com", password: "secret" }),
      }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await readJson(response), {
      status: "success",
      di_token: "header.eyJjbGllbnRfaWQiOiJDSUQifQ.sig",
      di_refresh_token: "refresh",
      di_client_id: "CID",
    });
  } finally {
    global.fetch = originalFetch;
  }
});

test("login falls back to mobile and returns MFA session", async () => {
  const env = makeEnv();
  const originalFetch = global.fetch;
  global.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/portal/sso/en-US/sign-in")) {
      return new Response("", {
        status: 200,
        headers: { "set-cookie": "SESSION=portal; Path=/;" },
      });
    }
    if (url.includes("/portal/api/login")) {
      return Response.json({ error: { "status-code": "427" } });
    }
    if (url.includes("/mobile/sso/en_US/sign-in")) {
      return new Response("", {
        status: 200,
        headers: { "set-cookie": "SESSION=mobile; Path=/;" },
      });
    }
    if (url.includes("/mobile/api/login")) {
      return new Response(
        JSON.stringify({
          responseStatus: { type: "MFA_REQUIRED" },
          customerMfaInfo: { mfaLastMethodUsed: "email" },
        }),
        {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            "set-cookie": "MFA=1; Path=/;",
          },
        },
      );
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    const response = await worker.fetch(
      new Request("https://worker.example/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "user@example.com", password: "secret" }),
      }),
      env,
    );
    const data = await readJson(response);
    assert.equal(response.status, 200);
    assert.equal(data.status, "needs_mfa");
    assert.equal(data.mfa_method, "email");
    assert.ok(data.session_id);
    const stored = JSON.parse(await env.MFA_SESSIONS.get(data.session_id));
    assert.equal(stored.flavour, "mobile");
    assert.match(stored.cookies, /SESSION=mobile/);
    assert.match(stored.cookies, /MFA=1/);
  } finally {
    global.fetch = originalFetch;
  }
});

test("login-mfa exchanges the stored session and returns success", async () => {
  const env = makeEnv();
  await env.MFA_SESSIONS.put(
    "session-123",
    JSON.stringify({
      flavour: "mobile",
      cookies: "SESSION=mobile; MFA=1",
      mfa_method: "email",
      params: "clientId=GCM_ANDROID_DARK&locale=en-US&service=https%3A%2F%2Fmobile.integration.garmin.com%2Fgcm%2Fandroid",
      referer: "https://sso.garmin.com/mobile/sso/en_US/sign-in?clientId=GCM_ANDROID_DARK",
      user_agent: "UA",
      service_url: "https://mobile.integration.garmin.com/gcm/android",
      mfa_path: "/mobile/api/mfa/verifyCode",
    }),
  );
  const originalFetch = global.fetch;
  global.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/mobile/api/mfa/verifyCode")) {
      return Response.json({
        responseStatus: { type: "SUCCESSFUL" },
        serviceTicketId: "ST-mfa",
      });
    }
    if (url.includes("diauth.garmin.com")) {
      return Response.json({
        access_token: "header.eyJjbGllbnRfaWQiOiJDSUQifQ.sig",
        refresh_token: "refresh",
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    const response = await worker.fetch(
      new Request("https://worker.example/login-mfa", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: "session-123", mfa_code: "123456" }),
      }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await readJson(response), {
      status: "success",
      di_token: "header.eyJjbGllbnRfaWQiOiJDSUQifQ.sig",
      di_refresh_token: "refresh",
      di_client_id: "CID",
    });
    assert.equal(await env.MFA_SESSIONS.get("session-123"), null);
  } finally {
    global.fetch = originalFetch;
  }
});

test("login returns invalid_credentials for rejected creds", async () => {
  const env = makeEnv();
  const originalFetch = global.fetch;
  global.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/portal/sso/en-US/sign-in")) {
      return new Response("", { status: 200 });
    }
    if (url.includes("/portal/api/login")) {
      return Response.json({ responseStatus: { type: "INVALID_USERNAME_PASSWORD" } });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    const response = await worker.fetch(
      new Request("https://worker.example/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "user@example.com", password: "bad" }),
      }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await readJson(response), { status: "invalid_credentials" });
  } finally {
    global.fetch = originalFetch;
  }
});

test("login returns rate_limited when both flavours are limited", async () => {
  const env = makeEnv();
  const originalFetch = global.fetch;
  global.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/portal/sso/en-US/sign-in") || url.includes("/mobile/sso/en_US/sign-in")) {
      return new Response("", { status: 200 });
    }
    if (url.includes("/portal/api/login") || url.includes("/mobile/api/login")) {
      return new Response("", { status: 429 });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    const response = await worker.fetch(
      new Request("https://worker.example/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "user@example.com", password: "secret" }),
      }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await readJson(response), { status: "rate_limited" });
  } finally {
    global.fetch = originalFetch;
  }
});

test("login returns needs_captcha when both flavours hit Garmin 427", async () => {
  const env = makeEnv();
  const originalFetch = global.fetch;
  global.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/portal/sso/en-US/sign-in") || url.includes("/mobile/sso/en_US/sign-in")) {
      return new Response("", { status: 200 });
    }
    if (url.includes("/portal/api/login") || url.includes("/mobile/api/login")) {
      return Response.json({ error: { "status-code": "427" } });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    const response = await worker.fetch(
      new Request("https://worker.example/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "user@example.com", password: "secret" }),
      }),
      env,
    );
    assert.equal(response.status, 200);
    const data = await readJson(response);
    assert.equal(data.status, "needs_captcha");
  } finally {
    global.fetch = originalFetch;
  }
});
