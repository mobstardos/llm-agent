#!/usr/bin/env node
// DeepSeekHashV1 PoW solver: stdin JSON {challenge, salt, expire_at, difficulty}
// -> stdout JSON {answer: <int>|null}. wasm-bindgen ABI как в sha3_wasm_bg.
"use strict";
const fs = require("fs");
const path = require("path");

const WASM_PATH = path.join(
  path.dirname(__filename), "..", "data", "pow", "sha3_wasm_bg.wasm");

async function main() {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const bytes = fs.readFileSync(WASM_PATH);
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const ex = instance.exports;
  const mem = ex.memory;

  function writeStr(s) {
    const b = Buffer.from(String(s), "utf8");
    const ptr = ex.__wbindgen_export_0(b.length, 1);
    new Uint8Array(mem.buffer, ptr, b.length).set(b);
    return [ptr, b.length];
  }

  const prefix = `${input.salt}_${input.expire_at}_`;
  const retptr = ex.__wbindgen_add_to_stack_pointer(-16);
  const [cp, clen] = writeStr(input.challenge);
  const [pp, plen] = writeStr(prefix);
  ex.wasm_solve(retptr, cp, clen, pp, plen, Number(input.difficulty));
  const view = new DataView(mem.buffer);
  const status = view.getInt32(retptr, true);
  const value = view.getFloat64(retptr + 8, true);
  ex.__wbindgen_add_to_stack_pointer(16);
  const answer = status === 0 ? null : Math.trunc(value);
  process.stdout.write(JSON.stringify({ answer }));
}

main().catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
