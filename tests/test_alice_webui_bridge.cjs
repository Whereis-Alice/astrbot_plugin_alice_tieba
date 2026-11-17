"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname,"../pages/alice-tieba/app.js"),"utf8");
const apiSource = source.slice(source.indexOf("async function api("), source.indexOf("function navigate("));

async function verifyBridge(modern, envelope=false) {
  const calls=[];
  // These prefixes match PluginViewPage.vue (4.29) and
  // PluginPagePage.vue (4.25), rather than accepting an arbitrary mock path.
  const prefix=modern?"/api/v1/plugins/extensions/astrbot_plugin_alice_tieba/":"/api/plug/astrbot_plugin_alice_tieba/";
  const bridge={
    async apiGet(endpoint){calls.push(["GET",prefix+endpoint]);return envelope?{status:"ok",data:{monitor:{enabled:false}}}:{monitor:{enabled:false}};},
    async apiPost(endpoint,body){calls.push(["POST",prefix+endpoint,body]);return envelope?{status:"ok",data:{id:"test-subscription"}}:{id:"test-subscription"};}
  };
  const sandbox={state:{bridge},window:modern?{AstrBotPluginView:bridge}:{AstrBotPluginPage:bridge},fetch:()=>{throw new Error("Bridge requests must not bypass native authentication");}};
  vm.createContext(sandbox);
  vm.runInContext(apiSource,sandbox);
  const overview=await sandbox.api("overview");
  assert.equal(overview.monitor.enabled,false);
  const body={kind:"user",target:"123",session:"bot:GroupMessage:123"};
  const created=await sandbox.api("subscriptions",body);
  assert.equal(created.id,"test-subscription");
  assert.equal(calls[0][1],prefix+"api/overview");
  assert.equal(calls[1][1],prefix+"api/subscriptions");
  assert.equal(calls[1][2],body);
  assert.equal(calls.length,2);
}

(async()=>{await verifyBridge(true);await verifyBridge(false);await verifyBridge(true,true);await verifyBridge(false,true);process.stdout.write("Both AstrBot bridge contracts passed.\n");})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
