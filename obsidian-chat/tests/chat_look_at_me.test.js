const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const calls=[];
  const context={console, setTimeout, clearTimeout, $:()=>null,
    document:{visibilityState:'visible',addEventListener(){}},
    currentConvId:'today', dailyChatState:{enabled:true,active_conv_id:'today'},
    scheduleVisibleMessagesSeen(){}, ChatApp:{registerModule(){}},
  };
  context.window={addEventListener(){},ObsidianLookAtMe:{requestId:()=> 'request',ready:(...args)=>calls.push(args)}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/js/chat/lifecycle.js'),'utf8'),context);
  return {context,calls};
}

test('native handshake carries request and actual daily window after current sync',()=>{
  const {context,calls}=harness();
  assert.equal(context.completeChatSync(1),false);assert.equal(calls.length,0);
  assert.equal(context.completeChatSync(0),true);
  assert.deepEqual(calls,[['request','today','today']]);
});

test('background, disabled daily mode, and ordinary unlocking cannot accept',()=>{
  const {context,calls}=harness();
  context.document.visibilityState='hidden';context.completeChatSync(0);assert.equal(calls.length,0);
  context.document.visibilityState='visible';context.dailyChatState.enabled=false;
  context.completeChatSync(0);assert.equal(calls.length,0);
  context.dailyChatState.enabled=true;context.window.ObsidianLookAtMe.requestId=()=>'';
  context.completeChatSync(0);assert.equal(calls.length,0);
});

test('historical window is reported truthfully for native rejection and return',()=>{
  const {context,calls}=harness();context.currentConvId='history';
  context.completeChatSync(0);
  assert.deepEqual(calls,[['request','history','today']]);
});

function initiativeHarness(events) {
  const speech=[], toasts=[], errors=[];
  let read=false;
  const context={
    console:{error:(...args)=>errors.push(args)}, TextDecoder, URLSearchParams,
    setTimeout:()=>1,clearTimeout(){},location:{search:''}, $:()=>null,
    localStorage:{getItem:()=>null},
    document:{addEventListener(){},getElementById:()=>null},ChatApp:{registerModule(){}},
    currentConvId:'today',currentMessages:[{id:'previous',content:'保留这句'}],toyConnected:true,
    toyToggleConnect(){},renderMessages(){},scrollBottom(){},formatMsg:text=>text,
    updateStreamingAssistantMessage(id, text) {
      const message = context.currentMessages.find(m => m.id === id);
      if (message) message.content = text;
    },
    resetStreamingAssistantMessage(id) {
      const message = context.currentMessages.find(m => m.id === id);
      if (message) message.content = '...';
    },
    cleanAssistantContent:text=>text,ttsSpeak:(...args)=>speech.push(args),
    showToast:text=>toasts.push(text),
    fetch:async()=>({body:{getReader:()=>({read:async()=>{
      if(read)return {done:true};
      read=true;
      return {done:false,value:Buffer.from(events.map(event=>'data: '+JSON.stringify(event)+'\n\n').join(''))};
    }})}}),
  };
  context.window=context;
  vm.createContext(context);
  for(const file of ['ai_dom.js','whisper_toy.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/js/chat',file),'utf8'),context);
  }
  vm.runInContext('aiDomMode=true; whisperMode=true; aiDomInitiativeStart=()=>{}; whisperInitStart=()=>{};',context);
  return {context,speech,toasts,errors};
}

for(const entry of ['aiDomFireInitiative','whisperFireInit']) {
  for(const body of ['', '先抱抱。']) {
    test(entry+' settles failed invitation without erasing delivered text: '+JSON.stringify(body),async()=>{
      const events=[{type:'start',id:'reply'}];
      if(body)events.push({type:'chunk',content:body});
      events.push({type:'tool_error',id:'reply',tool:'mobile.look_at_me',error:'device_busy:other',
        empty_reply:!body,message:'手机已有待处理的陪伴邀请或正在陪伴中'});
      const {context,speech,toasts,errors}=initiativeHarness(events);
      await context[entry]();
      assert.deepEqual(errors,[]);
      assert.equal(context.currentMessages[0].id,'previous');
      assert.equal(context.currentMessages.length,body?2:1);
      assert.ok(context.currentMessages.every(message=>message.content!=='...'));
      assert.deepEqual(speech,body?[[body,'reply']]:[]);
      assert.deepEqual(toasts,[events.at(-1).message]);
    });
  }
  test(entry+' removes speech buffered for a tool-only delivery',async()=>{
    const {context,speech,errors}=initiativeHarness([
      {type:'start',id:'reply'},
      {type:'chunk',content:'【陪我：20｜想贴贴】'}, // tolerate an older server's buffered tag
      {type:'tool_delivery',id:'reply',tool:'mobile.look_at_me'},
    ]);
    await context[entry]();
    assert.deepEqual(errors,[]);assert.deepEqual(speech,[]);
    assert.equal(context.currentMessages.length,1);
  });
}
