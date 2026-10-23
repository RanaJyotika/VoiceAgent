// `npm run dev`: next dev, with its "Failed to proxy ... ECONNREFUSED / ECONNRESET / socket hang up" errors folded
// into one short line. They come only while the Python server on port 8000 is stopped or restarting (run.bat builds
// the dashboard and loads the voice models first), about 15 lines per request, every few seconds (owner, 22 Oct 2026).
// Everything else next dev prints passes through unchanged. `npm run dev:raw` is plain next dev.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import readline from "node:readline";

const require = createRequire(import.meta.url);
const next = require.resolve("next/dist/bin/next");
const child = spawn(process.execPath, [next, "dev", ...process.argv.slice(2)], {
  stdio: ["inherit", "pipe", "pipe"],
  env: { ...process.env, FORCE_COLOR: process.env.FORCE_COLOR ?? "1" },
});

// The start of one of these errors, and the line that closes its details ("}").
const START = /(Failed to proxy .*(ECONNREFUSED|ECONNRESET|socket hang up))|^\s*Error: (connect ECONNREFUSED|read ECONNRESET|socket hang up)/;
const NOTE_EVERY_MS = 15_000;
let lastNote = 0;
let hidden = 0;

function pipe(stream, out) {
  let skipping = false;
  readline.createInterface({ input: stream, crlfDelay: Infinity }).on("line", (line) => {
    const plain = line.replace(/\x1b\[[0-9;]*m/g, "");
    if (skipping) {
      if (plain.trim() === "}") skipping = false;
      return;
    }
    if (START.test(plain)) {
      skipping = !/}\s*$/.test(plain);
      hidden += 1;
      const now = Date.now();
      if (now - lastNote >= NOTE_EVERY_MS) {
        out.write(`[dev] Python server on port 8000 not answering (stopped or restarting); ${hidden} request(s) failed\n`);
        lastNote = now;
        hidden = 0;
      }
      return;
    }
    out.write(line + "\n");
  });
}

pipe(child.stdout, process.stdout);
pipe(child.stderr, process.stderr);

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
child.on("exit", (code) => process.exit(code ?? 0));
