const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scripts = path.join(__dirname, '../static/js');
const state = (extra = {}) => ({id:'s1', owner_id:'owner1', status:'active', generation:1,
  revision:1, created_at:1000, started_at:1000, messages:[], devices:[], ...extra});
const flush = async () => { for (let i=0; i<60; i++) await Promise.resolve(); };
function node() {
  const classes = new Set();
  return {hidden:false, disabled:false, value:'', textContent:'', checked:false, dataset:{}, style:{}, children:[], parentElement:null,
    events:new Map(), attributes:{}, scrollHeight:100, scrollTop:100, clientHeight:100,
    classList:{add:value=>classes.add(value), remove:value=>classes.delete(value), contains:value=>classes.has(value)},
    set innerHTML(_) { throw new Error('Scene content must not be interpreted as HTML'); },
    addEventListener(name, fn) { this.events.set(name, fn); },
    append(...items) { for (const item of items) {
      if (item.parentElement) item.parentElement.children = item.parentElement.children.filter(child => child !== item);
      item.parentElement = this; this.children.push(item);
    } },
    replaceChildren(...items) { this.children = items; },
    setAttribute(key, value) { this.attributes[key] = value; }, scrollIntoView() {},
  };
}
function harness() {
  const nodes = new Map(), events = new Map(), intervals = new Map(), timeouts = new Map(), requests = [], opened = [];
  const storage = new Map([['obsidian_scene_owner_id','owner1']]);
  const local = new Map();
  let server = null, responder, now = 1000_000, timerId = 0;
  const context = {console, URLSearchParams, AbortController, currentConvId:'conv1', pendingAttachments:[], ChatApp:{registerModule() {}},
    Date:class extends Date { static now() { return now; } },
    location:{search:''},
    document:{hidden:false, visibilityState:'visible', addEventListener:(n,fn) => events.set(n,fn),
      getElementById:id => { if (!nodes.has(id)) nodes.set(id,node()); return nodes.get(id); }},
    sessionStorage:{getItem:key=>storage.get(key) || null, setItem:(key,value)=>storage.set(key,String(value)), removeItem:key=>storage.delete(key)},
    localStorage:{getItem:key=>local.get(key) || null, setItem:(key,value)=>local.set(key,String(value)), removeItem:key=>local.delete(key)},
    addEventListener:(n,fn)=>events.set(n,fn),
    setInterval:(fn,ms)=>{ const id=intervals.size+1; intervals.set(id,{fn,ms}); return id; },
    clearInterval:id=>intervals.delete(id),
    setTimeout:(fn,ms)=>{ const id=++timerId; timeouts.set(id,{fn,ms}); return id; },
    clearTimeout:id=>timeouts.delete(id),
    openSubPage:url=>opened.push(url),
    fetch:async (url,options={})=>{
      const request={url, method:options.method || 'GET', signal:options.signal, body:options.body ? JSON.parse(options.body) : undefined};
      requests.push(request);
      let value = responder ? await responder(request) : server;
      if (url.endsWith('/heartbeat') && !responder) value = {status:server.status,generation:server.generation};
      return {ok:!value?.httpError, status:value?.httpError || 200,
        json:async()=>value?.httpError ? {detail:value.detail} : value};
    },
  };
  context.window = context;
  context.$ = context.document.getElementById;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(scripts,'chat/scene_runtime.js'),'utf8'), context);
  return {context, runtime:context.SceneRuntime, nodes, events, intervals, timeouts, requests, storage, opened,
    serve:value=>{server=value;}, respond:fn=>{responder=fn;}, time:value=>{now=value;},
    load:name=>vm.runInContext(fs.readFileSync(path.join(scripts,name),'utf8'),context)};
}

