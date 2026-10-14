const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const device={id:'muse',driver:'muse_ble'}, sosexy={id:'sosexy',driver:'sosexy_ble'};
const state=(generation=1)=>({id:'scene_test',owner_id:'owner1',generation,status:'active',devices:[device,sosexy]});
function harness(initial={}) {
  const nativeState={version:2,background_player:true,foreground:true,muse_supported:true,sosexy_connected:true,active:false,...initial};
  const begins=[],stops=[],requests=[],observers=[],events=new Map(),intervals=[];
  let current=state(),accept=true;
  const connections=[],disconnections=[];
  const native={status:()=>JSON.stringify(nativeState),connectSosexy:()=>true,disconnectSosexy:()=>nativeState.sosexy_connected=false,
    connectDevice:driver=>{connections.push(driver);return true;},
    disconnectDevice:driver=>{disconnections.push(driver);if(nativeState.drivers)nativeState.drivers[driver]=false;},
    begin(raw){const value=JSON.parse(raw);begins.push(value);if(accept){nativeState.active=true;nativeState.session_id=value.id;nativeState.generation=value.generation;}return accept;},
    stop:(...args)=>{stops.push(args);nativeState.active=false;},
    panic:()=>{stops.push(['panic']);nativeState.active=false;},
    observe:(...args)=>observers.push(args)};
  const context={console,JSON,Map,Set,Date,ObsidianSceneDevices:native,
    SceneRuntime:{ownerId:()=> 'owner1',current:()=>current,request:async(method,url,body)=>{requests.push({method,url,body});return [];}},
    document:{addEventListener:(key,fn)=>events.set(key,fn)},addEventListener:(key,fn)=>events.set(key,fn),
    setInterval:(fn,ms)=>intervals.push({fn,ms}),setTimeout};
  context.window=context;vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/js/chat/scene_hardware.js'),'utf8'),context);
  return {hw:context.SceneHardware,context,nativeState,begins,stops,requests,observers,events,intervals,connections,disconnections,allow:value=>accept=value,current:value=>current=value};
}
test('connecting and observing never authorizes playback; only explicit begin does',async()=>{
  const h=harness();await h.hw.connect(device,true);await h.hw.connect(sosexy,true);
  h.hw.stateChanged(state());assert.equal(h.begins.length,0);
  h.hw.authorize(state());assert.equal(h.begins.length,1);
  assert.deepEqual(h.begins[0].devices,[device,sosexy]);assert.equal(h.begins[0].owner_id,'owner1');
});
test('native player owns all output polling and phase clocks; page has only idle readiness timer',async()=>{
  const h=harness();h.events.get('DOMContentLoaded')();
  assert.deepEqual(h.intervals.map(i=>i.ms),[5000]);
  await h.hw.connect(device,true);await h.hw.connect(sosexy,true);h.hw.authorize(state());
  h.nativeState.foreground=false;await h.intervals[0].fn();
  assert.equal(h.hw.isArmed(),true);assert.equal(h.stops.length,0);
  assert.equal(h.requests.filter(r=>r.url.endsWith('/output')).length,0);
  assert.equal(h.events.has('pagehide'),false);
});
test('page recovery attaches to an active native run without replaying or reauthorizing it',()=>{
  const h=harness({active:true,devices:[device,sosexy],session_id:'scene_test',owner_id:'owner1',generation:1});
  h.hw.stateChanged(state());
  assert.deepEqual(Array.from(h.hw.connected()),['muse','sosexy']);
  assert.equal(h.hw.isArmed(),true);assert.equal(h.begins.length,0);assert.equal(h.stops.length,0);
});
test('stop is session-generation scoped and panic is explicitly global',()=>{
  const h=harness();h.hw.stop('end');assert.deepEqual(h.stops[0],['scene_test',1,'end']);
  h.hw.panic();assert.deepEqual(h.stops[1],['panic']);
});
test('older APK cannot silently use the foreground-only hardware path',async()=>{
  const h=harness({version:1,background_player:false});assert.equal(h.hw.supported(),false);
  await assert.rejects(h.hw.connect(device,true),/后台播放/);assert.equal(h.begins.length,0);
});
test('explicit native authorization failure does not fall back to browser execution',async()=>{
  const h=harness();await h.hw.connect(device,true);await h.hw.connect(sosexy,true);h.allow(false);
  assert.throws(()=>h.hw.authorize(state()),/未获授权/);
  assert.equal(h.hw.isArmed(),false);assert.equal(h.requests.filter(r=>r.url.endsWith('/output')).length,0);
});
test('ordinary mock devices remain page-owned and never open native output',async()=>{
  const h=harness();const mock={id:'mock',driver:'mock'};
  await h.hw.connect(mock,true);h.hw.authorize({...state(),devices:[mock]});
  assert.equal(h.begins.length,0);assert.equal(h.hw.backgroundManaged({...state(),devices:[mock]}),false);
});
const five=[device,sosexy,{id:'sk30',driver:'sk30_ble'},{id:'sk40',driver:'sk40_ble'},{id:'cx492b',driver:'cx492b_ble'}];
test('five-device APK connects and authorizes independent bindings without legacy target switching',async()=>{
  const h=harness({version:3,drivers:Object.fromEntries(five.map(d=>[d.driver,true]))});
  for(const d of five) await h.hw.connect(d,true);
  assert.deepEqual(h.connections,five.map(d=>d.driver));
  assert.deepEqual(Array.from(h.hw.connected()),five.map(d=>d.id));
  h.hw.authorize({...state(),devices:five});
  assert.deepEqual(h.begins[0].devices,five);
  h.nativeState.drivers.cx492b_ble=false;
  assert.deepEqual(Array.from(h.hw.connected()),['muse','sosexy','sk30','sk40']);
  assert.throws(()=>h.hw.authorize({...state(),devices:five}),/重新确认连接/);
});
test('old background APK keeps the two original devices and clearly rejects the three new ones',async()=>{
  const h=harness();
  for(const d of five.slice(0,2)) { assert.equal(h.hw.supportsDevice(d),true);await h.hw.connect(d,true); }
  for(const d of five.slice(2)) {
    assert.equal(h.hw.supportsDevice(d),false);
    await assert.rejects(h.hw.connect(d,true),/新 APK/);
  }
  assert.deepEqual(Array.from(h.hw.connected()),['muse','sosexy']);
  assert.equal(h.connections.length,0);
});
test('a connected SOSEXY never makes another GATT or an unknown driver look connected',()=>{
  const h=harness({version:3,active:true,devices:five,
    drivers:{muse_ble:true,sosexy_ble:true,sk30_ble:true,sk40_ble:true,cx492b_ble:false}});
  assert.deepEqual(Array.from(h.hw.connected()),['muse','sosexy','sk30','sk40']);
  assert.equal(h.hw.supportsDevice({driver:'unknown_ble'}),false);
});
test('disconnect releases only the requested GATT connection after stopping the authorized run',async()=>{
  const h=harness({version:3,drivers:Object.fromEntries(five.map(d=>[d.driver,true]))});
  for(const d of five) await h.hw.connect(d,true);
  h.current({...state(),devices:five});h.hw.authorize({...state(),devices:five});
  await h.hw.connect(five[4],false);
  assert.deepEqual(h.stops,[['scene_test',1,'pause']]);
  assert.deepEqual(h.disconnections,['cx492b_ble']);
  assert.equal(h.nativeState.drivers.sosexy_ble,true);
});
