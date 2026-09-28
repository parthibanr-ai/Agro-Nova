// Load test for a STAGING deployment of the Agro Nova API, using k6 (https://k6.io).
//
//   k6 run -e BASE_URL=https://staging.example.org loadtest/k6_farmers.js
//
// Options (all -e NAME=value):
//   BASE_URL   the API root, without /api/v1                       (required)
//   PEAK_VUS   virtual farmers at the peak of the ramp             (default 50)
//   SKIP_AI    "true" to skip the Gemini-backed screens, for environments without a Gemini key
//   HOLD       how long to stay at the peak, e.g. "5m"             (default 2m)
//
// Each virtual farmer signs in as `k6-farmer-<n>` (the staging server must run with AUTH_MODE=dev), registers one plot
// in Punjab, then loops over the everyday screens and the AI jobs the way the app does, with think time in between.
//
// NEVER point this at production, and remember AI requests spend real Gemini quota if the staging server has a key.
// The thresholds below are the objectives from docs/OPERATIONS.md section 3: the run fails if they are missed.

import http from "k6/http";
import { check, group, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE = `${__ENV.BASE_URL}/api/v1`;
const SKIP_AI = __ENV.SKIP_AI === "true";
const PEAK = parseInt(__ENV.PEAK_VUS || "50");

const jobSuccess = new Rate("ai_job_success");
const jobSeconds = new Trend("ai_job_seconds", true);

export const options = {
  stages: [
    { duration: "1m", target: PEAK },
    { duration: __ENV.HOLD || "2m", target: PEAK },
    { duration: "30s", target: 0 },
  ],
  thresholds: {
    http_req_failed: ["rate<0.01"], // under 1% failed requests
    "http_req_duration{screen:everyday}": ["p(95)<500"], // schemes, soil, plots, forecast: from cache or DB
    "http_req_duration{screen:ai_start}": ["p(95)<3000"], // a job start answers at once, or within the 2 s fast wait
    ai_job_success: ["rate>0.95"], // 95% of AI jobs finish successfully
    ai_job_seconds: ["p(95)<45000"],
  },
};

// A plot of about 1 ha somewhere in Punjab; spreading farmers out exercises many weather cells.
function corners(vu) {
  const lat = 30.2 + ((vu * 37) % 150) / 100;
  const lon = 74.8 + ((vu * 53) % 150) / 100;
  const d = 0.0009;
  return [
    { lat: lat, lon: lon },
    { lat: lat, lon: lon + d },
    { lat: lat + d, lon: lon + d },
    { lat: lat + d, lon: lon },
  ];
}

export function setup() {
  const r = http.get(`${BASE}/health`);
  if (r.status !== 200) throw new Error(`API not reachable at ${BASE}: HTTP ${r.status}`);
}

let plotId = null;

function headers() {
  return { headers: { "X-Dev-User": `k6-farmer-${__VU}`, "Content-Type": "application/json" } };
}

function everyday(path) {
  const r = http.get(`${BASE}${path}`, { ...headers(), tags: { screen: "everyday" } });
  check(r, { [`${path.split("?")[0]} 200`]: (x) => x.status === 200 });
  return r;
}

function aiJob(startPath) {
  const started = Date.now();
  let r = http.post(`${BASE}${startPath}`, null, { ...headers(), tags: { screen: "ai_start" } });
  if (r.status === 429 || r.status === 503) return; // turned away by a limit or full queue: not a job failure
  let body = r.json();
  for (let i = 0; i < 60 && body && (body.status === "queued" || body.status === "running"); i++) {
    sleep(1 + Math.random());
    r = http.get(`${BASE}/jobs/${body.job_id}`, { ...headers(), tags: { screen: "poll" } });
    body = r.json();
  }
  const ok = body && body.status === "done";
  jobSuccess.add(ok);
  jobSeconds.add(Date.now() - started);
}

export default function () {
  if (plotId === null) {
    const r = http.post(
      `${BASE}/plots`,
      JSON.stringify({ name: "k6 plot", crop: "wheat", country: "IN", state: "Punjab", corners: corners(__VU) }),
      headers(),
    );
    if (r.status !== 201) return;
    plotId = r.json("id");
  }

  group("open the app", () => {
    everyday("/me");
    everyday("/plots");
    sleep(1 + Math.random() * 2);
  });
  group("everyday screens", () => {
    everyday("/schemes?plot_id=" + plotId);
    everyday(`/plots/${plotId}/soil`);
    everyday(`/plots/${plotId}/forecast`);
    sleep(2 + Math.random() * 3);
  });
  if (!SKIP_AI) {
    group("AI screens", () => {
      aiJob(`/plots/${plotId}/crop-recommendation/jobs`);
      everyday(`/resilience?plot_id=${plotId}`);
      everyday(`/water-tips?plot_id=${plotId}`);
      sleep(3 + Math.random() * 5);
    });
  }
}