test('scene controls dock above other pages and hide only after the mounted scene view is ready', () => {
  const h=harness(), $=h.context.$;
  const overlay=$('subPageOverlay'), frame=$('subPageFrame'), bar=$('sceneGlobalBar');
  frame.contentWindow={};
  h.runtime.accept(state());
  assert.equal(bar.parentElement,$('sceneGlobalChatSlot'));
  assert.equal(bar.hidden,false);
  overlay.classList.add('show');overlay.dataset.route='/scenes';h.runtime.navigationChanged();
  assert.equal(bar.parentElement,$('sceneGlobalSubpageSlot'));
  assert.equal(bar.hidden,false); // Still loading: stop is available.
  h.runtime.sceneViewReady({},'s1');
  assert.equal(bar.hidden,false); // An unrelated/stale window cannot hide it.
  h.runtime.sceneViewReady(frame.contentWindow,'another-scene');
  assert.equal(bar.hidden,false);
  h.runtime.sceneViewReady(frame.contentWindow,'s1');
  assert.equal(bar.hidden,true);
  h.runtime.accept(state({revision:2}));
  assert.equal(bar.hidden,true);
  h.runtime.sceneViewReady(frame.contentWindow,null);
  assert.equal(bar.hidden,false); // Unloading/failing child restores the fallback.
  overlay.dataset.route='/';h.runtime.navigationChanged();
  assert.equal(bar.hidden,false);
  h.runtime.sceneViewReady(frame.contentWindow,'s1');
  assert.equal(bar.hidden,false); // A non-scene route always retains controls.
  overlay.classList.remove('show');h.runtime.navigationChanged();
  assert.equal(bar.parentElement,$('sceneGlobalChatSlot'));
  assert.equal(bar.hidden,false);
  assert.equal(h.requests.length,0); // Navigation never pauses or starts playback.
  h.runtime.accept(state({status:'ended',generation:2,revision:3}));
  assert.equal(bar.hidden,true);
});

test('real-device pause and panic stop locally before waiting for HTTP', async () => {
  for (const action of ['pause','panic']) {
    const h=harness(),order=[];h.runtime.accept(state());
    h.context.SceneHardware={isArmed:()=>true,stop:()=>order.push('native-stop'),stateChanged(){}};
    h.respond(r=>{order.push(r.url);return state({status:'ended',generation:2,revision:2});});
    await (action==='panic'?h.runtime.panic():h.runtime.action('pause'));
    assert.equal(order[0],'native-stop');
  }
});

test('only explicit page finish reveals, and its native stop is pause-only', async () => {
  const h=harness(),order=[];h.runtime.accept(state({phase:'aftercare'}));
  h.context.SceneHardware={isArmed:()=>true,stop:name=>order.push(name),stateChanged(){}};
  h.respond(r=>{
    order.push(r.url);
    return state({status:'ended',generation:3,revision:5,revealed_at:1001,
      card:{raw_text:'revealed'},messages:[{role:'assistant',content:'最后一句仍在。'}]});
  });
  await h.runtime.action('finish');
  assert.deepEqual(order,['pause','/api/scenes/sessions/s1/finish']);
  assert.equal(h.runtime.current().messages[0].content,'最后一句仍在。');
  assert.equal(h.runtime.current().card.raw_text,'revealed');
});

test('wind-down state keeps device authorization and a paused state cannot output', () => {
  const h=harness(), observed=[];
  h.context.SceneHardware={backgroundManaged:()=>true,isArmed:()=>true,
    stateChanged:value=>observed.push(value),authorize:()=>assert.fail('Must not reauthorize')};
  h.runtime.accept(state({phase:'aftercare'}));
  assert.equal(h.runtime.canOutput(),true);
  assert.equal(h.context.$('sceneGlobalStatus').textContent,'收尾中');
  h.runtime.accept(state({phase:'aftercare',status:'paused',generation:2,revision:2}));
  assert.equal(h.runtime.canOutput(),false);
  assert.equal(h.runtime.isActive(),true);
  assert.equal(observed.length,2);
});

test('legacy foreground-only bridge still fails closed on background', async () => {
  const h=harness();h.runtime.accept(state());let stopped=0;
  h.context.SceneHardware={isArmed:()=>true,stop:()=>stopped++,stateChanged(){}};
  h.respond(()=>state({status:'paused',generation:2,revision:2}));
  h.load('chat/lifecycle.js');h.context.onChatBackground();await flush();
  assert.equal(stopped,1);assert.equal(h.runtime.current().status,'paused');
  assert.equal(h.requests.filter(r=>r.url.endsWith('/pause')).length,1);
});

