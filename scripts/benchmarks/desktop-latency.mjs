// Run from the repo root: node scripts/benchmarks/desktop-latency.mjs [baseline-ref]
// Compiles the ACTUAL before/after Chart and metrics modules, renders through
// React's server renderer, and checks sample equality before comparing cost.
// This measures JS/serialization, not a compositor or SSH transport.
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const require = createRequire(resolve('webui/package.json'));
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const baseline = process.argv[2] ?? '207f9ea';
const directory = mkdtempSync(join(tmpdir(), 'turing-latency-'));
symlinkSync(resolve('webui/node_modules'), join(directory, 'node_modules'));
async function load(version, ref) {
  const destination = join(directory, version);
  mkdirSync(destination);
  for (const name of ['metrics.ts', 'Chart.tsx', 'seriesColors.ts', ...(ref ? [] : ['chartGeometry.ts'])]) {
    const path = `webui/src/desktop/panes/${name}`;
    const source = ref ? execFileSync('git', ['show', `${ref}:${path}`], { encoding: 'utf8' }) : readFileSync(path, 'utf8');
    let code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    code = code.replace(/from "\.\/(\w+)"/g, 'from "./$1.mjs"');
    writeFileSync(join(destination, name.replace(/\.tsx?$/, '.mjs')), code);
  }
  return {
    metrics: await import(pathToFileURL(join(destination, 'metrics.mjs'))),
    Chart: (await import(pathToFileURL(join(destination, 'Chart.mjs')))).default,
  };
}
const before = await load('before', baseline);
const after = await load('after', null);
const samples = Array.from({ length: 50_000 }, (_, step) => ({step, ts: step, total_steps: 51000, loss: Math.sin(step / 100), accuracy: step / 50000, energy: step % 13, throughput: step % 7}));
const index = after.metrics.seriesOf(samples);
assert.deepEqual(index, before.metrics.seriesOf(samples));
const added = Array.from({length: 100}, (_, i) => ({step: 50000 + i, loss: i, accuracy: i / 100}));
const incremental = after.metrics.seriesOf(samples);
after.metrics.appendSeries(incremental, added, samples.length);
assert.deepEqual(incremental, before.metrics.seriesOf([...samples, ...added]));

function measure(fn, rounds = 15) {
  fn(); fn(); // warmup
  const elapsed = [];
  for (let i = 0; i < rounds; i++) { const start = performance.now(); fn(); elapsed.push(performance.now() - start); }
  elapsed.sort((a,b) => a-b);
  return { median_ms: elapsed[Math.floor(rounds / 2)], p95_ms: elapsed[Math.ceil(rounds * .95) - 1] };
}
function render(Chart, points) { return renderToStaticMarkup(React.createElement(Chart, {series: [{id: 'run/loss', label: 'loss', points}]})); }
const points = index.get('loss');
const results = {
  baseline_ref: baseline, node: process.version, samples: samples.length,
  // Mirrors both seriesOf calls per MetricsPane update in the baseline.
  series_before: measure(() => { before.metrics.seriesOf([...samples, ...added]); return before.metrics.seriesOf([...samples, ...added]); }),

  chart_before: measure(() => render(before.Chart, points)),
  chart_after: measure(() => render(after.Chart, points)),
  svg_bytes_before: Buffer.byteLength(render(before.Chart, points)),
  svg_bytes_after: Buffer.byteLength(render(after.Chart, points)),
};
// Exclude cache creation from append timings: production retains its index.
const appendTimes = [];
for (let i = 0; i < 31; i++) {
  const cache = new Map([...index].map(([k,v]) => [k, [...v]]));
  const start = performance.now(); after.metrics.appendSeries(cache, added, samples.length);
  appendTimes.push(performance.now() - start);
}
appendTimes.sort((a,b) => a-b);
results.series_after = {median_ms: appendTimes[15], p95_ms: appendTimes[29]};
const long = Array.from({length: 200_000}, (_, i) => [i, i % 100]);
try { render(before.Chart, long); results.long_before = 'renders'; } catch (error) { results.long_before = error.name; }
results.long_after = render(after.Chart, long).includes('polyline') ? 'renders' : 'missing';
assert.equal(results.long_after, 'renders');
console.log(JSON.stringify(results, null, 2));
if (process.env.RESEARCH_RUN_DIR) {
  writeFileSync(join(process.env.RESEARCH_RUN_DIR, 'metrics.json'), JSON.stringify({...results,
    seed: 'not-applicable: deterministic engineering workload', n_examples: samples.length,
    eval_set: 'not-applicable: synthetic engineering workload', eval_set_sha256: 'not-applicable: synthetic engineering workload'}, null, 2));
}