test('native-owned scene keeps running across background and chat socket loss without UI heartbeats', async () => {
  const h=harness();h.runtime.accept(state({devices:[{id:'muse',driver:'muse_ble'}]}));let stopped=0;
  h.context.SceneHardware={isArmed:()=>true,backgroundManaged:()=>true,stop:()=>stopped++,stateChanged(){}};
  h.load('chat/lifecycle.js');h.context.onChatBackground();
  h.runtime.connectionChanged(false);await h.runtime.heartbeat();await flush();
  assert.equal(stopped,0);assert.equal(h.runtime.current().status,'active');
  assert.equal(h.requests.filter(r=>r.url.endsWith('/pause')||r.url.endsWith('/heartbeat')).length,0);
});

test('a reloaded page reattaches native owner but never automatically starts output', async () => {
  const h=harness();let begins=0;
  h.context.ObsidianSceneDevices={status:()=>JSON.stringify({version:2,owner_id:'native-owner',session_id:'s1',active:true}),begin:()=>begins++};
  h.load('chat/scene_runtime.js');h.context.SceneRuntime.accept(state({owner_id:'native-owner'}));
  assert.equal(h.context.SceneRuntime.isOwner(),true);assert.equal(begins,0);
  assert.equal(h.storage.get('obsidian_scene_last_id'),'s1');
});

test('a fresh page can reveal a scene that ended while its WebView was gone', async () => {
  const h=harness();h.storage.clear();
  h.context.ObsidianSceneDevices={status:()=>JSON.stringify({version:2,owner_id:'native-owner',session_id:'s1',active:false})};
  h.respond(r=>r.url.endsWith('/current')?null:state({owner_id:'native-owner',status:'ended',generation:3,card:{raw_text:'revealed'}}));
  h.load('chat/scene_runtime.js');await h.context.SceneRuntime.recover();
  assert.equal(h.context.SceneRuntime.current().status,'ended');
  assert.equal(h.context.SceneRuntime.current().card.raw_text,'revealed');
});

test('explicit actions and queued failures carry the original generation', async () => {
  const h=harness();h.runtime.accept(state());h.respond(()=>state({status:'paused',generation:2,revision:2}));
  await h.runtime.action('pause');assert.equal(h.requests[0].body.generation,1);
  const newer=harness();newer.runtime.accept(state());newer.respond(()=>state({status:'paused',generation:2,revision:2}));
  newer.runtime.outputFailed();await flush();assert.equal(newer.requests[0].body.generation,1);
});

test('old mode cleanup cannot send raw device stops into armed scene hardware', () => {
  const h=harness();h.runtime.accept(state());let rawStops=0;
  h.context.SceneHardware={isArmed:()=>true};
  h.context.ObsidianMuse={stop:()=>rawStops++};
  h.context.toyDriver={stop:()=>rawStops++};
  h.load('chat/control_emergency_stop.js');
  assert.equal(h.context.ControlEmergencyStop.stopDevice('control_release'),false);
  assert.equal(rawStops,0);
});

test('parent heartbeat survives subpage unsubscribe, but hidden/paused/non-owner windows do not keep a scene alive', async () => {
  const h=harness(); h.serve(state()); h.events.get('DOMContentLoaded')(); await flush();
  assert.equal([...h.intervals.values()][0].ms,15000);
  let updates=0;
  const leaveChild=h.runtime.subscribe(()=>updates++); leaveChild();
  await [...h.intervals.values()][0].fn();
  assert.equal(h.requests.filter(r=>r.url.endsWith('heartbeat')).length,1);
  assert.equal(updates,1);
  h.context.document.hidden=true; await h.runtime.heartbeat();
  h.context.document.hidden=false;
  h.runtime.accept(state({owner_id:'another',revision:2})); await h.runtime.heartbeat();
  h.runtime.accept(state({status:'paused',generation:2,revision:3})); await h.runtime.heartbeat();
  assert.equal(h.requests.filter(r=>r.url.endsWith('heartbeat')).length,1);
  assert.deepEqual([...h.storage.keys()].sort(),['obsidian_scene_last_id','obsidian_scene_owner_id']);
});

test('native background stops scene heartbeats even when the WebView document stays visible', async () => {
  const h=harness(); h.serve(state()); h.runtime.accept(state());
  h.load('chat/lifecycle.js');
  assert.equal(h.context.isChatForeground(),true);
  await h.runtime.heartbeat();
  h.context.onChatBackground();
  assert.equal(h.context.isChatForeground(),false);
  assert.equal(h.context.document.hidden,false);
  await h.runtime.heartbeat();
  assert.equal(h.requests.filter(r=>r.url.endsWith('/heartbeat')).length,1);
  await h.context.onChatForeground();
  await h.runtime.heartbeat();
  assert.equal(h.requests.filter(r=>r.url.endsWith('/heartbeat')).length,2);
});

test('failed disconnect pause retries without renewing the lease when HTTP recovers before the socket', async () => {
  const h=harness(); h.runtime.accept(state());
  let attempts=0, release;
  const gate=new Promise(resolve=>{release=resolve;});
  const paused=state({status:'paused',generation:2,revision:2});
  h.respond(async r=>{
    if (r.url.endsWith('/pause')) {
      if (++attempts===1) throw new Error('offline');
      await gate;
    }
    return paused;
  });
  h.runtime.connectionChanged(false); await flush();
  assert.equal(attempts,1); assert.equal(h.runtime.current().status,'active');
  const retries=[h.runtime.heartbeat(),h.runtime.heartbeat()]; await flush();
  assert.equal(attempts,2); // Concurrent ticks share one retry, not two pauses.
  assert.equal(h.requests.some(r=>r.url.endsWith('/heartbeat')),false);
  await assert.rejects(h.runtime.action('resume'),/连接恢复/);
  release(); await Promise.all(retries);
  assert.equal(h.runtime.current().status,'paused');
  await h.runtime.heartbeat();
  h.runtime.connectionChanged(true); await flush();
  await h.runtime.heartbeat();
  assert.equal(h.runtime.current().status,'paused');
  assert.equal(h.requests.some(r=>r.url.endsWith('/heartbeat') || r.url.endsWith('/resume')),false);
});

test('a socket reconnect alone does not clear the pending pause after failed HTTP requests', async () => {
  const h=harness(); h.runtime.accept(state());
  let attempts=0;
  h.respond(r=>{
    if (r.url.endsWith('/pause')) {
      if (++attempts<3) throw new Error('pause unavailable');
      return state({status:'paused',generation:2,revision:2});
    }
    return state();
  });
  h.runtime.connectionChanged(false); await flush();
  h.runtime.connectionChanged(true); await flush();
  assert.equal(attempts,2);
  await h.runtime.heartbeat();
  assert.equal(attempts,3); assert.equal(h.runtime.current().status,'paused');
  assert.equal(h.requests.some(r=>r.url.endsWith('/heartbeat')),false);
});

test('a hung pause is aborted so the next heartbeat tick can retry without sending a heartbeat', async () => {
  const h=harness(); h.runtime.accept(state());
  let attempts=0;
  h.respond(r=>{
    if (r.url.endsWith('/pause') && ++attempts===1) {
      return new Promise((_,reject)=>r.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
    }
    return state({status:'paused',generation:2,revision:2});
  });
  h.runtime.connectionChanged(false); await flush();
  const timeout=[...h.timeouts.values()][0];
  assert.equal(timeout.ms,5000); timeout.fn(); await flush();
  await h.runtime.heartbeat();
  assert.equal(attempts,2); assert.equal(h.timeouts.size,0);
  assert.equal(h.runtime.current().status,'paused');
  assert.equal(h.requests.some(r=>r.url.endsWith('/heartbeat')),false);
});

test('late disconnect pause response stays scoped to the old session', async () => {
  const h=harness(); h.runtime.accept(state());
  let release;
  const gate=new Promise(resolve=>{release=resolve;});
  const newer=state({id:'s2',created_at:1020});
  h.respond(r=>r.url.endsWith('/pause') ? gate : newer);
  h.runtime.connectionChanged(false); await flush();
  h.runtime.accept(newer);
  h.runtime.connectionChanged(true); await flush();
  release(state({status:'paused',generation:2,revision:2})); await flush();
  assert.equal(h.runtime.current().id,'s2');
  assert.equal(h.runtime.current().status,'active');
  // The new session was also observed while disconnected, so its own pause
  // remains pending instead of being cleared by the old request's result.
  h.respond(r=>r.url.endsWith('/s2/pause') ? {...newer,status:'paused',generation:2,revision:2} : newer);
  await h.runtime.heartbeat();
  assert.equal(h.runtime.current().id,'s2'); assert.equal(h.runtime.current().status,'paused');
  assert.deepEqual(h.requests.filter(r=>r.url.endsWith('/pause')).map(r=>r.url),
    ['/api/scenes/sessions/s1/pause','/api/scenes/sessions/s2/pause']);
  assert.equal(h.requests.some(r=>r.url.endsWith('/heartbeat')),false);
});

test('late state/playback cannot revive ended sessions or overwrite the newer scene', () => {
  const h=harness(), r=h.runtime;
  r.accept(state({revision:8}));
  assert.equal(r.accept(state({revision:7})),false);
  r.accept(state({status:'ended',generation:2,revision:9,ended_at:1010,card:{raw_text:'reveal'}}));
  assert.equal(r.accept(state({generation:1,revision:50})),false);
  assert.equal(r.handleEvent({type:'scene_playback',data:state({generation:2,revision:99})}),false);
  r.accept(state({id:'s2',created_at:1020}));
  assert.equal(r.accept(state({status:'ended',generation:2,revision:99})),false);
  assert.equal(r.accept(state({generation:3,revision:99})),false);
  assert.equal(r.current().id,'s2');
  assert.equal(r.handleEvent({type:'scene_playback',data:state({id:'s2',revision:2,devices:[{id:'b'}]})}),true);
  assert.equal(r.current().devices[0].id,'b');
});

test('entry transmits explicit selected devices/owner only, and legacy control prevents it', async () => {
  const h=harness(); h.serve(state({status:'preparing'}));
  const options={device_ids:['mock_b'],safeword:'stop-now',request_id:'request1'};
  h.context.ControlRuntime={currentSession:()=>({status:'active'})};
  await assert.rejects(h.runtime.start(options),/先结束/); assert.equal(h.requests.length,0);
  h.context.ControlRuntime.currentSession=()=>null;
  await h.runtime.start(options);
  assert.deepEqual(h.requests[0].body,{...options,conv_id:'conv1',owner_id:'owner1'});
  h.respond(()=>({httpError:409,detail:'scene_device_unavailable'}));
  await assert.rejects(h.runtime.start(options),/未全部连接/);
});

test('socket loss pauses, refresh recovers, and panic coalesces through the existing global stop bridge', async () => {
  const h=harness(); h.runtime.accept(state());
  h.respond(r=>r.url.endsWith('/pause') ? state({status:'paused',generation:2,revision:2}) : null);
  h.runtime.connectionChanged(false); await flush();
  assert.equal(h.runtime.current().status,'paused');
  h.respond(()=>state({generation:3,revision:3}));
  h.runtime.connectionChanged(true); await flush();
  assert.equal(h.runtime.current().status,'active');
  let release; const gate=new Promise(resolve=>{release=resolve;});
  h.respond(async r=>r.url==='/api/scenes/panic' ? gate : state({status:'ended',generation:4,revision:4}));
  h.load('chat/control_emergency_stop.js');
  assert.equal(h.context.ControlEmergencyStop.stopDevice('panic'),true);
  const second=h.runtime.panic(); await flush();
  assert.equal(h.requests.filter(r=>r.url==='/api/scenes/panic').length,1);
  release({stopped:true}); await second;
  assert.equal(h.runtime.isActive(),false);
});

test('late old Dom recovery stops its local driver without ending a newly active scene', async () => {
  const h=harness(), stopped=[];
  h.context.toyDriver={active:'mock',stop:reason=>stopped.push(reason)};
  h.load('chat/control_emergency_stop.js');
  h.load('chat/control_runtime.js');
  const owner=h.storage.get('obsidian_control_owner_client_id');
  h.respond(r=>r.url.includes('/api/control/sessions/current?') ?
    {session_id:'old-dom',kind:'dom',status:'active',conv_id:'conv1',owner_client_id:owner} : null);
  await h.context.ControlRuntime.recover('conv1');
  assert.equal(h.context.ControlRuntime.currentSession().session_id,'old-dom');
  h.runtime.accept(state()); // Another window has already started the scene.
  h.respond(()=>null);
  await h.context.ControlRuntime.recover('conv1'); await flush();
  assert.equal(h.context.ControlRuntime.currentSession(),null);
  assert.deepEqual(stopped,['recover']);
  assert.equal(h.runtime.current().status,'active');
  assert.equal(h.requests.some(r=>r.url==='/api/scenes/panic'),false);
});

for (const reason of ['panic','safeword','device_emergency_stop','emergency_stop']) {
  test(`global ${reason} still ends the active scene`, async () => {
    const h=harness(); h.runtime.accept(state()); h.load('chat/control_emergency_stop.js');
    h.respond(()=>state({status:'ended',generation:2,revision:2}));
    assert.equal(h.context.ControlEmergencyStop.stopDevice(reason),true);
    await flush();
    assert.equal(h.requests.filter(r=>r.url==='/api/scenes/panic').length,1);
    assert.equal(h.runtime.isActive(),false);
  });
}

for (const reason of ['stop','control_release','session_switch','recover_failed']) {
  test(`scoped ${reason} does not escalate to a scene panic`, () => {
    const h=harness(), stopped=[]; h.runtime.accept(state());
    h.context.toyDriver={active:'mock',stop:value=>stopped.push(value)};
    h.load('chat/control_emergency_stop.js');
    assert.equal(h.context.ControlEmergencyStop.stopDevice(reason),true);
    assert.deepEqual(stopped,[reason]); assert.equal(h.requests.length,0);
    assert.equal(h.runtime.isActive(),true);
  });
}

test('ended transient messages expire in the parent, while the card remains available', async () => {
  const h=harness(); h.runtime.accept(state({status:'ended',ended_at:1000,card:{raw_text:'keep'},messages:[{content:'temporary'}]}));
  h.time(2801_000); await h.runtime.heartbeat();
  assert.equal(h.runtime.current().messages.length,0);
  assert.equal(h.runtime.current().card.raw_text,'keep');
  assert.equal(h.requests.length,0);
});

function childHarness() {
  const nodes=new Map(), events=new Map(), requests=[], intervals=new Map(), timeouts=new Map();
  const votes=['up','down',''].map(v=>{ const n=node();n.dataset.sceneVote=v;return n; });
  const devices=[{id:'mock_a',name:'模拟 A',description:'',connected:true}];
  let current=null, listener, feedback=null, unsubscribed=false;
  const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  const runtime={
    current:()=>current, isOwner:()=>true, recover:async()=>current,
    ownerId:()=>'owner1', action:async name=>{requests.push({action:name});},
    subscribe:fn=>{listener=fn;fn(current);return()=>{unsubscribed=true;};},
    request:async(method,url,body)=>{
      requests.push({method,url,body});
      if(url.endsWith('/config'))return {enabled:true,compiler_ready:true,devices};
      if(url.endsWith('/devices'))return devices;
      if(url.endsWith('/sessions'))return [];
      if(url.endsWith('/feedback')) {feedback={...body,note_status:'processed'};return feedback;}
      return {...current,feedback};
    },
    start:async options=>{requests.push({start:options});},
  };
  const context={console, Date, Map, Set, Promise,
    document:{getElementById:$,createElement:()=>node(),createTextNode:s=>({textContent:s}),
      querySelectorAll:sel=>sel==='[data-scene-vote]'?votes:[]},
    localStorage:{getItem:()=>'',setItem(){}},
    setInterval:(fn,ms)=>{intervals.set(1,{fn,ms});return 1;},clearInterval:id=>intervals.delete(id),
    setTimeout:(fn,ms)=>{timeouts.set(1,{fn,ms});return 1;},clearTimeout:id=>timeouts.delete(id),
    addEventListener:(name,fn)=>events.set(name,fn),parent:{SceneRuntime:runtime},
  };
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(scripts,'scenes.js'),'utf8'),context);
  return {$,votes,events,requests,intervals,timeouts,
    feedback:value=>{feedback=value;}, update:value=>{current=value;listener(value);},
    unsubscribed:()=>unsubscribed};
}

test('scene view is blind while active, renders literal text on reveal, and restores saved feedback without overwriting an edit', async () => {
  const h=childHarness(); await flush();
  h.update(state({messages:[{role:'assistant',content:'<img src=x>'}],devices:[]}));
  assert.equal(h.$('sceneReveal').hidden,true);
  assert.equal(h.$('sceneMessages').children[0].textContent,'<img src=x>');
  h.feedback({rating:'down',reason:'已有原因',note_status:'processed'});
  h.update(state({status:'ended',card:{raw_text:'<script>literal card</script>'},ended_at:1005})); await flush();
  assert.equal(h.$('sceneCardText').textContent,'<script>literal card</script>');
  assert.equal(h.$('sceneFeedbackReason').value,'已有原因');
  assert.equal(h.votes[1].attributes['aria-pressed'],'true');
  h.$('sceneFeedbackReason').value='新原因'; h.$('sceneFeedbackReason').events.get('input')();
  h.votes[0].events.get('click')();
  await h.$('sceneFeedbackSave').events.get('click')();
  assert.deepEqual({...h.requests.find(r=>r.method==='PUT').body},{rating:'up',reason:'新原因'});
  h.events.get('pagehide')();
  assert.equal(h.unsubscribed(),true); assert.equal(h.intervals.size,0);
  assert.equal(h.requests.some(r=>/\/end$|\/pause$/.test(r.url||'')),false);
});

test('entry requires an explicit checkbox and safeword before invoking the parent', async () => {
  const h=childHarness(); await flush();
  h.$('sceneSafeword').value='stop-now';
  await h.$('sceneEnter').events.get('click')();
  assert.equal(h.requests.some(r=>r.start),false);
  const check=h.$('sceneDevices').children[0].children[0].children[0].children[0];
  check.checked=true; check.events.get('change')();
  await h.$('sceneEnter').events.get('click')();
  const body=h.requests.find(r=>r.start).start;
  assert.deepEqual(Array.from(body.device_ids),['mock_a']);
  assert.equal(body.safeword,'stop-now'); assert.ok(body.request_id);
});

test('wind-down and safety pause retain input; explicit reveal retains final conversation', async () => {
  const h=childHarness();await flush();
  const messages=[{role:'assistant',content:'刚才的话。'},{role:'user',content:'再聊聊。'},
    {role:'assistant',content:'最后一句也留下。'}];
  h.update(state({phase:'aftercare',messages}));
  assert.equal(h.$('sceneStatus').textContent,'收尾中');
  assert.equal(h.$('sceneLive').hidden,false);
  assert.equal(h.$('sceneMessageForm').hidden,false);
  assert.equal(h.$('sceneReveal').hidden,true);
  assert.equal(h.$('sceneCardText').textContent,'');
  h.update(state({phase:'aftercare',status:'paused',reason:'safeword',messages}));
  assert.equal(h.$('sceneMessageForm').hidden,false);
  assert.equal(h.$('sceneReveal').hidden,true);
  assert.match(h.$('sceneReason').textContent,/只有手动继续才恢复设备/);
  await h.$('sceneEnd').events.get('click')();
  assert.equal(h.requests.at(-1).action,'finish');
  h.update(state({status:'ended',revealed_at:1005,ended_at:1005,card:{raw_text:'原卡'},messages}));
  await flush();
  assert.equal(h.$('sceneReveal').hidden,false);
  assert.equal(h.$('sceneLive').hidden,false);
  assert.equal(h.$('sceneMessages').children.at(-1).textContent,'最后一句也留下。');
  assert.equal(h.$('sceneMessageForm').hidden,true);
  assert.equal(h.$('scenePause').hidden,true);
  assert.equal(h.$('sceneEnd').hidden,true);
  assert.equal(h.$('sceneOutputPanel').hidden,true);
});

test('interrupted but unrevealed scene offers a manual reveal, not the card itself', async () => {
  const h=childHarness();await flush();
  h.update(state({status:'ended',reason:'server_restart',has_card:true,revealed_at:null,messages:[]}));
  assert.equal(h.$('sceneLive').hidden,false);
  assert.equal(h.$('sceneReveal').hidden,true);
  assert.equal(h.$('sceneEnd').hidden,false);
  assert.equal(h.$('sceneEnd').textContent,'结束并揭卡');
  assert.equal(h.$('sceneMessageForm').hidden,true);
  await h.$('sceneEnd').events.get('click')();
  assert.equal(h.requests.at(-1).action,'finish');
});
